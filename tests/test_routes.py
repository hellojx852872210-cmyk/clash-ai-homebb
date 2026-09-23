#!/usr/bin/env python3
"""出口覆盖：输入归一化、安全校验、记忆读写、规则集渲染，以及生成的规则顺序。"""
from __future__ import annotations

import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import routes as R


class NormalizeTest(unittest.TestCase):
    def test_bare_name_is_process(self):
        self.assertEqual(R.normalize_match("Telegram"), "PROCESS-NAME,Telegram")
        self.assertEqual(R.normalize_match("Google Chrome"), "PROCESS-NAME,Google Chrome")

    def test_domain_becomes_suffix(self):
        self.assertEqual(R.normalize_match("github.com"), "DOMAIN-SUFFIX,github.com")
        self.assertEqual(R.normalize_match("*.github.com"), "DOMAIN-SUFFIX,github.com")
        self.assertEqual(R.normalize_match(".github.com"), "DOMAIN-SUFFIX,github.com")

    def test_ip_becomes_cidr_no_resolve(self):
        self.assertEqual(R.normalize_match("203.0.113.9"), "IP-CIDR,203.0.113.9/32,no-resolve")
        self.assertEqual(R.normalize_match("203.0.113.0/24"), "IP-CIDR,203.0.113.0/24,no-resolve")

    def test_full_rule_passes_through_and_uppercases(self):
        self.assertEqual(R.normalize_match("domain-suffix,a.com"), "DOMAIN-SUFFIX,a.com")
        self.assertEqual(R.normalize_match("PROCESS-PATH-REGEX,.*/foo.app/"), "PROCESS-PATH-REGEX,.*/foo.app/")

    def test_empty_and_typeless_comma(self):
        with self.assertRaises(ValueError):
            R.normalize_match("  ")
        # 逗号前不是已知类型 → 当成进程名整体
        self.assertEqual(R.normalize_match("weird,thing"), "PROCESS-NAME,weird,thing")


class TargetTest(unittest.TestCase):
    def test_aliases(self):
        for raw, want in (("家宽", "homebb"), ("直连", "direct"), ("代理", "daily"), ("日常", "daily"),
                          ("HOMEBB", "homebb"), ("d", "direct"), ("proxy", "daily")):
            self.assertEqual(R.parse_target(raw), want, raw)

    def test_unknown(self):
        with self.assertRaises(ValueError):
            R.parse_target("月球")


class ValidateTest(unittest.TestCase):
    def test_ok(self):
        self.assertIsNone(R.validate("PROCESS-NAME,Telegram", "homebb"))
        self.assertIsNone(R.validate("DOMAIN-SUFFIX,github.com", "direct"))

    def test_ai_domain_cannot_leave_homebb(self):
        for t in ("direct", "daily"):
            msg = R.validate("DOMAIN-SUFFIX,claude.ai", t)
            self.assertIsNotNone(msg)
            self.assertIn("AI", msg)
        # 反过来：把 AI 域名设成家宽是允许的（虽然本来就是）
        self.assertIsNone(R.validate("DOMAIN-SUFFIX,claude.ai", "homebb"))

    def test_bad_type_and_empty_payload(self):
        self.assertIn("不支持", R.validate("GEOIP,CN", "direct"))
        self.assertIn("不能为空", R.validate("DOMAIN-SUFFIX,", "direct"))


class MemoryTest(unittest.TestCase):
    def path(self) -> Path:
        return Path(tempfile.mkdtemp()) / "routes.toml"

    def test_round_trip(self):
        p = self.path()
        rs = [R.Route("PROCESS-NAME,Telegram", "homebb", "备注"), R.Route("DOMAIN-SUFFIX,a.com", "direct")]
        R.save_routes(rs, p)
        back = R.load_routes(p)
        self.assertEqual([(r.match, r.target) for r in back], [(r.match, r.target) for r in rs])
        self.assertEqual(back[0].note, "备注")
        tomllib.loads(p.read_text(encoding="utf-8"))  # 合法 TOML

    def test_missing_file_is_empty(self):
        self.assertEqual(R.load_routes(self.path()), [])

    def test_upsert_replaces_same_match(self):
        rs: list[R.Route] = []
        rs, old = R.upsert(rs, "PROCESS-NAME,X", "homebb")
        self.assertIsNone(old)
        rs, old = R.upsert(rs, "PROCESS-NAME,X", "direct")
        self.assertEqual(old, "homebb")
        self.assertEqual(len(rs), 1)
        self.assertEqual(rs[0].target, "direct")

    def test_remove(self):
        rs = [R.Route("PROCESS-NAME,X", "homebb")]
        rs, hit = R.remove(rs, "PROCESS-NAME,X")
        self.assertTrue(hit)
        self.assertEqual(rs, [])
        _, hit = R.remove(rs, "PROCESS-NAME,X")
        self.assertFalse(hit)

    def test_bad_rows_ignored(self):
        p = self.path()
        p.write_text('[[route]]\nmatch = "PROCESS-NAME,A"\ntarget = "homebb"\n\n[[route]]\nmatch = ""\ntarget = "homebb"\n\n[[route]]\nmatch = "X"\ntarget = "月球"\n', encoding="utf-8")
        self.assertEqual([r.match for r in R.load_routes(p)], ["PROCESS-NAME,A"])


class RenderTest(unittest.TestCase):
    def test_payload_per_target(self):
        rs = [R.Route("PROCESS-NAME,A", "homebb"), R.Route("DOMAIN-SUFFIX,b.com", "homebb"), R.Route("DOMAIN-SUFFIX,c.com", "direct")]
        homebb = R.render_payload(rs, "homebb")
        self.assertIn('"PROCESS-NAME,A"', homebb)
        self.assertIn('"DOMAIN-SUFFIX,b.com"', homebb)
        self.assertNotIn("c.com", homebb)
        self.assertIn("payload: []", R.render_payload(rs, "daily"))

    def test_write_providers_creates_three_files(self):
        d = Path(tempfile.mkdtemp()) / "rules"
        got = R.write_providers([R.Route("PROCESS-NAME,A", "homebb")], d)
        self.assertEqual(sorted(p.name for p in got.values()), ["user-daily.yaml", "user-direct.yaml", "user-homebb.yaml"])
        self.assertTrue(all(p.exists() for p in got.values()))
        self.assertIn("PROCESS-NAME,A", got["homebb"].read_text(encoding="utf-8"))


class GeneratedRuleOrderTest(unittest.TestCase):
    """生成的配置里，覆盖规则必须排在 AI 规则之后 —— 这是 AI 改不了出口的保证。"""

    def build(self):
        import genconfig
        spec = {
            "names": {"ai_group": "Claude-Only", "homebb_group": "HB", "daily_group": "日常出口"},
            "homebb": {"nodes": [{"name": "H1", "type": "socks5", "server": "203.0.113.5", "port": 1080}]},
            "daily": {"subscriptions": [{"name": "a", "url": "https://a.example/sub"}]},
        }
        files, _ = genconfig.build(spec)
        return files["Merge.yaml"]

    def test_rule_providers_and_order(self):
        merge = self.build()
        for name in ("user-homebb", "user-direct", "user-daily"):
            self.assertIn(name, merge)
        lines = merge.splitlines()
        ai_last = max(i for i, l in enumerate(lines) if "Claude-Only" in l and "DOMAIN-SUFFIX" in l)
        rs_first = min(i for i, l in enumerate(lines) if "RULE-SET,user-" in l)
        self.assertLess(ai_last, rs_first, "覆盖规则必须排在 AI 域名规则之后")

    def test_targets_point_at_right_groups(self):
        merge = self.build()
        self.assertIn('"RULE-SET,user-homebb,Claude-Only"', merge)
        self.assertIn('"RULE-SET,user-direct,DIRECT"', merge)
        self.assertIn('"RULE-SET,user-daily,日常出口"', merge)


if __name__ == "__main__":
    unittest.main()


class HudChoiceTest(unittest.TestCase):
    """悬浮窗点击后的逻辑（不含 AppKit）：写记忆、拒绝、清除、取消。"""

    def setUp(self):
        import importlib
        import os
        self.work = Path(tempfile.mkdtemp())
        cfg = self.work / "cfg.toml"
        cfg.write_text(f'[clash]\nsocket = "{self.work}/nope.sock"\ncontroller = ""\nauto_discover = false\n'
                       f'[routes]\nfile = "{self.work}/routes.toml"\ndir = "{self.work}/rules"\n', encoding="utf-8")
        self.prev_env = os.environ.get("CLASH_AI_HOMEBB_CONFIG")
        os.environ["CLASH_AI_HOMEBB_CONFIG"] = str(cfg)
        import config
        importlib.reload(config)
        importlib.reload(R)
        import importlib.util
        spec = importlib.util.spec_from_file_location("flt", Path(__file__).resolve().parents[1] / "float.py")
        self.flt = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.flt)

    def tearDown(self):
        import importlib
        import os
        if self.prev_env is None:  # 恢复原值，别让后面的测试读到项目里的真实 config.toml
            os.environ.pop("CLASH_AI_HOMEBB_CONFIG", None)
        else:
            os.environ["CLASH_AI_HOMEBB_CONFIG"] = self.prev_env
        import config
        importlib.reload(config)
        importlib.reload(R)

    def routes(self):
        return R.load_routes(self.work / "routes.toml")

    def test_set_each_target(self):
        for tag, want in ((1, "homebb"), (2, "direct"), (3, "daily")):
            self.flt.choose_egress("PROCESS-NAME,Telegram", tag)
            got = self.routes()
            self.assertEqual([(r.match, r.target) for r in got], [("PROCESS-NAME,Telegram", want)], tag)
        # 规则集文件同步写了
        self.assertIn("Telegram", (self.work / "rules" / "user-daily.yaml").read_text(encoding="utf-8"))

    def test_cancel_changes_nothing(self):
        self.assertEqual(self.flt.choose_egress("PROCESS-NAME,X", 5), "")
        self.assertEqual(self.routes(), [])

    def test_clear_without_override(self):
        self.assertEqual(self.flt.choose_egress("PROCESS-NAME,X", 4), "本来就没有覆盖")

    def test_clear_after_set(self):
        self.flt.choose_egress("PROCESS-NAME,X", 2)
        self.assertEqual(len(self.routes()), 1)
        self.flt.choose_egress("PROCESS-NAME,X", 4)
        self.assertEqual(self.routes(), [])

    def test_ai_domain_refused(self):
        msg = self.flt.choose_egress("DOMAIN-SUFFIX,claude.ai", 2)
        self.assertIn("AI", msg)
        self.assertEqual(self.routes(), [])

    def test_ai_pinned_app_warns(self):
        # 让「写入并生效」这步成功，才看得到 AI 优先的提示（真机上 mihomo 在跑时就是这条路径）
        real_apply = R.apply
        R.apply = lambda rs, directory=None: (True, "已生效")
        try:
            warn = self.flt.choose_egress("PROCESS-PATH-REGEX,.*/Claude\\.app/", 2, ai_pinned=True)
            plain = self.flt.choose_egress("PROCESS-NAME,Telegram", 2, ai_pinned=False)
        finally:
            R.apply = real_apply
        self.assertIn("可能不生效", warn)
        self.assertIn("直连", warn)
        self.assertNotIn("可能不生效", plain)
        self.assertIn("即刻生效", plain)

    def test_apply_failure_is_reported(self):
        real_apply = R.apply
        R.apply = lambda rs, directory=None: (False, "让 mihomo 重读失败：X")
        try:
            msg = self.flt.choose_egress("PROCESS-NAME,Telegram", 1)
        finally:
            R.apply = real_apply
        self.assertIn("失败", msg)
        self.assertEqual(len(self.routes()), 1)  # 记忆仍然写下了

    def test_bad_tag(self):
        self.assertEqual(self.flt.choose_egress("PROCESS-NAME,X", 9), "")


class AppMatcherTest(unittest.TestCase):
    def matcher(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("flt2", Path(__file__).resolve().parents[1] / "float.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m.app_matcher

    def test_gui_app_uses_bundle_path_regex(self):
        f = self.matcher()
        self.assertEqual(f("Google Chrome", "/Applications/Google Chrome.app"), r"PROCESS-PATH-REGEX,.*/Google Chrome\.app/")
        # 元字符要转义，空格不用（两种写法内核都收，这里取干净的）
        self.assertEqual(f("A+B (x)", "/Applications/A+B (x).app"), r"PROCESS-PATH-REGEX,.*/A\+B \(x\)\.app/")

    def test_no_bundle_falls_back_to_process_name(self):
        f = self.matcher()
        self.assertEqual(f("curl", ""), "PROCESS-NAME,curl")
        self.assertIsNone(f("", ""))


class PickTargetTest(unittest.TestCase):
    """悬浮窗「换目标」：整个 App ↔ 各个域名。浏览器里不同页面走不同出口时用它单独调。"""

    def flt(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("flt3", Path(__file__).resolve().parents[1] / "float.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m

    def ctx(self, hosts=(("ab.chatgpt.com", "homebb"), ("www.bing.com", "daily"))):
        return {"app": "Microsoft Edge", "match": r"PROCESS-PATH-REGEX,.*/Microsoft Edge\.app/",
                "kind": "mixed", "ai": False, "hosts": hosts}

    def test_index_zero_is_whole_app(self):
        m = self.flt()
        match, shown, kind, ai = m.pick_target(self.ctx(), 0)
        self.assertEqual(shown, "Microsoft Edge")
        self.assertEqual(kind, "mixed")
        self.assertTrue(match.startswith("PROCESS-PATH-REGEX"))

    def test_index_selects_domain_with_its_own_kind(self):
        m = self.flt()
        self.assertEqual(m.pick_target(self.ctx(), 1)[:3], ("DOMAIN-SUFFIX,ab.chatgpt.com", "ab.chatgpt.com", "homebb"))
        self.assertEqual(m.pick_target(self.ctx(), 2)[:3], ("DOMAIN-SUFFIX,www.bing.com", "www.bing.com", "daily"))

    def test_ai_domain_flagged(self):
        m = self.flt()
        self.assertTrue(m.pick_target(self.ctx(), 1)[3])   # chatgpt 是 AI 域名
        self.assertFalse(m.pick_target(self.ctx(), 2)[3])

    def test_out_of_range_falls_back_to_app(self):
        m = self.flt()
        for idx in (-1, 3, 99):
            self.assertEqual(m.pick_target(self.ctx(), idx)[1], "Microsoft Edge")

    def test_count_and_host_cap(self):
        m = self.flt()
        self.assertEqual(m.target_count(self.ctx()), 3)
        self.assertEqual(m.target_count({"app": "X", "hosts": ()}), 1)
        many = tuple((f"h{i}.example.com", "daily") for i in range(20))
        self.assertEqual(m.target_count(self.ctx(many)), 1 + m.MAX_TARGET_HOSTS)
        self.assertEqual(m.pick_target(self.ctx(many), m.MAX_TARGET_HOSTS)[1], f"h{m.MAX_TARGET_HOSTS - 1}.example.com")

    def test_domain_target_is_rejected_for_ai(self):
        m = self.flt()
        match = m.pick_target(self.ctx(), 1)[0]
        self.assertIsNotNone(R.validate(match, "direct"))     # AI 域名不许离开家宽
        self.assertIsNone(R.validate(m.pick_target(self.ctx(), 2)[0], "direct"))
