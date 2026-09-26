#!/usr/bin/env python3
"""出口守护：用假内核演各种不通的情况，看它分析得对不对、修得对不对、有没有碰红线。不碰真网。"""
from __future__ import annotations

import contextlib
import copy
import dataclasses
import fcntl
import io
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import guard as G
from config import GuardCfg
from watch import Policy

POLICY = Policy(ai_group="Claude-Only", homebb_group="RESIP-Claude", homebb_members=("RESIP-A", "RESIP-B"),
                daily_group="日常出口")
ALL_ALIVE = {"a1", "a2", "b1", "b2", "tw1", "tw2", "hk1", "RESIP-A", "RESIP-B", "DIRECT"}
EVERYTHING = {G.CORE, G.DAILY, G.HOMEBB}


def topo(**over) -> dict:
    g = {
        "Final": {"type": "Selector", "now": "Proxies", "all": ["Proxies", "DIRECT"]},
        "Proxies": {"type": "Selector", "now": "日常出口", "all": ["日常出口", "TW", "HK", "DIRECT"]},
        "TW": {"type": "Selector", "now": "tw1", "all": ["tw1", "tw2"]},
        "HK": {"type": "Selector", "now": "hk1", "all": ["hk1"]},
        "日常出口": {"type": "Selector", "now": "日常-自动", "all": ["日常-自动", "机场A-自动", "机场B-自动"]},
        "日常-自动": {"type": "Fallback", "now": "机场A-自动", "all": ["机场A-自动", "机场B-自动"], "fixed": ""},
        "机场A-自动": {"type": "URLTest", "now": "a1", "all": ["a1", "a2"], "fixed": ""},
        "机场B-自动": {"type": "URLTest", "now": "b1", "all": ["b1", "b2"], "fixed": ""},
        "Claude-Only": {"type": "Selector", "now": "RESIP-Claude", "all": ["RESIP-Claude"]},
        "RESIP-Claude": {"type": "Fallback", "now": "RESIP-A", "all": ["RESIP-A", "RESIP-B"], "fixed": ""},
    }
    for name, fields in over.items():
        g.setdefault(name, {}).update(fields)
    return g


class FakeClash:
    """按 mihomo 的规矩模拟：组按当前选择往下走到节点；自动组只在重测后才换，钉住的节点活着就一直用它。

    types：给节点指定 mihomo 报的类型（默认 Shadowsocks，DIRECT 是 Direct）。
    hidden：像订阅节点那样不在 /proxies 里；provider_types 是订阅接口能查到的类型。
    unknown：测这些名字时接口出错（ProbeError）。on_probe：每次测速前调一下（测试里用来推进时钟、模拟人插手）。
    """

    def __init__(self, groups: dict, alive: set, match: str = "Final", flaky: dict | None = None,
                 up: bool = True, retest_error: str = "", types: dict | None = None,
                 hidden: set | None = None, provider_types: dict | None = None,
                 unknown: set | None = None) -> None:
        self.g = copy.deepcopy(groups)
        self.alive = set(alive)
        self.match = match
        self.flaky = dict(flaky or {})  # 名字 → 前几次测速故意失败
        self.up = up
        self.retest_error = retest_error
        self.types = dict(types or {})
        self.hidden = set(hidden or ())
        self.provider_types = dict(provider_types or {})
        self.unknown = set(unknown or ())
        self.match_error: Exception | None = None
        self.on_probe = lambda name: None
        self.calls: list[tuple] = []
        self.hints: dict[str, str | None] = {}  # 名字 → 测它时守护给的订阅提示

    def reachable(self) -> bool:
        return self.up

    def proxies(self) -> dict:
        out = copy.deepcopy(self.g)
        for info in self.g.values():
            for m in info.get("all", []):
                if m not in self.hidden:
                    out.setdefault(m, {"type": self.types.get(m, "Direct" if m == "DIRECT" else "Shadowsocks")})
        out.setdefault("DIRECT", {"type": "Direct"})
        return out

    def providers(self) -> dict:
        if not self.provider_types:
            return {}
        return {"sub": {"vehicleType": "File",
                        "proxies": [{"name": n, "type": t} for n, t in self.provider_types.items()]}}

    def match_target(self, fresh: bool = False) -> str:
        if self.match_error:
            raise self.match_error
        return self.match

    def _now(self, name: str) -> str:
        info = self.g[name]
        if info.get("fixed") and info["type"] in G.AUTO_TYPES and self._ok(info["fixed"]):
            return info["fixed"]
        return info["now"]

    def _ok(self, name: str, seen: tuple = ()) -> bool:
        if name in seen:
            return False
        if name not in self.g:
            return name in self.alive
        return self._ok(self._now(name), seen + (name,))

    def probe(self, name: str, url: str, provider: str | None = None) -> int | None:
        self.calls.append(("delay", name))
        self.hints[name] = provider
        self.on_probe(name)
        if name in self.unknown:
            raise G.ProbeError("/proxies/x/delay HTTP 500")
        if self.flaky.get(name, 0) > 0:
            self.flaky[name] -= 1
            return None
        return 42 if self._ok(name) else None

    def retest(self, group: str, url: str) -> str:
        self.calls.append(("retest", group))
        if self.retest_error:
            return self.retest_error
        info = self.g[group]
        if info["type"] in ("URLTest", "Fallback"):
            info["fixed"] = ""  # 实测 mihomo 1.19：对 url-test / fallback 组测速会顺带解开固定
        live = [m for m in info["all"] if self._ok(m)]
        if live:
            info["now"] = live[0]
            return "ok"
        return "dead"

    def select(self, group: str, name: str) -> None:
        self.calls.append(("select", group, name))
        self.g[group]["now"] = name

    def unfix(self, group: str) -> None:
        self.calls.append(("unfix", group))
        self.g[group]["fixed"] = ""

    def selects(self) -> list[tuple]:
        return [c for c in self.calls if c[0] == "select"]

    def changes(self) -> list[tuple]:
        return [c for c in self.calls if c[0] in ("select", "unfix", "retest")]


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def make(clash: FakeClash, dry_run: bool = False, clock: Clock | None = None, lock_path=None,
         policy: Policy = POLICY, **cfg) -> G.Guard:
    conf = GuardCfg(confirm_delay=0, cooldown=60, **cfg)
    return G.Guard(clash, conf, policy, dry_run=dry_run, now=clock or Clock(), sleep=lambda s: None,
                   lock_path=lock_path)


def daily(reps):
    return next(r for r in reps if r.path == G.DAILY)


def homebb(reps):
    return next(r for r in reps if r.path == G.HOMEBB)


class HealthyTest(unittest.TestCase):
    def test_all_good_does_nothing(self):
        c = FakeClash(topo(), ALL_ALIVE)
        guard = make(c)
        self.assertEqual(guard.tick(), [])
        self.assertEqual(c.changes(), [])
        self.assertEqual(guard.last_checked, EVERYTHING)

    def test_blip_is_confirmed_before_acting(self):
        c = FakeClash(topo(), ALL_ALIVE, flaky={"Final": 1})  # 第一次测失败，复测就好了
        self.assertEqual(make(c).tick(), [])
        self.assertEqual(c.changes(), [])

    def test_recovery_after_both_checks_still_means_no_change(self):
        # 两次都测失败（抖得久一点），但动手前复核时已经好了：不能再去改选
        c = FakeClash(topo(Proxies={"now": "TW"}), ALL_ALIVE, flaky={"Final": 2})
        (r,) = make(c).tick()
        self.assertEqual(c.selects(), [])
        self.assertIn("目标已自行恢复", r.blocked)

    def test_target_recovered_before_retest_leaves_auto_group_alone(self):
        # 自动组没被钉住；两次都测失败、动手前复核时又好了：重测也会让组换选择，一样不能做
        c = FakeClash(topo(), ALL_ALIVE, flaky={"Final": 2})
        (r,) = make(c).tick()
        self.assertEqual(c.changes(), [])
        self.assertIn("目标已自行恢复", r.blocked)
        self.assertTrue(r.recovered)

    def test_homebb_is_checked_less_often(self):
        clock = Clock()
        c = FakeClash(topo(), ALL_ALIVE)
        guard = make(c, clock=clock)
        guard.tick()
        clock.t += 5
        guard.tick()
        self.assertNotIn(G.HOMEBB, guard.last_checked)
        self.assertEqual(c.calls.count(("delay", "Claude-Only")), 1)  # 30 秒内只测一次
        clock.t += 30
        guard.tick()
        self.assertEqual(c.calls.count(("delay", "Claude-Only")), 2)


class DailyTest(unittest.TestCase):
    def test_dead_node_in_auto_group_is_retested_away(self):
        c = FakeClash(topo(), ALL_ALIVE - {"a1"})
        (r,) = make(c).tick()
        self.assertEqual(r.path, G.DAILY)
        self.assertTrue(r.recovered)
        self.assertIn(("retest", "机场A-自动"), c.calls)
        self.assertEqual(c.g["机场A-自动"]["now"], "a2")
        self.assertEqual(c.selects(), [])  # 没动任何手动组
        self.assertIn("当前节点 a1 不通", r.cause)

    def test_pinned_auto_group_is_unfixed(self):
        c = FakeClash(topo(**{"机场A-自动": {"fixed": "a1"}}), ALL_ALIVE - {"a1"})
        (r,) = make(c).tick()
        self.assertTrue(r.recovered)
        self.assertIn(("unfix", "机场A-自动"), c.calls)
        self.assertIn("被钉在 a1", r.cause)
        self.assertTrue(any("解开 机场A-自动" in a for a in r.actions))

    def test_dead_subscription_falls_back_to_next_one(self):
        c = FakeClash(topo(), ALL_ALIVE - {"a1", "a2"})
        (r,) = make(c).tick()
        self.assertTrue(r.recovered)
        self.assertEqual(c.g["日常-自动"]["now"], "机场B-自动")
        self.assertEqual(c.selects(), [])

    def test_manual_group_switches_inside_itself_first(self):
        c = FakeClash(topo(Proxies={"now": "TW"}), ALL_ALIVE - {"tw1"})
        (r,) = make(c).tick()
        self.assertTrue(r.recovered)
        self.assertEqual(c.selects(), [("select", "TW", "tw2")])  # 还留在台湾，没被拉去别处
        self.assertIn("TW 是手动组，固定选了 tw1", r.cause)

    def test_dead_manual_region_moves_to_daily_group(self):
        c = FakeClash(topo(Proxies={"now": "TW"}), ALL_ALIVE - {"tw1", "tw2"})
        (r,) = make(c).tick()
        self.assertTrue(r.recovered)
        self.assertIn(("select", "Proxies", "日常出口"), c.calls)  # 同时测了日常出口和 HK，优先日常出口

    def test_fallback_group_set_to_direct_is_moved_back(self):
        c = FakeClash(topo(Final={"now": "DIRECT"}), ALL_ALIVE - {"DIRECT"})  # 国外站直连不通
        (r,) = make(c).tick()
        self.assertTrue(r.recovered)
        self.assertEqual(c.selects(), [("select", "Final", "Proxies")])
        self.assertIn("Final 选了直连", r.cause)

    def test_never_picks_direct_reject_homebb_or_wrappers_around_them(self):
        groups = topo(Proxies={"now": "TW", "all": ["TW", "DIRECT", "REJECT", "Claude-Only", "Sneaky", "Wrapper"]},
                      Sneaky={"type": "Selector", "now": "Claude-Only", "all": ["Claude-Only"]},  # 其实走家宽
                      Wrapper={"type": "Selector", "now": "DIRECT", "all": ["DIRECT"]})  # 其实是直连
        c = FakeClash(groups, ALL_ALIVE - {"tw1", "tw2"})  # Sneaky / Wrapper 都能通，但都不许选
        (r,) = make(c).tick()
        self.assertFalse(r.recovered)
        self.assertEqual(c.selects(), [])
        for bad in ("Sneaky", "Wrapper"):  # 伪装成普通组的家宽 / 直连：连当候选测都不测
            self.assertNotIn(("delay", bad), c.calls)
        self.assertIn("安全出口都不通", r.summary())

    def test_runtime_homebb_members_are_excluded_even_without_whitelist(self):
        groups = topo(Proxies={"now": "TW", "all": ["TW", "RESIP-B"]})
        c = FakeClash(groups, ALL_ALIVE - {"tw1", "tw2"})  # RESIP-B 能通，但它是家宽
        r = daily(make(c, policy=dataclasses.replace(POLICY, homebb_members=())).tick())
        self.assertEqual(c.selects(), [])
        self.assertNotIn(("delay", "RESIP-B"), c.calls)  # 家宽链是好的，RESIP-B 根本不该被测，更不会当候选
        self.assertFalse(r.recovered)

    def test_missing_homebb_group_without_whitelist_stops_daily(self):
        groups = topo(Proxies={"now": "TW", "all": ["TW", "RESIP-B", "HK"]},
                      **{"Claude-Only": {"now": "RESIP-A", "all": ["RESIP-A"]}})
        del groups["RESIP-Claude"]  # 家宽组不见了（重载中 / 配置被改）
        c = FakeClash(groups, ALL_ALIVE - {"tw1", "tw2"})
        r = daily(make(c, policy=dataclasses.replace(POLICY, homebb_members=())).tick())
        self.assertIn("认不出哪些是家宽", r.cause)  # 又没写白名单：RESIP-B 是不是家宽认不出来，HK 能通也不动
        self.assertEqual(c.changes(), [])
        self.assertNotIn(("delay", "RESIP-B"), c.calls)
        c2 = FakeClash(groups, ALL_ALIVE - {"tw1", "tw2"})
        r2 = daily(make(c2).tick())  # 写了白名单：照样修，白名单里的家宽节点不碰
        self.assertTrue(r2.recovered)
        self.assertEqual(c2.selects(), [("select", "Proxies", "HK")])
        self.assertNotIn(("delay", "RESIP-B"), c2.calls)

    def test_direct_type_node_under_another_name_is_never_picked(self):
        groups = topo(Proxies={"now": "TW", "all": ["TW", "本地直连", "AutoWrap"]},
                      AutoWrap={"type": "URLTest", "now": "hk1", "all": ["hk1", "本地直连"], "fixed": ""})
        c = FakeClash(groups, ALL_ALIVE - {"tw1", "tw2"} | {"本地直连"}, types={"本地直连": "Direct"})
        (r,) = make(c).tick()
        self.assertEqual(c.selects(), [])  # 名字不叫 DIRECT，但类型是直连；混了它的自动组也不行
        self.assertNotIn(("delay", "本地直连"), c.calls)
        self.assertNotIn(("delay", "AutoWrap"), c.calls)
        self.assertFalse(r.recovered)

    def test_cycles_are_unsafe(self):
        groups = topo(Proxies={"now": "TW", "all": ["TW", "Loop1"]},
                      Loop1={"type": "Selector", "now": "Loop2", "all": ["Loop2"]},
                      Loop2={"type": "Selector", "now": "Loop1", "all": ["Loop1"]})
        c = FakeClash(groups, ALL_ALIVE - {"tw1", "tw2"})
        make(c).tick()
        self.assertNotIn(("delay", "Loop1"), c.calls)
        self.assertEqual(c.selects(), [])

    def test_too_deep_chain_is_left_alone(self):
        deep = {f"S{i}": {"type": "Selector", "now": f"S{i + 1}", "all": [f"S{i + 1}"]} for i in range(9)}
        deep["S9"] = {"type": "Selector", "now": "a1", "all": ["a1", "a2"]}
        c = FakeClash(topo(Final={"now": "S0", "all": ["S0", "DIRECT"]}, **deep), ALL_ALIVE - {"a1"})
        (r,) = make(c).tick()
        self.assertIn("看不全", r.cause)
        self.assertEqual(c.changes(), [])

    def test_auto_group_with_unknown_member_types_is_not_retested(self):
        c = FakeClash(topo(), ALL_ALIVE - {"a1"}, hidden={"a1", "a2"})  # 订阅节点类型拿不到
        (r,) = make(c).tick()
        self.assertNotIn(("retest", "机场A-自动"), c.calls)
        self.assertNotIn(("retest", "日常-自动"), c.calls)  # 它的成员里有类型不明的组，一样不碰
        self.assertEqual(c.selects(), [("select", "日常出口", "机场B-自动")])  # 在手动组里换到干净的那份
        self.assertTrue(r.recovered)

    def test_provider_types_make_subscription_nodes_trusted(self):
        c = FakeClash(topo(), ALL_ALIVE - {"a1"}, hidden={"a1", "a2"}, provider_types={"a1": "Vmess", "a2": "Vmess"})
        (r,) = make(c).tick()
        self.assertIn(("retest", "机场A-自动"), c.calls)
        self.assertTrue(r.recovered)
        self.assertEqual(c.selects(), [])

    def test_mixed_auto_group_is_not_retested(self):
        groups = topo(Proxies={"now": "Mixed", "all": ["Mixed", "日常出口"]},
                      Mixed={"type": "URLTest", "now": "m1", "all": ["m1", "DIRECT"], "fixed": ""})
        c = FakeClash(groups, ALL_ALIVE)  # m1 不通；重测的话 mihomo 会换到 DIRECT
        (r,) = make(c).tick()
        self.assertNotIn(("retest", "Mixed"), c.calls)
        self.assertEqual(c.selects(), [("select", "Proxies", "日常出口")])
        self.assertTrue(r.recovered)
        self.assertTrue(any("混有" in s for s in r.steps))

    def test_suspected_uplink_still_tries_safe_alternatives(self):
        # 直连检测地址和家宽都不通、当前节点也不通，但同订阅里还有活节点：不能直接认定「上行断了」放弃
        c = FakeClash(topo(), ALL_ALIVE - {"DIRECT", "RESIP-A", "RESIP-B", "a1"})
        r = daily(make(c).tick())
        self.assertTrue(r.recovered)
        self.assertEqual(c.g["机场A-自动"]["now"], "a2")
        self.assertNotIn("上行", r.summary())

    def test_real_uplink_loss_reports_and_switches_nothing(self):
        c = FakeClash(topo(), set())
        reps = make(c).tick()
        self.assertEqual(c.selects(), [])
        self.assertIn("疑似本机上行断了", daily(reps).summary())  # 本轮测出来的怀疑：进摘要，不进去重用的原因
        self.assertTrue(all(r.recovered is False for r in reps))

    def big_group(self, n: int = 20):
        return topo(Proxies={"now": "Big", "all": ["Big", "日常出口"]},
                    Big={"type": "Selector", "now": "n0", "all": [f"n{i}" for i in range(n)]})

    def test_group_is_scanned_to_the_end_before_moving_up(self):
        c = FakeClash(self.big_group(), ALL_ALIVE | {"n15"})  # 上一层的日常出口也能通，但 Big 里还有活的
        r = daily(make(c).tick())
        self.assertTrue(r.recovered)
        self.assertEqual(c.selects(), [("select", "Big", "n15")])

    def test_out_of_budget_waits_for_next_round_instead_of_moving_up(self):
        clock = Clock()
        c = FakeClash(self.big_group(), ALL_ALIVE | {"n15"})
        c.on_probe = lambda n: setattr(clock, "t", clock.t + 1) if n.startswith("n") else None  # 测一个 1 秒
        guard = make(c, clock=clock, scan_budget=5)
        r1 = daily(guard.tick())
        self.assertFalse(r1.recovered)
        self.assertEqual(c.selects(), [])  # 没证明 Big 整组都不通，不跳到日常出口
        self.assertIn("Big 里还有候选没测完", r1.blocked)
        self.assertNotIn(("delay", "n15"), c.calls)
        r2 = daily(guard.tick())
        self.assertTrue(r2.recovered)
        self.assertEqual(c.selects(), [("select", "Big", "n15")])

    def test_upper_layer_gets_a_batch_right_after_lower_layer_is_proven_dead(self):
        clock = Clock()
        tw = [f"tw{i}" for i in range(40)]  # 39 个候选 = 4 批：扫完就超了预算
        c = FakeClash(topo(Proxies={"now": "TW"}, TW={"now": "tw0", "all": tw}), ALL_ALIVE - {"tw1", "tw2"})
        firsts = {tw[i] for i in range(1, 40, G.BATCH)}
        c.on_probe = lambda n: setattr(clock, "t", clock.t + 3) if n in firsts else None  # 一批死节点约 3 秒
        (r,) = make(c, clock=clock).tick()
        self.assertTrue(r.recovered)  # 同一轮就往上换：不会每轮重扫台湾、上一层永远轮不到
        self.assertEqual(c.selects(), [("select", "Proxies", "日常出口")])

    def test_upper_layer_is_not_started_while_lower_layer_is_unfinished(self):
        clock = Clock()
        c = FakeClash(self.big_group(), ALL_ALIVE)
        c.on_probe = lambda n: setattr(clock, "t", clock.t + 1) if n.startswith("n") else None
        (r,) = make(c, clock=clock, scan_budget=5).tick()
        self.assertEqual(c.selects(), [])
        self.assertNotIn(("delay", "日常出口"), c.calls)  # Big 还没测完：不能开始上一层
        self.assertIn("Big 里还有候选没测完", r.blocked)

    def test_new_candidates_between_rounds_are_not_skipped(self):
        clock = Clock()
        c = FakeClash(self.big_group(), ALL_ALIVE)
        c.on_probe = lambda n: setattr(clock, "t", clock.t + 1) if n.startswith("n") else None
        guard = make(c, clock=clock, scan_budget=5)
        daily(guard.tick())  # 第一轮测了前 12 个，都不通
        c.g["Big"]["all"].insert(1, "新节点")  # 订阅更新，前面插进一个活节点
        c.alive.add("新节点")
        r2 = daily(guard.tick())
        self.assertTrue(r2.recovered)
        self.assertEqual(c.selects(), [("select", "Big", "新节点")])

    def test_tested_candidates_are_forgotten_once_the_path_recovers(self):
        clock = Clock()
        c = FakeClash(self.big_group(), ALL_ALIVE)
        c.on_probe = lambda n: setattr(clock, "t", clock.t + 1) if n.startswith("n") else None
        guard = make(c, clock=clock, scan_budget=5)
        daily(guard.tick())  # 第一次故障：n1..n12 测过都不通，时间用完
        c.alive.add("n0")  # 当前节点自己好了，这次故障结束
        self.assertEqual(guard.tick(), [])
        c.alive.discard("n0")
        c.alive.add("n3")  # 过一阵又挂了；上次测过不通的 n3 现在能通
        r = daily(guard.tick())
        self.assertTrue(r.recovered)
        self.assertEqual(c.selects(), [("select", "Big", "n3")])  # 不拿上次故障的结论跳过它，更不往上一层跳

    def test_probe_errors_are_not_counted_as_dead(self):
        c = FakeClash(topo(Proxies={"now": "TW"}), ALL_ALIVE - {"tw1"}, unknown={"tw2"})
        (r,) = make(c).tick()
        self.assertEqual(c.selects(), [])  # 判断不了 tw2，就不能说 TW 整组都不通、往上一层换
        self.assertIn("TW 里还有候选没测完", r.blocked)
        self.assertTrue(any("接口出错" in s for s in r.steps))

    def test_probe_error_on_current_node_is_not_reported_as_dead(self):
        c = FakeClash(topo(), ALL_ALIVE - {"a1"}, unknown={"a1"})
        (r,) = make(c).tick()
        self.assertIn("当前节点 a1 测速接口出错", r.cause)
        self.assertNotIn("a1 不通", r.cause)
        self.assertTrue(r.recovered)  # 自动组照样重测换走

    def test_target_probe_error_never_triggers_changes(self):
        c = FakeClash(topo(Proxies={"now": "TW"}), ALL_ALIVE, unknown={"Final"})  # 网是好的，只是测 Final 的接口出错
        (r,) = make(c).tick()
        self.assertEqual(c.changes(), [])
        self.assertTrue(r.undecided)
        self.assertFalse(r.summary().startswith("日常流量不通"))
        self.assertIn("判断不了", r.summary())

    def test_permission_probe_error_means_no_change(self):
        c = FakeClash(topo(Proxies={"now": "TW"}), ALL_ALIVE - {"tw1"})
        seen = {"n": 0}

        def later(name):
            if name == "Final":
                seen["n"] += 1
                if seen["n"] == 3:  # 前两次确认不通；动手前的复测开始，接口就出错了
                    c.unknown.add("Final")
        c.on_probe = later
        (r,) = make(c).tick()
        self.assertEqual(c.selects(), [])
        self.assertTrue(r.undecided)
        self.assertIn("测速接口出错", r.blocked)

    def test_stale_permission_is_not_used(self):
        clock = Clock()
        c = FakeClash(topo(Proxies={"now": "TW", "all": ["TW", "HK"]}), ALL_ALIVE - {"tw1", "tw2"})

        def slow(name):
            if name == "HK":  # 测上一层的候选花了 5 秒，这期间 tw1 自己好了
                clock.t += 5
                c.alive.add("tw1")
        c.on_probe = slow
        (r,) = make(c, clock=clock).tick()
        self.assertEqual(c.selects(), [])  # 诊断开始时那次「不通」已经过期，不能拿来当动手许可
        self.assertIn("目标已自行恢复", r.blocked)
        self.assertTrue(r.recovered)

    def test_permission_expires_and_keeps_only_the_newest_observation(self):
        clock = Clock()
        c = FakeClash(topo(Proxies={"now": "TW"}), ALL_ALIVE - {"tw1"})
        guard = make(c, clock=clock)
        with G.Confirm(guard, "Final", lambda: True) as confirm:
            self.assertEqual(confirm.verdict(), "down")
            c.alive.add("tw1")  # 目标自己好了
            self.assertEqual(confirm.verdict(), "down")  # 刚测完、还在有效期（timeout 秒）内：沿用
            clock.t += guard.cfg.timeout + 1
            self.assertEqual(confirm.verdict(), "up")  # 过期了就重测
            confirm.saw(clock.t - 60, None)  # 更早开测的观察晚到了：不能盖掉新的
            self.assertEqual(confirm.verdict(), "up")
        with G.Confirm(guard, "Final", lambda: False) as confirm:
            self.assertEqual(confirm.verdict(), "moved")

    def test_permission_is_rechecked_when_reading_rules_takes_long(self):
        clock = Clock()
        c = FakeClash(topo(Proxies={"now": "TW"}), ALL_ALIVE - {"tw1"})
        guard = make(c, clock=clock)
        reads = {"n": 0}

        def same():
            reads["n"] += 1
            if reads["n"] == 1:  # 规则表很大，第一次读了好几秒；这期间目标自己好了
                clock.t += guard.cfg.timeout + 2
                c.alive.add("tw1")
            return True
        with G.Confirm(guard, "Final", same) as confirm:
            confirm.fut.result()  # 开头那次先测完（不通）
            self.assertEqual(confirm.verdict(), "up")  # 读完规则观察已经过期：重测，看到已经好了

    def test_upstream_change_after_own_action_stops(self):
        class Racing(FakeClash):
            def retest(self, group, url):
                out = super().retest(group, url)
                if group == "机场A-自动":
                    self.g["Proxies"]["now"] = "TW"  # 守护重测的同时，人把 Proxies 换到了台湾（也不通）
                return out

        c = Racing(topo(), ALL_ALIVE - {"a1", "a2", "tw1", "tw2"})
        (r,) = make(c).tick()
        self.assertIn(("retest", "机场A-自动"), c.calls)
        self.assertEqual(c.selects(), [])  # 上游是人改的：不认领，更不把 Proxies 改回日常出口
        self.assertNotIn(("retest", "日常-自动"), c.calls)
        self.assertIn("链路刚被别人改过", r.blocked)

    def test_tested_candidates_are_forgotten_when_handling_ends_recovered(self):
        clock = Clock()
        c = FakeClash(self.big_group(), ALL_ALIVE)

        def probe(name):
            if name.startswith("n"):
                clock.t += 1
            if name == "n12":
                c.alive.add("n0")  # 扫描快结束时，当前节点自己好了
        c.on_probe = probe
        guard = make(c, clock=clock, scan_budget=5)
        self.assertTrue(daily(guard.tick()).recovered)  # 处理末尾确认通了：这次故障结束
        c.on_probe = lambda name: None
        c.alive.discard("n0")
        c.alive.add("n3")  # 下一轮检查前又挂了；上次测过不通的 n3 现在能通
        r = daily(guard.tick())
        self.assertTrue(r.recovered)
        self.assertEqual(c.selects(), [("select", "Big", "n3")])

    def test_daily_never_touches_groups_on_the_ai_chain(self):
        groups = topo(**{"Claude-Only": {"now": "AI-Mid", "all": ["AI-Mid"]},
                         "AI-Mid": {"type": "Selector", "now": "RESIP-Claude", "all": ["RESIP-Claude", "a2"]},
                         "Final": {"now": "AI-Mid", "all": ["AI-Mid", "Proxies", "DIRECT"]}})
        c = FakeClash(groups, ALL_ALIVE - {"RESIP-A", "RESIP-B"})  # 日常也经过 AI 链上的中间组；家宽全挂
        make(c).tick()
        self.assertNotIn(("select", "AI-Mid", "a2"), c.selects())  # 改它就把 AI 带去了机场
        self.assertEqual(G.W.follow_chain(c.proxies(), "Claude-Only")[:3], ("Claude-Only", "AI-Mid", "RESIP-Claude"))

    def test_match_change_while_waiting_for_a_fresh_probe_stops(self):
        clock = Clock()
        c = FakeClash(topo(Proxies={"now": "TW"}, Final2={"type": "Selector", "now": "HK", "all": ["HK"]}),
                      ALL_ALIVE - {"tw1"})
        armed = {"on": False}

        def hook(name):
            if name == "tw2":  # 挑候选花了一阵，手上的目标观察过期了
                time.sleep(0.3)  # 让开扫时后台那次复测先测完
                clock.t += 10
                armed["on"] = True
            elif name == "Final" and armed["on"]:
                c.match = "Final2"  # 动手许可重测目标的这几秒里，规则重载了
        c.on_probe = hook
        (r,) = make(c, clock=clock).tick()
        self.assertEqual(c.selects(), [])  # 规则放在所有等待之后才读：看得见这次变化
        self.assertIn("兜底规则刚变了", r.blocked)

    def test_actions_survive_a_later_controller_error(self):
        class Boom(FakeClash):
            def proxies(self):
                if any(x[0] == "retest" for x in self.calls):
                    raise RuntimeError("/proxies HTTP 500")  # 重测完，再读状态就出错了
                return super().proxies()

        c = Boom(topo(), ALL_ALIVE - {"a1"})
        (r,) = make(c).tick()
        self.assertEqual(c.g["机场A-自动"]["now"], "a2")
        self.assertTrue(r.undecided)
        self.assertIn("控制接口出错", r.blocked)
        self.assertTrue(any("重测" in a for a in r.actions))  # 内核已经被改了：动作要留在报告里、照样通知

    def test_dry_run_lists_only_the_first_step(self):
        c = FakeClash(topo(Proxies={"now": "TW"}), ALL_ALIVE - {"tw1"})
        (dry,) = make(c, dry_run=True).tick()
        c2 = FakeClash(topo(Proxies={"now": "TW"}), ALL_ALIVE - {"tw1"})
        (real,) = make(c2).tick()
        self.assertEqual(len(dry.actions), 1)  # 真跑时第一步就修好了：演练不能列出真跑不会做的动作
        self.assertEqual(real.actions, [dry.actions[0].replace("（演练，未执行）", "")])

    def test_unknown_group_type_with_members_is_checked_like_a_group(self):
        groups = topo(Proxies={"now": "TW", "all": ["TW", "SmartG"]},
                      SmartG={"type": "Smart", "now": "RESIP-A", "all": ["RESIP-A", "DIRECT"]})  # 分支内核的新组类型
        c = FakeClash(groups, ALL_ALIVE - {"tw1", "tw2"})
        make(c).tick()
        self.assertNotIn(("select", "Proxies", "SmartG"), c.selects())  # 有成员就当组：成员里有家宽 / 直连，不能选

    def test_match_changed_between_check_and_handling_waits_for_next_round(self):
        c = FakeClash(topo(Proxies={"now": "TW"}, Final2={"type": "Selector", "now": "HK", "all": ["HK"]}),
                      ALL_ALIVE - {"tw1"})
        real = c.match_target
        c.match_target = lambda fresh=False: "Final2" if fresh else real(fresh)  # 检查用的缓存还是旧的
        (r,) = make(c).tick()
        self.assertEqual(c.changes(), [])
        self.assertTrue(r.undecided)
        self.assertIn("兜底规则刚变了", r.cause)

    def test_daily_outage_does_not_probe_ai_chain_when_uplink_is_fine(self):
        c = FakeClash(topo(), ALL_ALIVE - {"a1"})
        make(c).tick()
        self.assertEqual(c.calls.count(("delay", "Claude-Only")), 1)  # 只有家宽那条自己的检查；直连通就不经家宽再测

    def test_pinned_auto_group_is_left_alone_without_unfix_auto(self):
        c = FakeClash(topo(**{"机场A-自动": {"fixed": "a1"}}), ALL_ALIVE - {"a1"})
        (r,) = make(c, unfix_auto=False).tick()
        self.assertNotIn(("retest", "机场A-自动"), c.calls)  # 重测会顺带解开固定：没开自动解固定就不替它重测
        self.assertNotIn(("unfix", "机场A-自动"), c.calls)
        self.assertTrue(any("没开自动解固定" in s and "往上一层找" in s for s in r.steps))  # 层开头就跳过，不白等动手许可

    def test_failed_select_does_not_escalate(self):
        class Flaky(FakeClash):
            def select(self, group, name):
                if group == "TW":
                    raise RuntimeError("/proxies/TW HTTP 500")
                super().select(group, name)

        c = Flaky(topo(Proxies={"now": "TW"}), ALL_ALIVE - {"tw1"})
        (r,) = make(c).tick()
        self.assertEqual(c.selects(), [])  # TW 改选失败：不能顺手把 Proxies 改到日常出口
        self.assertIn("控制接口出错", r.blocked)

    def test_group_joining_the_ai_chain_during_scan_is_left_alone(self):
        variants = {
            "AI 组本来就能选 Mid": {"Claude-Only": {"now": "RESIP-Claude", "all": ["RESIP-Claude", "Mid"]}},
            "AI 组被改成能选 Mid": {},
        }
        for name, over in variants.items():
            with self.subTest(name):
                groups = topo(Mid={"type": "Selector", "now": "RESIP-Claude", "all": ["RESIP-Claude", "a2"]},
                              Final={"now": "Mid", "all": ["Mid", "DIRECT"]}, **over)
                c = FakeClash(groups, ALL_ALIVE - {"RESIP-A", "RESIP-B"})

                def hook(n, c=c):
                    if n == "a2":  # 测 Mid 的候选时，AI 组被人切成经过 Mid（仍走家宽，本身不漏）
                        c.g["Claude-Only"]["all"] = ["RESIP-Claude", "Mid"]
                        c.g["Claude-Only"]["now"] = "Mid"
                c.on_probe = hook
                make(c).tick()
                self.assertNotIn(("select", "Mid", "a2"), c.selects())
                self.assertNotIn("a2", G.W.follow_chain(c.proxies(), "Claude-Only"))

    def test_daily_never_touches_backup_members_of_ai_fallback(self):
        groups = topo(**{"Claude-Only": {"now": "Y", "all": ["Y"]},
                         "Y": {"type": "Fallback", "now": "RESIP-Claude", "all": ["RESIP-Claude", "Z"], "fixed": ""},
                         "Z": {"type": "Selector", "now": "RESIP-Claude", "all": ["RESIP-Claude", "a2"]},
                         "Final": {"now": "Z", "all": ["Z", "DIRECT"]}})
        c = FakeClash(groups, ALL_ALIVE - {"RESIP-A", "RESIP-B"})  # 家宽全挂：AI 应该保持断开
        make(c).tick()
        self.assertNotIn(("select", "Z", "a2"), c.selects())  # Z 是 AI 的 fallback 备用成员：改它，AI 就跟着走了
        c.retest("Y", "u")  # mihomo 之后对 Y 的例行健康检查
        self.assertNotIn("a2", G.W.follow_chain(c.proxies(), "Claude-Only"))

    def test_pin_during_permission_wait_is_respected(self):
        for unfix_auto in (False, True):
            with self.subTest(unfix_auto=unfix_auto):
                c = FakeClash(topo(), ALL_ALIVE - {"a1"})
                real = G.Confirm.verdict

                def slow(confirm, c=c):
                    c.g["机场A-自动"]["fixed"] = "a1"  # 等许可的这几秒，人在 Verge 里点了一下当前节点（钉住）
                    return real(confirm)
                with mock.patch.object(G.Confirm, "verdict", slow):
                    (r,) = make(c, unfix_auto=unfix_auto).tick()
                self.assertNotIn(("unfix", "机场A-自动"), c.changes())
                self.assertNotIn(("retest", "机场A-自动"), c.changes())  # 重测也会顺带解开
                self.assertIn("链路刚被别人改过", r.blocked)

    def test_heal_auto_never_unfixes_without_unfix_auto(self):
        c = FakeClash(topo(**{"机场A-自动": {"fixed": "a1"}}), ALL_ALIVE - {"a1"})
        guard = make(c, unfix_auto=False)
        rep = G.Report(G.DAILY)
        self.assertFalse(guard.heal_auto("机场A-自动", guard.view(), rep))  # 第二道闸：调用方漏了也不解、不重测
        self.assertEqual(c.changes(), [])

    def test_subscription_nodes_are_probed_through_their_provider(self):
        groups = topo(Proxies={"now": "TW"}, TW={"now": "a1", "all": ["a1", "a2"]})
        c = FakeClash(groups, ALL_ALIVE - {"a1"}, hidden={"a1", "a2"}, provider_types={"a1": "Vmess", "a2": "Vmess"})
        (r,) = make(c).tick()
        self.assertTrue(r.recovered)
        self.assertEqual(c.selects(), [("select", "TW", "a2")])
        self.assertEqual(c.hints["a2"], "sub")  # 知道在哪个订阅，直接走订阅的测速接口

    def test_retest_api_error_is_not_counted_as_done(self):
        c = FakeClash(topo(), ALL_ALIVE - {"a1"}, retest_error="接口出错：boom")
        guard = make(c)
        r = daily(guard.tick())
        self.assertTrue(any("重测没做成" in s for s in r.steps))
        self.assertFalse(guard.cooling("机场A-自动"))  # 没测成就不进冷却，下一轮还会再试
        self.assertEqual(c.selects(), [])  # 接口出错不是「这层修不好」：不能因此往上一层改

    def test_candidate_turning_unsafe_during_probe_is_not_selected(self):
        groups = topo(Proxies={"now": "TW", "all": ["TW", "HK"]}, HK={"all": ["hk1", "DIRECT"]})
        c = FakeClash(groups, ALL_ALIVE - {"tw1", "tw2"})

        def flip(name):
            if name == "HK":
                c.g["HK"]["now"] = "DIRECT"  # 测它的那几秒里，被人切成了直连
        c.on_probe = flip
        (r,) = make(c).tick()
        self.assertEqual(c.selects(), [])
        self.assertIn("链路刚被别人改过", r.blocked)

    def test_candidate_joining_homebb_group_during_probe_is_not_selected(self):
        c = FakeClash(topo(Proxies={"now": "TW", "all": ["TW", "HK"]}), ALL_ALIVE - {"tw1", "tw2"})

        def join(name):
            if name == "HK":
                c.g["RESIP-Claude"]["all"].append("hk1")  # 测它的那几秒里，hk1 被加进了家宽组
        c.on_probe = join
        (r,) = make(c).tick()
        self.assertEqual(c.selects(), [])  # 按最新状态重算家宽排除范围，HK 已经会走到家宽
        self.assertIn("链路刚被别人改过", r.blocked)

    def test_candidate_changed_while_waiting_for_permission_is_not_selected(self):
        c = FakeClash(topo(Proxies={"now": "TW", "all": ["TW", "HK"]}, HK={"all": ["hk1", "DIRECT"]}),
                      ALL_ALIVE - {"tw1", "tw2"})
        real = G.Confirm.verdict

        def slow(confirm):
            c.g["HK"]["now"] = "DIRECT"  # 等后台复测结果的那几秒里，HK 被人切成了直连
            return real(confirm)
        with mock.patch.object(G.Confirm, "verdict", slow):
            (r,) = make(c).tick()
        self.assertEqual(c.selects(), [])  # 拿到许可之后才读最新状态复核，这个变化看得见
        self.assertIn("链路刚被别人改过", r.blocked)

    def test_auto_group_changed_while_waiting_for_permission_is_not_retested(self):
        c = FakeClash(topo(), ALL_ALIVE - {"a1"})
        real = G.Confirm.verdict

        def slow(confirm):
            if "DIRECT" not in c.g["机场A-自动"]["all"]:
                c.g["机场A-自动"]["all"].append("DIRECT")  # 等许可的时候，自动组里被加进了直连
            return real(confirm)
        with mock.patch.object(G.Confirm, "verdict", slow):
            (r,) = make(c).tick()
        self.assertEqual(c.changes(), [])
        self.assertIn("链路刚被别人改过", r.blocked)

    def test_match_target_change_before_first_change_stops(self):
        c = FakeClash(topo(Proxies={"now": "TW"}, Final2={"type": "Selector", "now": "HK", "all": ["HK"]}),
                      ALL_ALIVE - {"tw1"})
        reads = {"n": 0}
        real = c.match_target

        def match_target(fresh=False):
            if fresh:
                reads["n"] += 1
                if reads["n"] > 1:
                    return "Final2"  # 处理过程中规则重载了，兜底换成了 Final2
            return real(fresh)
        c.match_target = match_target
        (r,) = make(c).tick()
        self.assertEqual(c.selects(), [])
        self.assertIn("兜底规则刚变了", r.blocked)
        self.assertTrue(r.undecided)  # 规则刚变：这轮不下结论，更不拿旧目标报「仍不通」
        self.assertIsNone(r.recovered)

    def test_user_switch_during_probe_is_respected(self):
        c = FakeClash(topo(Proxies={"now": "TW"}), ALL_ALIVE - {"tw1"})

        def flip(name):
            if name == "tw2" and c.g["TW"]["now"] == "tw1":
                c.g["TW"]["now"] = "tw2"  # 测候选的时候，人手动换了
        c.on_probe = flip
        (r,) = make(c).tick()
        self.assertEqual(c.selects(), [])
        self.assertTrue(set(r.blocked) & {"链路刚被别人改过", "目标已自行恢复"})
        self.assertTrue(r.recovered)

    def test_user_fix_during_diagnosis_stops_all_changes(self):
        class Racing(FakeClash):
            def proxies(self):
                out = super().proxies()
                if not getattr(self, "flipped", False):
                    self.flipped = True
                    self.g["Proxies"]["now"] = "HK"  # 诊断刚读完状态，人就把 Proxies 换到 HK 了
                return out

        c = Racing(topo(**{"机场A-自动": {"fixed": "a1"}}), ALL_ALIVE - {"a1"})
        (r,) = make(c).tick()
        self.assertEqual(c.changes(), [])  # 不解固定、不重测、不切换
        self.assertIn("链路刚被别人改过", r.blocked)
        self.assertTrue(r.recovered)

    def test_cooldown_stops_flip_flop_but_still_escalates(self):
        clock = Clock()
        c = FakeClash(topo(Proxies={"now": "TW"}), ALL_ALIVE - {"tw1"})
        guard = make(c, clock=clock)
        guard.tick()
        self.assertIn(("select", "TW", "tw2"), c.calls)
        c.alive.discard("tw2")
        c.alive.add("tw1")
        clock.t += 10  # 冷却期内：TW 不许来回切，但网不能断 → 往上一层换到日常出口
        r = guard.handle_daily()
        self.assertNotIn(("select", "TW", "tw1"), c.calls)
        self.assertIn(("select", "Proxies", "日常出口"), c.calls)
        self.assertTrue(r.recovered)
        c.g["Proxies"]["now"] = "TW"  # 有人又手动选回了台湾
        clock.t += 60  # 冷却过了，TW 可以再动
        guard.handle_daily()
        self.assertIn(("select", "TW", "tw1"), c.calls)

    def test_dry_run_changes_nothing(self):
        c = FakeClash(topo(Proxies={"now": "TW"}, **{"机场A-自动": {"fixed": "a1"}}), ALL_ALIVE - {"tw1", "tw2"})
        (r,) = make(c, dry_run=True).tick()
        self.assertEqual(c.changes(), [])
        self.assertTrue(r.actions and all(a.startswith("（演练") for a in r.actions))
        self.assertIsNone(r.recovered)


class HomebbTest(unittest.TestCase):
    def test_switches_only_inside_homebb_group(self):
        c = FakeClash(topo(), ALL_ALIVE - {"RESIP-A"})
        (r,) = make(c).tick()
        self.assertEqual(r.path, G.HOMEBB)
        self.assertTrue(r.recovered)
        self.assertEqual(c.g["RESIP-Claude"]["now"], "RESIP-B")
        self.assertNotIn("Claude-Only", [x[1] for x in c.changes()])

    def test_all_homebb_dead_stays_closed(self):
        c = FakeClash(topo(), ALL_ALIVE - {"RESIP-A", "RESIP-B"})
        r = homebb(make(c).tick())
        self.assertFalse(r.recovered)
        self.assertIn("家宽节点都不通", r.summary())
        self.assertIn("不回落", r.summary())
        self.assertEqual(c.selects(), [])  # 没把 AI 切去任何地方

    def test_alive_member_but_blocked_is_reported_precisely(self):
        c = FakeClash(topo(), ALL_ALIVE - {"RESIP-A"})
        guard = make(c)
        guard.touched["RESIP-Claude"] = guard.now()  # 家宽组还在冷却
        r = homebb(guard.tick())
        self.assertFalse(r.recovered)
        self.assertIn("RESIP-B 能通", r.summary())
        self.assertIn("冷却中", r.summary())
        self.assertNotIn("都不通", r.summary())

    def test_probe_error_is_not_reported_as_all_dead(self):
        c = FakeClash(topo(), ALL_ALIVE - {"RESIP-A", "RESIP-B"}, unknown={"RESIP-B"})
        r = homebb(make(c).tick())
        self.assertIn("测速接口出错", r.summary())
        self.assertNotIn("都不通", r.summary())

    def test_selector_homebb_group_picks_its_own_members_only(self):
        groups = topo(**{"RESIP-Claude": {"type": "Selector", "now": "RESIP-A", "all": ["RESIP-A", "RESIP-B"]}})
        c = FakeClash(groups, ALL_ALIVE - {"RESIP-A"})
        (r,) = make(c).tick()
        self.assertTrue(r.recovered)
        self.assertEqual(c.selects(), [("select", "RESIP-Claude", "RESIP-B")])

    def test_untrusted_member_in_homebb_group_is_left_alone(self):
        for intruder in ("机场节点X", "DIRECT"):
            with self.subTest(intruder=intruder):
                groups = topo(**{"RESIP-Claude": {"all": ["RESIP-A", "RESIP-B", intruder]}})
                c = FakeClash(groups, (ALL_ALIVE - {"RESIP-A", "RESIP-B"}) | {"机场节点X"})
                r = homebb(make(c).tick())
                self.assertIn("成员不对", r.cause)
                self.assertIn(intruder, r.cause)
                self.assertEqual(c.changes(), [])  # 不重测、不切换
                self.assertFalse(r.recovered)

    def test_whitelisted_name_that_is_really_a_group_is_left_alone(self):
        for kind in ("Selector", "Smart"):  # Smart：分支内核的新组类型，不认识也照样当组
            with self.subTest(kind=kind):
                groups = topo(**{"RESIP-B": {"type": kind, "now": "a1", "all": ["a1"]}})  # 名字在白名单，其实套着机场节点
                c = FakeClash(groups, ALL_ALIVE - {"RESIP-A"})
                r = homebb(make(c).tick())
                self.assertIn("成员不对", r.cause)
                self.assertIn("RESIP-B", r.cause)
                self.assertEqual(c.changes(), [])  # 不重测：重测会让家宽组换到 RESIP-B，AI 就走了机场
                self.assertFalse(r.recovered)

    def test_empty_whitelist_means_report_only(self):
        groups = topo(**{"RESIP-Claude": {"all": ["RESIP-A", "机场节点X"]}})
        c = FakeClash(groups, (ALL_ALIVE - {"RESIP-A"}) | {"机场节点X"})
        r = homebb(make(c, policy=dataclasses.replace(POLICY, homebb_members=())).tick())
        self.assertIn("homebb_members", r.cause)
        self.assertEqual(c.changes(), [])
        self.assertFalse(r.recovered)

    def test_ai_group_not_on_homebb_group_is_left_alone(self):
        c = FakeClash(topo(**{"Claude-Only": {"now": "a1", "all": ["RESIP-Claude", "a1"]}}), ALL_ALIVE - {"a1"})
        r = homebb(make(c).tick())
        self.assertIn("死链结构被改", r.cause)
        self.assertEqual(c.selects(), [])


class RobustnessTest(unittest.TestCase):
    def test_one_chain_crashing_does_not_stop_the_other(self):
        c = FakeClash(topo(), ALL_ALIVE - {"a1", "a2", "b1", "b2", "RESIP-A"})
        guard = make(c)
        with mock.patch.object(guard, "handle_daily", side_effect=RuntimeError("炸了")):
            reps = guard.tick()
        self.assertIn("出错", daily(reps).cause)
        self.assertTrue(homebb(reps).recovered)

    def test_rules_api_failure_does_not_block_homebb(self):
        c = FakeClash(topo(), ALL_ALIVE - {"RESIP-A"})
        c.match_error = RuntimeError("/rules HTTP 500")
        reps = make(c).tick()
        self.assertIn("出错", daily(reps).cause)
        self.assertTrue(homebb(reps).recovered)  # 家宽照样检查、照样修

    def test_explain_keeps_going_when_rules_api_fails(self):
        c = FakeClash(topo(), ALL_ALIVE)
        c.match_error = RuntimeError("/rules HTTP 500")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = G.explain_all(make(c, dry_run=True))
        self.assertIn("日常流量：查不了", out.getvalue())
        self.assertIn("家宽：通", out.getvalue())
        self.assertEqual(rc, 1)

    def test_second_guard_reports_busy_instead_of_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "guard.lock"
            with open(lock, "a") as held:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                c = FakeClash(topo(Proxies={"now": "TW"}), ALL_ALIVE - {"tw1"})
                guard = make(c, lock_path=lock)
                reps = guard.tick()
                self.assertEqual(c.changes(), [])
                self.assertTrue(daily(reps).undecided)
                self.assertEqual(G.exit_code(reps, dry_run=False), 1)  # 没确认恢复，不能说成功
            (r,) = guard.tick()  # 锁放开以后正常处理
            self.assertTrue(r.recovered)

    def test_exit_codes(self):
        ok, bad, unknown = (G.Report(G.DAILY, recovered=v) for v in (True, False, None))
        busy = G.Report(G.DAILY, recovered=True, undecided=True, down=False)
        self.assertEqual(G.exit_code([], False), 0)
        self.assertEqual(G.exit_code([ok], False), 0)
        self.assertEqual(G.exit_code([ok, bad], False), 1)
        self.assertEqual(G.exit_code([unknown], False), 1)  # 没确认恢复就不算成功
        self.assertEqual(G.exit_code([busy], False), 1)
        self.assertEqual(G.exit_code([], True), 0)
        self.assertEqual(G.exit_code([unknown], True), 1)

    def test_status_parsing_matches_watch_request_errors(self):
        self.assertEqual(G._status(RuntimeError("/proxies/a/delay?url=x HTTP 504")), 504)
        self.assertEqual(G._status(RuntimeError("/group/%E6/delay HTTP 404")), 404)
        self.assertIsNone(G._status(RuntimeError("connection refused")))

    def test_provider_hint_goes_straight_to_healthcheck(self):
        seen = []

        def api(path, timeout=5.0, method="GET", body=None):
            seen.append(path)
            return {"delay": 88}
        with mock.patch.object(G.W, "api_json", api):
            self.assertEqual(G.Clash(3).probe("节点 1", "http://x/204", provider="sub a"), 88)
        self.assertEqual(len(seen), 1)  # 不先按名字测一次（404），也不再拉整份订阅列表
        self.assertTrue(seen[0].startswith("/providers/proxies/sub%20a/%E8%8A%82%E7%82%B9%201/healthcheck?"))


class NoCoreTest(unittest.TestCase):
    def run_ticks(self, guard, pgrep_code: int, clock: Clock, steps: list[float]) -> list[list]:
        seen = []
        for dt in steps:
            clock.t += dt
            ran = []

            def fake_run(cmd, **kw):
                ran.append(cmd)
                return mock.Mock(returncode=pgrep_code if cmd[0] == "pgrep" else 0, stderr="")

            with mock.patch.object(G.subprocess, "run", fake_run):
                guard.tick()
            seen.append(ran)
        return seen

    def test_reopens_verge_only_when_not_running_and_backs_off(self):
        clock = Clock()
        guard = make(FakeClash(topo(), ALL_ALIVE, up=False), clock=clock)
        opened = [["open", "-a", "Clash Verge"] in ran for ran in self.run_ticks(guard, 1, clock, [0, 10, 60])]
        self.assertEqual(opened, [True, False, True])  # 打开后至少隔 60 秒才再试

    def test_does_not_open_when_verge_is_running(self):
        clock = Clock()
        guard = make(FakeClash(topo(), ALL_ALIVE, up=False), clock=clock)
        (ran,) = self.run_ticks(guard, 0, clock, [0])
        self.assertNotIn(["open", "-a", "Clash Verge"], ran)
        self.assertEqual(guard.last_checked, {G.CORE})


class AnnouncerTest(unittest.TestCase):
    def setUp(self):
        self.logs, self.notes = [], []
        p1 = mock.patch.object(G.W, "log_line", self.logs.append)
        p2 = mock.patch.object(G.W, "notify_macos", lambda title, body, modal=False: self.notes.append(body))
        p1.start(), p2.start()
        self.addCleanup(p1.stop), self.addCleanup(p2.stop)
        self.a = G.Announcer(GuardCfg())

    def test_same_outage_is_announced_once_then_recovery_once(self):
        self.a([G.Report(G.DAILY, cause="当前节点 x 不通", recovered=False)], EVERYTHING)
        self.a([G.Report(G.DAILY, cause="当前节点 x 不通", recovered=False)], EVERYTHING)
        self.assertEqual(len(self.notes), 1)
        self.a([], EVERYTHING)
        self.a([], EVERYTHING)
        self.assertEqual(self.notes[-1], "日常流量已恢复")
        self.assertEqual(sum("已恢复" in n for n in self.notes), 1)

    def test_fixed_in_same_round_is_announced_and_not_repeated_as_recovery(self):
        self.a([G.Report(G.DAILY, cause="x", actions=["TW：tw1（不通）→ tw2"], recovered=True)], EVERYTHING)
        self.a([], EVERYTHING)
        self.assertEqual(len(self.notes), 1)
        self.assertIn("已恢复", self.notes[0])

    def test_self_recovered_blip_is_logged_but_not_notified(self):
        self.a([G.Report(G.DAILY, cause="当前节点 x 不通", recovered=True)], EVERYTHING)
        self.assertEqual(self.notes, [])  # 守护没动手、之前也没报过：只记日志
        self.assertTrue(any("已恢复" in line for line in self.logs))
        self.a([G.Report(G.DAILY, cause="当前节点 x 不通", recovered=False)], EVERYTHING)
        self.a([G.Report(G.DAILY, cause="当前节点 x 不通", recovered=True)], EVERYTHING)  # 报过不通的，好了要说一声
        self.assertEqual(len(self.notes), 2)
        self.assertIn("已恢复", self.notes[-1])

    def test_persistent_undecided_alerts_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "guard.lock"
            lock.write_text("")
            lock.chmod(0o444)  # 比如之前有人 sudo 跑过 --once，锁文件成了 root 的
            c = FakeClash(topo(Proxies={"now": "TW"}), ALL_ALIVE - {"tw1"})  # 日常断了、本来一换就好
            clock = Clock()
            guard = make(c, clock=clock, lock_path=lock)
            a = G.Announcer(GuardCfg(), now=clock)
            for _ in range(60):  # 5 分钟
                a(guard.tick(), guard.last_checked)
                clock.t += 5
        self.assertEqual(len(self.notes), 1)  # 守护自己一直出错：提醒一次，不刷屏
        self.assertIn("守护可能没在正常工作", self.notes[0])

    def test_undecided_rounds_are_logged_once_and_not_notified_while_short(self):
        r = G.Report(G.DAILY, cause="测速接口出错，判断不了现在通不通，这轮不动手", undecided=True, down=False)
        for _ in range(3):
            self.a([r], EVERYTHING)
        self.assertEqual(self.notes, [])
        self.assertEqual(sum("判断不了" in line for line in self.logs), 1)

    def test_changing_alive_counts_do_not_renotify(self):
        class Prov(FakeClash):
            def providers(self):
                out = super().providers()
                for n in out["sub"]["proxies"]:
                    n["alive"] = n["name"] in self.alive  # mihomo 的 alive 随健康检查、各种测速一直在变
                return out

        groups = topo(Proxies={"now": "TW", "all": ["TW"]}, TW={"now": "tw1", "all": ["tw1", "tw2"]})
        prov = {n: "Vmess" for n in ("tw1", "tw2", "x1", "x2", "x3")}
        c = Prov(groups, ALL_ALIVE - {"tw1", "tw2"} | {"x2"}, hidden={"tw1", "tw2"}, provider_types=prov)
        clock = Clock()
        guard = make(c, clock=clock)
        for i in range(24):  # 2 分钟：台湾整组一直不通、也没有别的安全出口；订阅里别的节点偶尔起落
            if i % 6 == 3:
                c.alive ^= {"x1"}
            self.a(guard.tick(), guard.last_checked)
            clock.t += 5
        self.assertEqual(sum(1 for n in self.notes if n.startswith("日常流量不通")), 1)
        self.assertTrue(any("个能通" in line for line in self.logs))  # 数字照样进日志

    def test_controller_read_error_is_not_announced_as_outage(self):
        clock = Clock()
        c = FakeClash(topo(), ALL_ALIVE)  # 网一直是好的
        guard = make(c, clock=clock)
        c.match_error = RuntimeError("/rules HTTP 500")  # 内核重载那一下 /rules 读失败
        self.a(guard.tick(), guard.last_checked)
        c.match_error = None
        clock.t += 61
        self.a(guard.tick(), guard.last_checked)
        self.assertEqual(self.notes, [])  # 不报「不通」，也就不会冒出「已恢复」
        self.assertTrue(any("守护处理时出错" in line for line in self.logs))

    def test_core_outage_notifies_once_per_reopen(self):
        clock = Clock()
        guard = make(FakeClash(topo(), ALL_ALIVE, up=False), clock=clock)
        run = lambda cmd, **kw: mock.Mock(returncode=1 if cmd[0] == "pgrep" else 0, stderr="")  # noqa: E731
        with mock.patch.object(G.subprocess, "run", run):
            for _ in range(26):  # 130 秒：Verge 一直没起来，守护每 60 秒重开一次
                self.a(guard.tick(), guard.last_checked)
                clock.t += 5
        self.assertEqual(len(self.notes), 3)  # 每次重开报一次；等它启动的那些轮不算新故障

    def test_steps_go_into_the_log(self):
        self.a([G.Report(G.DAILY, cause="x", steps=["TW 当前选的 tw1 不通；2 个安全候选都确认不通"], recovered=False)],
               EVERYTHING)
        self.assertTrue(any("2 个安全候选都确认不通" in line for line in self.logs))

    def test_busy_is_neither_down_nor_recovered(self):
        self.a([G.Report(G.DAILY, cause="x", recovered=False)], EVERYTHING)
        self.a([G.Report(G.DAILY, cause="另一个守护进程正在处理", undecided=True, down=False)], EVERYTHING)
        self.assertEqual(len(self.notes), 1)
        self.assertFalse(any("已恢复" in n for n in self.notes))

    def test_unchecked_rounds_do_not_count_as_recovery(self):
        self.a([G.Report(G.HOMEBB, cause="家宽节点都不通", recovered=False)], EVERYTHING)
        self.a([], {G.CORE, G.DAILY})  # 这轮没测家宽
        self.a([G.Report(G.CORE, cause="mihomo 控制器连不上", recovered=False)], {G.CORE})
        self.assertFalse(any("家宽已恢复" in n for n in self.notes))

    def test_persistent_daily_outage_across_cooldown_cycles_notifies_once(self):
        clock = Clock()
        c = FakeClash(topo(), ALL_ALIVE - {"a1", "a2", "b1", "b2", "tw1", "tw2", "hk1"})
        guard = make(c, clock=clock)
        for _ in range(40):  # 200 秒：跨过好几轮 60 秒冷却，状态文字在变，诊断没变
            self.a(guard.tick(), guard.last_checked)
            clock.t += 5
        self.assertEqual(sum(1 for n in self.notes if n.startswith("日常流量不通")), 1)

    def test_persistent_homebb_outage_across_real_ticks(self):
        clock = Clock()
        c = FakeClash(topo(), ALL_ALIVE - {"RESIP-A", "RESIP-B"})
        guard = make(c, clock=clock)
        for _ in range(8):  # 40 秒里家宽一直断：只报一次，不能冒出「已恢复」
            self.a(guard.tick(), guard.last_checked)
            clock.t += 5
        self.assertEqual(sum(1 for n in self.notes if n.startswith("家宽不通")), 1)
        self.assertFalse(any("家宽已恢复" in n for n in self.notes))
        c.alive |= {"RESIP-A", "RESIP-B"}
        for _ in range(8):
            self.a(guard.tick(), guard.last_checked)
            clock.t += 5
        self.assertEqual(sum(1 for n in self.notes if n == "家宽已恢复"), 1)


if __name__ == "__main__":
    unittest.main()
