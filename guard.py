#!/usr/bin/env python3
"""出口守护：日常流量 / 家宽不通时，分析原因并自动切到能通的出口，别让软件断网。

    python3 guard.py              # 常驻（launchd 用）：日常每 5 秒、AI / 家宽每 30 秒测一次（[guard] 可改）
    python3 guard.py --once       # 测一轮：不通就分析并处理；都通或都修好了才返回 0
    python3 guard.py --dry-run    # 测一轮并分析，只打印打算怎么处理，不改任何东西；发现问题返回 1
    python3 guard.py --explain    # 两条链现在走哪、通不通、有没有挂了也不会自动换的隐患（只读）

怎么测：直接让 mihomo 测两条链——没设覆盖的流量（最后那条 MATCH 规则）和 AI 组。
不通先隔 confirm_delay 秒复测，两次都明确不通（mihomo 回 503/504）才动手；测速接口出错不算不通。

五条原则：
  1. 安全：只切到能证明安全的出口。按 mihomo 报的实际类型判断，不看名字——直连 / 拒绝类节点（不管叫什么）、
     AI 组、家宽组及其能走到的一切、类型查不到的节点、绕回自己的环都算不安全；手动组看当前选择，
     自动组 / Relay 看全部成员（它随时可能自己换过去）。内核里找不到家宽组、又没写家宽白名单时认不出家宽，
     日常也不动手。家宽组只在 [deadchain] homebb_members 写明的节点之间换，而且这些名字必须真是节点
     （不是套着别的出口的组）；没写白名单就只报告不动手。
  2. 最小改动：从最靠近节点的一层往上修，修一层测一次，通了就停。手动组先在组内换：一批批并发测，
     记住这次故障里测过哪些（这条链一通就作废），有时间预算（软上限：开始了的那批会测完）；
     没测完下一轮接着测，没证明整组都不通不往上一层跳。
  3. 不用过期判断：每次动手（改选、解固定、让自动组重测）前先拿动手许可——兜底规则没变、目标此刻还明确不通
     （后台测，和诊断、候选测速并行；结果只在测完后 timeout 秒内算数，过期就重测）——再读一次最新状态，
     把要动的组、候选和家宽排除范围整个复核一遍，复核完立刻动手。动手后链路上游也变了（不是这次改动能
     解释的）就停手，不把别人的改动当成自己的。日常修复不碰 AI 组能走到的任何组（包括备用成员，
     不然 AI 的 fallback 一换就走过去了）。控制接口出错时不往上一层跳。
  4. 如实报告：「原因」只写稳定的诊断（去重靠它），冷却、扫描进度、接口出错、订阅还剩几个活节点、
     疑似上行断了这些随时在变的另写（本轮状态进摘要，数字进日志）；
     测速把「确认不通」（mihomo 回 503/504）和「接口出错、判断不了」分开，后者不算节点死了，也不当动手的依据。
     没下结论的（另一个守护正在处理、兜底规则刚变、接口出错、处理中控制接口出错）只记日志，已经做了的动作照样报；
     一条链持续没下结论超过 2 分钟，提醒一次（守护自己可能坏了）；
     自己闪断又自己好了也只记日志，不弹通知。
  5. 隔离：两条链各自处理异常；日常规则读不到不影响家宽自己的检查和修复。
AI 组本身永远不改选；家宽全挂时 AI 保持断开，不回落到机场。两条链串行处理（家宽 30 秒才测一次，
被日常扫描拖几秒可以接受）；冷却、退避只在单个常驻进程里记，跨进程只靠一把锁保证同一时间只有一个守护动手。
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import contextlib
import fcntl
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import watch as W  # noqa: E402
from config import SETTINGS, GuardCfg  # noqa: E402

AUTO_TYPES = frozenset({"URLTest", "Fallback", "LoadBalance"})
NOT_A_PROXY = frozenset({"Direct", "Reject", "RejectDrop", "Pass", "Compatible", "Dns"})  # mihomo 报的类型
UNSAFE_NAMES = frozenset({"DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE", "GLOBAL"})
CHAIN_LIMIT = 8  # watch.follow_chain 的默认层数上限
BATCH = 12  # 一批并发测几个候选
STALE_DAYS = 3
REOPEN_BACKOFF = 60.0
DAILY, HOMEBB, CORE = "日常流量", "家宽", "内核"
NO_HOMEBB = "内核里找不到家宽组、又没写家宽白名单：认不出哪些节点是家宽，这轮不动手"
STUCK_ALERT = 120.0  # 一条链持续「没下结论」这么久就提醒一次：守护自己可能没在正常工作


class ProbeError(Exception):
    """控制接口本身出错（不是「节点不通」的结论）。"""


def _status(e: Exception) -> int | None:
    """watch._request 对 >=300 的响应抛 RuntimeError("<path> HTTP <状态码>")。"""
    m = re.search(r"HTTP (\d{3})$", str(e))
    return int(m.group(1)) if m else None


class Clash:
    """守护用到的 mihomo 控制器接口。测试里换成假的。"""

    DEAD = (503, 504)  # 实测 mihomo 1.19：测速超时 / 测不通回 504（出错时 503）；找不到回 404

    def __init__(self, timeout: float) -> None:
        self.timeout = timeout
        self._match: tuple[float, str] = (0.0, "")  # 兜底规则目标缓存 60 秒：只给「测一下」用，动手时现读

    def _q(self, url: str) -> str:
        return urllib.parse.urlencode({"url": url, "timeout": int(self.timeout * 1000)})

    @staticmethod
    def _p(name: str) -> str:
        return urllib.parse.quote(name, safe="")

    def reachable(self) -> bool:
        return W.clash_reachable()

    def proxies(self) -> dict:
        return W.api_json("/proxies", timeout=8).get("proxies") or {}

    def providers(self) -> dict:
        return W.api_json("/providers/proxies", timeout=8).get("providers") or {}

    def match_target(self, fresh: bool = False) -> str:
        if not fresh and self._match[1] and time.monotonic() - self._match[0] < 60:
            return self._match[1]
        rules = W.api_json("/rules", timeout=8).get("rules") or []
        target = next((str(r.get("proxy") or "") for r in reversed(rules)
                       if str(r.get("type") or "").lower() == "match"), "")
        self._match = (time.monotonic(), target)
        return target

    def _delay_call(self, path: str) -> int | None:
        try:
            r = W.api_json(path, timeout=self.timeout + 3)
        except RuntimeError as e:
            if _status(e) in self.DEAD:
                return None
            raise
        d = (r or {}).get("delay")
        if isinstance(d, int) and d > 0:
            return d
        raise ProbeError(f"测速响应里没有延迟：{r!r}"[:120])

    def probe(self, name: str, url: str, provider: str | None = None) -> int | None:
        """经这个节点 / 组（组按当前选择往下走）访问 url 的延迟；确认不通返回 None；接口出错抛 ProbeError。

        订阅（provider）里的节点不在 /proxies 里（按名字测会 404），要走 provider 的测速接口；
        调用方知道它在哪个订阅（provider）就直接走那里，省掉一次 404 和一次整份订阅列表的查询。"""
        if provider is None:
            try:
                return self._delay_call(f"/proxies/{self._p(name)}/delay?{self._q(url)}")
            except RuntimeError as e:
                if _status(e) != 404:
                    raise ProbeError(str(e)) from e
            except ProbeError:
                raise
            except Exception as e:
                raise ProbeError(str(e)) from e
            provider = self._provider_of(name)
            if not provider:
                raise ProbeError(f"内核里找不到 {name}")
        try:
            return self._delay_call(f"/providers/proxies/{self._p(provider)}/{self._p(name)}/healthcheck?{self._q(url)}")
        except ProbeError:
            raise
        except Exception as e:
            raise ProbeError(str(e)) from e

    def _provider_of(self, node: str) -> str | None:
        try:
            provs = self.providers()
        except Exception:
            return None
        return next((n for n, p in provs.items() if n != "default"
                     and any(x.get("name") == node for x in p.get("proxies") or [])), None)

    def retest(self, group: str, url: str) -> str:
        """让组立刻把成员测一遍，mihomo 按结果重新选。

        返回 "ok"（测完、有成员能通）、"dead"（测完、全都不通：回 503/504），其它字符串是接口出错、没测成。"""
        try:
            W.api_json(f"/group/{self._p(group)}/delay?{self._q(url)}", timeout=self.timeout + 5)
        except RuntimeError as e:
            return "dead" if _status(e) in self.DEAD else f"接口出错：{e}"
        except Exception as e:
            return f"接口出错：{e}"
        return "ok"

    def select(self, group: str, name: str) -> None:
        W.api_json(f"/proxies/{self._p(group)}", method="PUT", body={"name": name})

    def unfix(self, group: str) -> None:
        W.api_json(f"/proxies/{self._p(group)}", method="DELETE")


@dataclass
class View:
    """一次读到的内核状态：各组 / 节点，加上每个名字的实际类型（订阅节点的类型来自 provider）。"""

    proxies: dict
    types: dict[str, str]
    provider_of: dict[str, str] = field(default_factory=dict)  # 订阅节点 → 所在订阅（它们不在 /proxies 里）


@dataclass
class Report:
    path: str  # 日常流量 / 家宽 / 内核
    cause: str = ""  # 稳定的诊断：去重、通知靠它
    steps: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)  # 本轮状态：为什么这轮没修好（冷却、没测完、接口出错……）
    recovered: bool | None = None  # True 修好了；False 还不通；None 没法判断（演练、接口出错）
    undecided: bool = False  # 这轮没下结论（另一个守护正在处理、兜底规则刚变、测速接口出错）：不算不通也不算恢复
    down: bool = True  # 摘要里说不说「不通」：两次检查都确认过不通才说（另一个守护正在处理、测速判断不了、读控制接口失败时不说）

    def summary(self) -> str:
        head = f"{self.path}不通：{self.cause or '原因未明'}" if self.down else f"{self.path}：{self.cause}"
        if self.actions:
            head += "。已处理：" + "；".join(self.actions)
        why = f"（{'，'.join(dict.fromkeys(self.blocked))}）" if self.blocked else ""
        if self.recovered is True:
            head += "。已恢复"
        elif self.undecided:
            head += ("。这轮没下结论" + why) if self.down else ""
        elif self.recovered is False:
            head += "。仍不通" + why
        return head


class Confirm:
    """动手许可：兜底目标没变、而且目标此刻还明确不通（mihomo 回 503/504）。

    目标在后台测，和诊断、候选测速并行，不多等；一个结果只在测完后 timeout 秒内算数，过期了就重测。
    多次观察只留开测最晚的那次（自己动手后刚测的那次也算）。接口出错（判断不了）不算不通。"""

    def __init__(self, guard: Guard, target: str, same: Callable[[], bool]) -> None:
        self.guard, self.target, self.same = guard, target, same
        self.pool = cf.ThreadPoolExecutor(max_workers=1)
        self.lock = threading.Lock()
        self.fut: cf.Future | None = None
        self.last: tuple[float, float, int | None | ProbeError] | None = None  # (开测, 测完, 结果)
        self.kick()

    def __enter__(self) -> Confirm:
        return self

    def __exit__(self, *exc) -> None:
        self.pool.shutdown(wait=False)

    def kick(self) -> None:
        """后台开测一次；已经在测就不重复开。"""
        if self.fut is None or self.fut.done():
            self.fut = self.pool.submit(self._run)

    def _run(self) -> None:
        start = self.guard.now()
        self.saw(start, self.guard.probe_state(self.target))

    def saw(self, start: float, result: int | None | ProbeError) -> None:
        with self.lock:
            if self.last is None or start >= self.last[0]:
                self.last = (start, self.guard.now(), result)

    def verdict(self) -> str:
        """down（可以动手）/ up（已经好了）/ error（判断不了）/ moved（兜底规则变了）。

        兜底规则放在所有等待之后才读：拿到新鲜的观察、现读规则，读完立刻返回，调用方接着复核、动手。"""
        fresh = max(self.guard.cfg.timeout, 1.0)
        for _ in range(3):  # 在测的那次可能比手上的还旧：等它测完还不新鲜，就再开一次
            with self.lock:
                last = self.last
            if last is not None and self.guard.now() - last[1] <= fresh:
                try:
                    if not self.same():
                        return "moved"
                except Exception:
                    return "error"
                if self.guard.now() - last[1] <= fresh:  # 规则表大时读一次要好几秒：读完还新鲜才算
                    v = last[2]
                    return "down" if v is None else "up" if isinstance(v, int) else "error"
                continue
            self.kick()
            self.fut.result()
        return "error"


class Guard:
    def __init__(self, clash, cfg: GuardCfg, policy: W.Policy, dry_run: bool = False,
                 now: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 lock_path: Path | None = None) -> None:
        self.clash, self.cfg, self.policy, self.dry_run = clash, cfg, policy, dry_run
        self.now, self.sleep, self.lock_path = now, sleep, lock_path
        self.touched: dict[str, float] = {}  # 组 → 上次动它的时间（冷却）
        self.tested: dict[tuple[str, str], tuple[str, set[str]]] = {}  # (链, 组) → (当时选的, 这次故障里测过不通的候选)
        self.last_homebb = float("-inf")
        self.last_reopen = float("-inf")
        self.last_checked: set[str] = set()  # 这一轮实际测过哪几条（通知靠它判断「恢复」）

    # ---------- 小工具 ----------
    def probe_state(self, name: str, url: str | None = None, provider: str | None = None) -> int | None | ProbeError:
        """测一次：延迟、None（确认不通）或 ProbeError（接口出错、判断不了）。"""
        try:
            return self.clash.probe(name, url or self.cfg.test_url, provider)
        except ProbeError as e:
            return e

    def ok(self, name: str, url: str | None = None) -> int | None:
        """只用来辅助判断（比如怀疑上行断了）：接口出错也当不通。"""
        v = self.probe_state(name, url)
        return v if isinstance(v, int) else None

    def probe_many(self, names: list[str], view: View) -> dict[str, int | None | ProbeError]:
        """并发测一批：值是延迟、None（确认不通）或 ProbeError（接口出错、判断不了）。"""
        if not names:
            return {}
        with cf.ThreadPoolExecutor(max_workers=min(len(names), BATCH)) as ex:
            return dict(zip(names, ex.map(lambda n: self.probe_state(n, provider=view.provider_of.get(n)), names)))

    def cooling(self, group: str) -> bool:
        t = self.touched.get(group)
        return t is not None and self.now() - t < self.cfg.cooldown

    def act(self, rep: Report, text: str, fn: Callable[[], None]) -> bool:
        if self.dry_run:
            rep.actions.append("（演练，未执行）" + text)
            return False
        try:
            fn()
        except Exception as e:
            rep.steps.append(f"{text}：失败（{e}）")
            rep.blocked.append("控制接口出错")
            return False
        rep.actions.append(text)
        return True

    @contextlib.contextmanager
    def locked(self) -> Iterator[bool]:
        """同一时间只让一个守护进程动手（常驻进程和手动 --once 可能撞上）。"""
        if self.lock_path is None or self.dry_run:
            yield True
            return
        with open(self.lock_path, "a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def forget(self, path: str) -> None:
        """这条链通了，这次故障就算结束：记下的「测过不通」作废，下次故障重新测。"""
        for key in [k for k in self.tested if k[0] == path]:
            del self.tested[key]

    def view(self) -> View:
        proxies = self.clash.proxies()
        types = {str(n): str(v.get("type") or "") for n, v in proxies.items()}
        provider_of: dict[str, str] = {}
        try:
            for pname, p in self.clash.providers().items():
                for node in p.get("proxies") or []:
                    n = str(node.get("name"))
                    types.setdefault(n, str(node.get("type") or ""))
                    if pname != "default" and n not in proxies:
                        provider_of.setdefault(n, pname)
        except Exception:
            pass  # 拿不到订阅节点的类型：这些节点就算「不确定」，不会被当成安全
        return View(proxies, types, provider_of)

    @staticmethod
    def reach(name: str, view: View) -> set[str]:
        """从这个组出发、顺着全部成员能走到的所有名字（包括自己）。"""
        out, todo = set(), [name]
        while todo:
            n = todo.pop()
            if n in out:
                continue
            out.add(n)
            todo.extend((view.proxies.get(n) or {}).get("all") or [])
        return out

    def clean(self, name: str, view: View, unsafe: set[str], seen: tuple[str, ...] = ()) -> bool:
        """这个出口现在和以后会不会落到不安全的地方。按实际类型判断：直连 / 拒绝类（不管叫什么）、
        unsafe 里的名字、类型查不到的、绕回自己的环都不安全；自动组 / Relay 看全部成员，手动组看当前选择。"""
        if name in unsafe or name in UNSAFE_NAMES or name in seen:
            return False
        t = view.types.get(name)
        if not t or t in NOT_A_PROXY:
            return False
        info = view.proxies.get(name) or {}
        if t in AUTO_TYPES or t == "Relay" or (t not in W.GROUP_TYPES and info.get("all")):
            # 自动组 / Relay / 不认识的组（有成员就当组，比如分支内核的新组类型）：全部成员都得安全
            members = info.get("all") or []
            return bool(members) and all(self.clean(m, view, unsafe, seen + (name,)) for m in members)
        if t in W.GROUP_TYPES:
            now = info.get("now")
            return bool(now) and self.clean(now, view, unsafe, seen + (name,))
        return True

    @staticmethod
    def allowed(name: str, view: View, only: set[str] | None) -> bool:
        """白名单模式（家宽）：名字在白名单里，而且真是个节点——不是套着别的出口的组（有成员的都算组）。"""
        if only is None:
            return True
        t = view.types.get(name)
        return (name in only and bool(t) and t not in NOT_A_PROXY and t not in W.GROUP_TYPES
                and not (view.proxies.get(name) or {}).get("all"))

    def auto_ok(self, group: str, view: View, unsafe: set[str], only: set[str] | None) -> bool:
        """能不能替这个自动组重测：它连同全部成员都安全；白名单模式下成员还得都是白名单里的节点。"""
        members = (view.proxies.get(group) or {}).get("all") or []
        return self.clean(group, view, unsafe) and all(self.allowed(m, view, only) for m in members)

    @staticmethod
    def truncated(chain: tuple[str, ...], view: View) -> bool:
        """follow_chain 走到层数上限时还停在组上：后面还有，整条链看不全。"""
        return len(chain) >= CHAIN_LIMIT and view.types.get(chain[-1]) in W.GROUP_TYPES

    def uplink_suspect(self, chains: list[str]) -> bool:
        """直连的几个检测地址都不通、给的链也都不通：多半是本机上行断了。只是怀疑，照样试备选。
        直连有一个通就不用再测别的链（家宽常按流量计费，日常断着的时候别每轮都经家宽测一次）。"""
        urls = list(self.cfg.uplink_urls)
        with cf.ThreadPoolExecutor(max_workers=max(1, len(urls))) as ex:
            if any(list(ex.map(lambda u: self.ok("DIRECT", u), urls))):
                return False
        return not any(self.ok(c) for c in chains)

    # ---------- 分析 ----------
    def provider_facts(self, node: str) -> tuple[str, str]:
        """节点所在订阅的情况：(稳定的：副本多久没更新, 会变的：还剩几个活节点)。

        活节点数随健康检查、守护自己的测速一直在变，只进日志，不进「原因」（去重靠原因）。"""
        try:
            provs = self.clash.providers()
        except Exception:
            return "", ""
        for name, p in provs.items():
            nodes = p.get("proxies") or []
            if name == "default" or not any(n.get("name") == node for n in nodes):
                continue
            alive = sum(1 for n in nodes if n.get("alive"))
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(str(p.get("updatedAt")))).days
            except Exception:
                age = 0
            stale = f"所在订阅 {name} 的副本 {age} 天没更新" if age >= STALE_DAYS and p.get("vehicleType") == "File" else ""
            return stale, f"所在订阅 {name} 共 {len(nodes)} 个节点，{alive} 个能通"
        return "", ""

    @staticmethod
    def risks(chain: tuple[str, ...], proxies: dict) -> list[str]:
        """链上会让它挂了也不自动换的地方：被钉住的自动组、选了单个节点的手动组。通的时候也值得知道。"""
        out = []
        for g in chain:
            info = proxies.get(g) or {}
            t, now = info.get("type"), info.get("now")
            if t in AUTO_TYPES and info.get("fixed"):
                out.append(f"{g} 被钉在 {info['fixed']}，不会自动换（常见原因：Verge 重启后套回了以前点过的选择）")
            if t == "Selector" and now not in (None, "", "DIRECT") and (proxies.get(now) or {}).get("type") not in W.GROUP_TYPES:
                out.append(f"{g} 是手动组，固定选了 {now}，挂了不会自动切换")
        return out

    def explain(self, chain: tuple[str, ...], view: View, rep: Report) -> str:
        """稳定的诊断（当「原因」用）；会变的数字记进 rep.steps。"""
        facts = []
        node = chain[-1] if chain else ""
        if node == "DIRECT" and len(chain) >= 2:
            facts.append(f"{chain[-2]} 选了直连（DIRECT），走它的网站直连访问不了")
        elif node and (view.proxies.get(node) or {}).get("type") not in W.GROUP_TYPES:
            alive = self.probe_state(node, provider=view.provider_of.get(node))
            if isinstance(alive, ProbeError):
                facts.append(f"当前节点 {node} 测速接口出错，判断不了它通不通")
            elif alive:
                facts.append(f"当前节点 {node} 单独测是通的，链上的组还没换过来")
            else:
                facts.append(f"当前节点 {node} 不通")
                stale, count = self.provider_facts(node)
                if stale:
                    facts.append(stale)
                if count:
                    rep.steps.append(count)
        facts.extend(self.risks(chain, view.proxies))
        return "；".join(facts) or "链路不通"

    # ---------- 修 ----------
    def pick(self, path: str, group: str, view: View, unsafe: set[str], only: set[str] | None, deadline: float,
             first_scan: bool) -> tuple[str | None, str, bool]:
        """在组里找能通的安全替换：日常出口组 > 其它自动组 > 手动组 > 单个节点，同级按组里原顺序。

        一批批并发测，找到就停；记住这次故障里测过哪些（当前选择变了、或者这条链通过一次就作废重来），
        所以候选集变了（订阅更新插进新节点）也不会漏。first_scan 的那批不看预算，之后每批开始前看，用完就停。
        返回 (候选, 说明, 当前候选是否全都确认不通)。"""
        info = view.proxies.get(group) or {}
        cur = info.get("now")
        pool = [m for m in info.get("all") or []
                if m != cur and self.allowed(m, view, only) and self.clean(m, view, unsafe)]

        def rank(m: str) -> int:
            t = view.types.get(m)
            if m == self.policy.daily_group:
                return 0
            return 1 if t in AUTO_TYPES else 2 if t in W.GROUP_TYPES else 3

        ranked = sorted(pool, key=lambda m: (rank(m), pool.index(m)))
        if not ranked:
            return None, "组里没有可换的安全出口", True
        key = (path, group)
        was, tested = self.tested.get(key, ("", set()))
        if was != cur:
            tested = set()
        allow = first_scan
        while True:
            untested = [m for m in ranked if m not in tested]
            if not untested:
                self.tested.pop(key, None)
                return None, f"{len(ranked)} 个安全候选都确认不通", True
            if not allow and self.now() >= deadline:
                self.tested[key] = (cur, tested)
                return None, f"已确认 {len(tested)} 个不通，还有 {len(untested)} 个没测完（这轮时间用完），下一轮接着测", False
            allow = False
            batch = untested[:BATCH]
            got = self.probe_many(batch, view)
            alive = [m for m in batch if isinstance(got[m], int)]
            if alive:
                self.tested.pop(key, None)
                return alive[0], f"{alive[0]} {got[alive[0]]}ms 能通", True
            errs = [m for m in batch if isinstance(got[m], ProbeError)]
            tested |= {m for m in batch if got[m] is None}
            if errs:
                self.tested[key] = (cur, tested)
                return None, f"{len(errs)} 个候选测速接口出错（{got[errs[0]]}），判断不了，下一轮再测", False

    def heal_auto(self, group: str, view: View, rep: Report) -> bool | None:
        """解开钉住的选择，让它立刻重测。返回 True 真的重测了、False 什么也没做（演练 / 没开解固定）、
        None 控制接口出错（这轮别再往上一层跳）。

        动手许可和按最新状态的复核由调用方先做完，view 就是复核时读到的最新状态。
        实测 mihomo 1.19：对 url-test / fallback 组调 /group/{g}/delay 会顺带解开固定，所以被钉住、又没开
        unfix_auto 时连重测也不做（调用方在层开头已经跳过了，这里是第二道闸）。"""
        info = view.proxies.get(group) or {}
        before = info.get("now")
        did = False
        if info.get("fixed"):
            if not self.cfg.unfix_auto:
                rep.steps.append(f"{group} 被钉在 {info['fixed']}，没开自动解固定，不替它重测")
                return False
            did = self.act(rep, f"解开 {group} 的固定（原来钉在 {info['fixed']}）", lambda: self.clash.unfix(group))
            if not did and not self.dry_run:
                return None
        if self.dry_run:
            rep.steps.append(f"（演练）会让 {group} 立刻重测成员")
            return False
        status = self.clash.retest(group, info.get("testUrl") or self.cfg.test_url)
        if status not in ("ok", "dead"):
            rep.steps.append(f"{group} 重测没做成（{status}），不进冷却，下一轮再试")
            rep.blocked.append(f"{group} 重测接口出错")
            return None
        self.touched[group] = self.now()
        rep.steps.append(f"{group} 已重测：{'有成员能通' if status == 'ok' else '成员全都不通'}")
        try:
            after = (self.clash.proxies().get(group) or {}).get("now")
        except Exception:  # 重测已经做了（可能已经换了）：先记成动作，再让上层按「控制接口出错」收尾
            rep.actions.append(f"让 {group} 立刻重测（之后读不到它的选择，不知道换没换）")
            raise
        if after and after != before:
            rep.actions.append(f"{group}：{before} → {after}（重测后自动换）")
        return True

    def heal_chain(self, rep: Report, target: str, base: tuple[str, ...], groups: list[str],
                   unsafe_of: Callable[[View], set[str] | None], confirm: Confirm,
                   only: set[str] | None = None) -> None:
        """从最靠近节点的一层往上修，修一层测一次，通了就停。

        base 是诊断时看到的整条链；每层开始前重新核对，跟它不一样（有人手动改过）这轮就停手。
        unsafe_of 按某一刻读到的内核状态给出不能去的名字（None = 认不出，不动手），每次读状态都重新算。
        每次动手前先拿动手许可（可能要等后台复测），再读一次最新状态把要动的组和候选整个复核一遍，
        复核完立刻动手，中间不再等任何东西。动手后只认这一层往下的变化：上游也变了就是别人改的，停手。
        中途控制接口出错：已经做了的动作留在报告里，这轮不下结论。"""
        try:
            self._heal(rep, target, base, groups, unsafe_of, confirm, only)
        except Exception as e:
            rep.steps.append(f"处理中控制接口出错：{e!r}")
            rep.blocked.append("控制接口出错")
            rep.undecided, rep.recovered = True, None

    def _heal(self, rep: Report, target: str, base: tuple[str, ...], groups: list[str],
              unsafe_of: Callable[[View], set[str] | None], confirm: Confirm, only: set[str] | None) -> None:
        deadline = self.now() + self.cfg.scan_budget
        free = True  # 下一次挑候选能不能先测一批再看预算：这轮头一次挑，或者下面一层刚证明整组都不通

        def stop(step: str, why: str) -> None:
            rep.steps.append(step)
            rep.blocked.append(why)

        def undecided(step: str, why: str) -> None:
            stop(step, why)
            rep.undecided = True

        def ready(g: str, info: dict, still_ok: Callable[[View, set[str]], bool]) -> View | None:
            """动手许可 + 按最新状态复核（要动的组本身、它的当前选择和钉住状态、候选）；
            通过就返回最新状态，调用方拿它立刻动手。info 是这一层开头读到的 g。"""
            verdict = confirm.verdict()
            if verdict == "up":
                stop("动手前复核：目标已经自己好了，这轮不动手", "目标已自行恢复")
                return None
            if verdict == "moved":
                undecided("动手前复核：兜底规则刚变了（内核可能在重载），这轮不动手", "兜底规则刚变了")
                return None
            if verdict == "error":
                undecided("动手前复核：目标测速接口出错，判断不了它还通不通，这轮不动手", "测速接口出错")
                return None
            fresh = self.view()
            funsafe = unsafe_of(fresh)
            if funsafe is None:
                stop(NO_HOMEBB, "认不出家宽节点")
                return None
            now_g = fresh.proxies.get(g) or {}
            if (W.follow_chain(fresh.proxies, target) != base or g in funsafe
                    or now_g.get("now") != info.get("now") or now_g.get("fixed") != info.get("fixed")
                    or not still_ok(fresh, funsafe)):
                stop(f"等动手许可这段时间 {g} 或它的候选被改过，不覆盖", "链路刚被别人改过")
                return None
            return fresh

        for g in reversed(groups):
            if self.cooling(g):
                stop(f"{g} 刚动过，冷却中先不碰", f"{g} 冷却中")
                continue
            chain = W.follow_chain(self.clash.proxies(), target)
            if chain != base:
                stop(f"链路刚被改过（现在走 {' → '.join(chain)}），这轮不再动手", "链路刚被别人改过")
                break
            view = self.view()
            unsafe = unsafe_of(view)
            if unsafe is None:
                stop(NO_HOMEBB, "认不出家宽节点")
                break
            info = view.proxies.get(g) or {}
            t, before = info.get("type"), info.get("now")
            did = False
            if g in unsafe:  # 比如日常链经过了 AI 链上的组：改它就等于改 AI 的出口
                rep.steps.append(f"{g} 在不能碰的范围里（AI / 家宽链上的组，或 GLOBAL 这类特殊组），不碰它，往上一层找")
                continue
            if t in AUTO_TYPES:
                if not self.auto_ok(g, view, unsafe, only):
                    rep.steps.append(f"{g} 里混有直连 / 拒绝 / 类型不明或不该走的成员，守护不替它重测，往上一层找")
                    continue
                if info.get("fixed") and not self.cfg.unfix_auto:
                    rep.steps.append(f"{g} 被钉在 {info['fixed']}，没开自动解固定（重测会顺带解开），不替它重测，往上一层找")
                    continue
                fresh = ready(g, info, lambda v, u, g=g: self.auto_ok(g, v, u, only))
                if fresh is None:
                    break
                did = self.heal_auto(g, fresh, rep)
                if did is None or self.dry_run:  # 接口出错不是「这层修不好」，别往上跳；演练停在第一步
                    break
            elif t == "Selector":
                if not self.cfg.switch_selectors:
                    rep.blocked.append("没开手动组自动切换")
                    continue
                confirm.kick()  # 目标复测和候选测速一起跑：挑出候选时，动手许可也是新鲜的
                cand, note, done = self.pick(rep.path, g, view, unsafe, only, deadline, first_scan=free)
                free = cand is None and done  # 这层刚证明整组都不通：上一层至少测一批，不然大组会让上一层永远轮不到
                rep.steps.append(f"{g} 当前选的 {before} 不通；{note}")
                if cand:
                    def still_ok(v: View, u: set[str], g: str = g, c: str = cand) -> bool:
                        return (c in ((v.proxies.get(g) or {}).get("all") or [])
                                and self.clean(c, v, u) and self.allowed(c, v, only))
                    if ready(g, info, still_ok) is None:
                        break
                    did = self.act(rep, f"{g}：{before}（不通）→ {cand}", lambda g=g, c=cand: self.clash.select(g, c))
                    if did:
                        self.touched[g] = self.now()
                    if not did:  # 演练停在第一步；改选没成功（控制接口出错）不是「这层修不好」，别往上跳
                        break
                elif not done:  # 没证明整组都不通，不往上一层跳
                    rep.blocked.append(f"{g} 里还有候选没测完")
                    break
                else:
                    rep.blocked.append(f"{g} 里能换的安全出口都不通")
            else:
                continue
            if did and not self.dry_run:
                start = self.now()
                v = self.probe_state(target)
                confirm.saw(start, v)
                if isinstance(v, int):
                    rep.recovered = True
                    return
                if isinstance(v, ProbeError):
                    undecided(f"动手后确认时测速接口出错（{v}），这轮不再动手", "测速接口出错")
                    rep.recovered = None
                    return
                new = W.follow_chain(self.clash.proxies(), target)
                upper = base[:base.index(g) + 1]
                if new[:len(upper)] != upper:  # 自己只动了 g 这一层：g 往上也变了，就是别人改的
                    stop(f"动手后链路上游也变了（现在走 {' → '.join(new)}），不是这次改动能解释的，这轮不再动手",
                         "链路刚被别人改过")
                    break
                base = new
        if self.dry_run or rep.undecided:
            rep.recovered = None
            return
        v = self.probe_state(target)
        if isinstance(v, ProbeError):
            undecided(f"最后确认时测速接口出错（{v}）", "测速接口出错")
            rep.recovered = None
        else:
            rep.recovered = bool(v)

    # ---------- 两条链 ----------
    def handle_daily(self, checked: str | None = None) -> Report:
        """checked：刚才两次检查测的兜底目标（可能来自缓存）；和现读的不一样，就是规则刚变，这轮不动。"""
        rep = Report(DAILY)
        target = self.clash.match_target(fresh=True)  # 要动手了，别用缓存
        if checked is not None and target != checked:
            rep.cause = f"兜底规则刚变了（{checked} → {target}），新目标还没测过，这轮先不动"
            rep.undecided, rep.down = True, False
            return rep
        view = self.view()
        chain = W.follow_chain(view.proxies, target)
        if not chain:
            rep.cause, rep.undecided, rep.down = "规则表里没找到兜底规则（内核可能正在重载），这轮先不动", True, False
            return rep
        rep.steps.append("没设覆盖的流量走：" + " → ".join(chain))
        if self.truncated(chain, view):
            rep.cause, rep.recovered = f"链路超过 {CHAIN_LIMIT} 层，看不全，守护不动手", False
            return rep
        ai, hb = self.policy.ai_group, self.policy.homebb_group
        listed = set(UNSAFE_NAMES) | {ai, hb, *self.policy.homebb_members}

        def unsafe_of(v: View) -> set[str] | None:
            """日常绝不能进家宽，也不能碰 AI 的出口：配置写明的、家宽组运行时能走到的一切、AI 组能走到的每个组
            （包括 fallback 的备用成员：改了它，AI 的 fallback 一换就走过去了），都保守地排除（只排除，不据此信任
            任何东西）。内核里找不到家宽组、又没写白名单：认不出家宽节点，返回 None（不动手）。"""
            ai_path = {n for n in self.reach(ai, v) if v.types.get(n) in W.GROUP_TYPES or (v.proxies.get(n) or {}).get("all")}
            if hb in v.proxies:
                return listed | ai_path | self.reach(hb, v)
            return (listed | ai_path) if self.policy.homebb_members else None

        if unsafe_of(view) is None:
            rep.cause = (f"内核里找不到家宽组 {hb}，也没在 [deadchain] homebb_members 写明家宽节点，"
                         "认不出哪些是家宽，日常这轮不自动切换")
            rep.recovered = False
            return rep
        with Confirm(self, target, lambda: self.clash.match_target(fresh=True) == target) as confirm:
            suspect = self.uplink_suspect([ai])
            rep.cause = self.explain(chain, view, rep)
            groups = [g for g in chain if view.types.get(g) in W.GROUP_TYPES]
            self.heal_chain(rep, target, chain, groups, unsafe_of, confirm)
        if rep.recovered is False and suspect:
            rep.blocked.insert(0, "疑似本机上行断了：直连检测地址和家宽都不通")
        return rep

    def handle_homebb(self) -> Report:
        rep = Report(HOMEBB)
        ai, hb = self.policy.ai_group, self.policy.homebb_group
        view = self.view()
        chain = W.follow_chain(view.proxies, ai)
        rep.steps.append("AI 流量走：" + " → ".join(chain))
        info = view.proxies.get(hb) or {}
        members = list(info.get("all") or [])
        trusted = set(self.policy.homebb_members)
        if not trusted:
            rep.cause = "没在 [deadchain] homebb_members 写明家宽节点，守护认不出哪些算家宽，只报告不动手"
            rep.recovered = False
            return rep
        unsafe = set(UNSAFE_NAMES) | {ai}
        bad = [m for m in members if not self.allowed(m, view, trusted) or not self.clean(m, view, unsafe)]
        if hb not in chain or not members or bad:
            if hb not in chain:
                rep.cause = f"AI 组 {ai} 没走家宽组 {hb}（死链结构被改），守护不碰它"
            else:
                rep.cause = (f"家宽组 {hb} 的成员不对（{', '.join(bad) or '组是空的'}）：成员必须是白名单里的节点，"
                             "守护不碰它，请检查死链配置")
            rep.recovered = False
            return rep
        with Confirm(self, ai, lambda: True) as confirm:
            states = self.probe_many(members, view)
            rep.steps.append("家宽节点：" + "，".join(
                f"{m} {s}ms" if isinstance(s, int) else f"{m} 测速接口出错" if isinstance(s, ProbeError) else f"{m} 不通"
                for m, s in states.items()))
            try:
                suspect = self.uplink_suspect([self.clash.match_target()])
            except Exception:  # 日常规则读不到：上行只是辅助判断，不影响家宽自己的处理
                suspect = False
            rep.cause = self.explain(chain, view, rep)
            # 只动家宽组，只在白名单里的家宽节点之间换；AI 组本身永远不改选
            self.heal_chain(rep, ai, chain, [hb], lambda v: unsafe, confirm, only=trusted)
        if rep.recovered is False:  # 这些都是本轮测出来的、随时会变：进摘要，不进原因（去重靠原因）
            alive = [m for m, s in states.items() if isinstance(s, int)]
            unknown = [m for m, s in states.items() if isinstance(s, ProbeError)]
            if alive:
                rep.blocked.append(f"家宽节点 {'、'.join(alive)} 能通，但这轮没换过去")
            elif unknown:
                rep.blocked.append(f"家宽节点 {'、'.join(unknown)} 测速接口出错，判断不了")
            else:
                rep.blocked.append("家宽节点都不通，AI 按设计保持断开、不回落到机场"
                                   + ("；直连检测地址和日常流量也不通，疑似本机上行断了" if suspect else ""))
        return rep

    def handle_no_core(self) -> Report:
        rep = Report(CORE, cause="mihomo 控制器连不上", recovered=False)
        try:
            running = subprocess.run(["pgrep", "-x", "clash-verge"], capture_output=True, timeout=10).returncode == 0
        except Exception as e:
            rep.cause += "；查不到 Clash Verge 的进程状态，先不动"
            rep.steps.append(f"pgrep 出错：{e}")
            return rep
        if running:
            rep.cause += "；Clash Verge 在运行，等它自己把内核拉起来"
            return rep
        if not self.cfg.reopen_verge:
            rep.cause += "；Clash Verge 没在运行（没开自动重开）"
            return rep
        rep.cause += "；Clash Verge 没在运行"  # 重开、退避这些本轮状态不进原因，免得每轮都算新故障
        if self.now() - self.last_reopen < REOPEN_BACKOFF:
            rep.blocked.append("刚尝试打开过，等它启动")
        elif self.dry_run:
            rep.actions.append("（演练，未执行）重新打开 Clash Verge")
        else:
            self.last_reopen = self.now()
            try:
                r = subprocess.run(["open", "-a", "Clash Verge"], capture_output=True, text=True, timeout=15)
                ok, err = r.returncode == 0, (r.stderr or "").strip() or str(r.returncode)
            except Exception as e:
                ok, err = False, str(e)
            if ok:
                rep.actions.append("已重新打开 Clash Verge（等内核起来）")
            else:
                rep.steps.append(f"重新打开 Clash Verge 失败：{err}")
                rep.blocked.append("重新打开失败")
        return rep

    # ---------- 一轮 ----------
    def guarded(self, path: str, handler: Callable[[], Report]) -> Report:
        with self.locked() as mine:
            if not mine:
                return Report(path, cause="另一个守护进程正在处理，这轮没下结论", undecided=True, down=False)
            return handler()

    def run_path(self, path: str, check: Callable[[], int | None | ProbeError],
                 handler: Callable[[], Report]) -> list[Report]:
        try:
            if isinstance(check(), int):
                self.forget(path)
                return []
            self.sleep(self.cfg.confirm_delay)
            second = check()
            if isinstance(second, int):  # 抖了一下，已经好了
                self.forget(path)
                return []
            if isinstance(second, ProbeError):  # 接口出错不等于不通：不动手，只记一笔
                return [Report(path, cause="测速接口出错，判断不了现在通不通，这轮不动手", steps=[str(second)],
                               undecided=True, down=False)]
            reps = [self.guarded(path, handler)]
            if reps[0].recovered is True:  # 处理完确认通了：这次故障结束
                self.forget(path)
            return reps
        except Exception as e:  # 一条链出错不影响另一条；读控制接口失败不等于不通，只记日志
            return [Report(path, cause=f"守护处理时出错：{e!r}", undecided=True, down=False)]

    def tick(self) -> list[Report]:
        self.last_checked = {CORE}
        if not self.clash.reachable():
            try:
                return [self.guarded(CORE, self.handle_no_core)]
            except Exception as e:
                return [Report(CORE, cause=f"守护处理时出错：{e!r}", undecided=True, down=False)]
        self.last_checked.add(DAILY)
        checked: dict[str, str] = {}

        def check_daily() -> int | None | ProbeError:
            checked["target"] = self.clash.match_target()
            return self.probe_state(checked["target"])
        reps = self.run_path(DAILY, check_daily, lambda: self.handle_daily(checked.get("target")))
        if self.now() - self.last_homebb >= self.cfg.homebb_interval:  # 家宽链测得疏一些
            self.last_homebb = self.now()
            self.last_checked.add(HOMEBB)
            reps += self.run_path(HOMEBB, lambda: self.probe_state(self.policy.ai_group), self.handle_homebb)
        return reps


def log_report(r: Report) -> None:
    W.log_line("[守护] " + r.summary())
    for s in r.steps:
        W.log_line("[守护]   · " + s)


class Announcer:
    """写日志、发通知。同一条链持续不通、诊断没变时只报一次（冷却、扫描进度这些本轮状态不算变化）；
    只有这一轮真测过、而且通了才报「恢复」。没下结论的（另一个守护正在处理、兜底规则刚变、测速接口出错、
    守护处理时出错）不算不通也不算恢复，只记日志、同样的话不重复记——但一条链持续没下结论超过 STUCK_ALERT 秒，
    提醒一次：守护自己可能坏了（比如锁文件被 root 占了），别让人以为它还在兜底。
    自己闪断又自己好了（守护没动手、之前也没报过不通）也只记日志，不弹通知。"""

    def __init__(self, cfg: GuardCfg, now: Callable[[], float] = time.monotonic) -> None:
        self.cfg, self.now = cfg, now
        self.down: dict[str, str] = {}
        self.quiet: dict[str, str] = {}  # 链 → 上次记下的「没下结论」
        self.stuck: dict[str, float] = {}  # 链 → 从什么时候起一直没下结论
        self.alerted: set[str] = set()

    def say(self, text: str) -> None:
        if self.cfg.notify:
            W.notify_macos("出口守护", text)

    def settle(self, path: str) -> None:
        """这条链有结论了（通了、不通、或者做了动作）：「一直没下结论」的计时作废。"""
        self.quiet.pop(path, None)
        self.stuck.pop(path, None)
        self.alerted.discard(path)

    def __call__(self, reps: list[Report], checked: set[str]) -> None:
        seen = {r.path for r in reps}
        for path in [p for p in checked if p not in seen]:
            self.settle(path)
        for path in [p for p in list(self.down) if p in checked and p not in seen]:
            self.down.pop(path)
            W.log_line(f"[守护] {path}已恢复")
            self.say(f"{path}已恢复")
        for r in reps:
            if r.undecided and not r.actions:
                if self.quiet.get(r.path) != r.summary():
                    self.quiet[r.path] = r.summary()
                    log_report(r)
                since = self.stuck.setdefault(r.path, self.now())
                if self.now() - since >= STUCK_ALERT and r.path not in self.alerted:
                    self.alerted.add(r.path)
                    text = f"{r.summary()}（已经连续 {int(self.now() - since)} 秒没下结论，守护可能没在正常工作，详见 watch.log）"
                    W.log_line("[守护] " + text)
                    self.say(text)
                continue
            self.settle(r.path)
            if r.recovered is True:
                announced = self.down.pop(r.path, None) is not None
                log_report(r)
                if r.actions or announced:
                    self.say(r.summary())
                continue
            if self.down.get(r.path) == r.cause and not r.actions:
                continue  # 诊断没变、这轮也没新动作：不重复记、不重复通知
            self.down[r.path] = r.cause
            log_report(r)
            self.say(r.summary())


def exit_code(reps: list[Report], dry_run: bool) -> int:
    """--once：没问题或者全都修好了才算成功（没下结论的不算）；--dry-run：发现问题就返回 1。"""
    if dry_run:
        return 1 if reps else 0
    return 0 if all(r.recovered is True and not r.undecided for r in reps) else 1


def print_report(r: Report) -> None:
    print(r.summary())
    for s in r.steps:
        print("  · " + s)


def explain_all(guard: Guard) -> int:
    """--explain：两条链各查各的，一条出错不影响另一条。"""
    if not guard.clash.reachable():
        print_report(guard.handle_no_core())
        return 1
    rc = 0
    for label, get_target, handler in ((DAILY, guard.clash.match_target, guard.handle_daily),
                                       (HOMEBB, lambda: guard.policy.ai_group, guard.handle_homebb)):
        try:
            target = get_target()
            d = guard.probe_state(target)
            if isinstance(d, ProbeError):
                print(f"{label}：测速接口出错，判断不了（{d}）")
                rc = 1
                continue
            if not d:
                print_report(handler())
                rc = 1
                continue
            view = guard.view()
            chain = W.follow_chain(view.proxies, target)
            print(f"{label}：通（{d}ms），走 {' → '.join(chain)}")
            for risk in guard.risks(chain, view.proxies):
                print("  ! " + risk)
        except Exception as e:
            print(f"{label}：查不了（{e!r}）")
            rc = 1
    return rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="出口守护：出口不通时分析原因并自动切到能通的出口")
    ap.add_argument("--once", action="store_true", help="测一轮，不通就处理，打印结果")
    ap.add_argument("--dry-run", action="store_true", help="测一轮并分析，只打印打算怎么做，不改任何东西")
    ap.add_argument("--explain", action="store_true", help="两条链现在走哪、通不通、有没有隐患（只读）")
    a = ap.parse_args(argv)
    cfg = SETTINGS.guard
    guard = Guard(Clash(cfg.timeout), cfg, W.DEFAULT_POLICY, dry_run=a.dry_run or a.explain,
                  lock_path=HERE / "guard.lock")
    if a.explain:
        return explain_all(guard)
    if a.once or a.dry_run:
        reps = guard.tick()
        for r in reps:
            print_report(r)
            if not a.dry_run and (r.actions or not r.undecided):
                log_report(r)
        if not reps:
            print("日常流量和家宽都通，不用处理")
        return exit_code(reps, a.dry_run)
    announce = Announcer(cfg)
    while True:
        try:
            announce(guard.tick(), guard.last_checked)
        except Exception as e:  # 常驻进程别因为一次异常退出；launchd 也会拉起，但会被节流
            W.log_line(f"[守护] 本轮出错：{e!r}")
        time.sleep(cfg.interval)


if __name__ == "__main__":
    sys.exit(main())
