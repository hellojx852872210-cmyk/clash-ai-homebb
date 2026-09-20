#!/usr/bin/env python3
"""Clash「AI 走家宽」死链监控（fail-closed）。

约束：
  * 日常出口（daily_proxy，任何能打通的订阅）必须通；
  * AI 域名 / 家宽入站口（homebb_proxy）只允许走家宽组，家宽挂了就是断线，绝不回落到日常或直连；
  * 家宽出口 IP 和日常出口 IP 不能相同（撞 IP 就是死链失效）。
本脚本只探测、判级、告警，不改任何路由；路由在 Clash 的 Merge/Script 里（见 examples/）。

告警只走本机：状态切换时通知 + 模态框；持续非正常每 realert_secs 再提醒；抖动类状态先观察一轮。
配置锁：--pin 给 lock.files 打 uchg 不可变标记并记 sha256，每轮校验，丢标记/改内容 → config_tampered。
所有可变项见 config.toml（config.example.toml 有注释）。
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import ipaddress
import json
import os
import socket
import stat
import subprocess
import sys
import time
import urllib.parse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import SETTINGS, Settings, expand_files

HERE = Path(__file__).resolve().parent
STATE_PATH = HERE / "state.json"
LOG_PATH = HERE / "watch.log"
LOCK_PATH = HERE / "lock.json"

LEVEL_CN = {"ok": "正常", "warn": "警告", "crit": "严重"}
CODE_CN = {
    "ok": "家宽与日常出口不同，死链完整",
    "homebb_down": "家宽不通，AI 已 fail-closed（没有掉到日常出口）",
    "leak_homebb_is_daily": "家宽口的出口等于日常出口，死链失效",
    "leak_ai_via_daily": "AI 域名经日常口走通了，链路里没有 AI 组",
    "clash_dead": "mihomo 控制器连不上",
    "core_reloading": "内核正在重载（控制器暂时没有返回 AI 组），下一轮再看",
    "deadchain_broken": "死链结构被改（AI 组 / 家宽组不是预期形态）",
    "config_tampered": "上锁的配置被改或锁标记丢了",
    "direct_route_missing": "自家机直连的 TUN exclude / 网卡路由丢了",
    "daily_down": "日常出口出不了网（所有订阅都不通）",
}
CODE_SHORT = {
    "ok": "正常",
    "homebb_down": "家宽断线，AI 已断（未漏）",
    "leak_homebb_is_daily": "泄漏！家宽口出口=日常出口",
    "leak_ai_via_daily": "泄漏！AI 走了日常出口",
    "clash_dead": "mihomo 挂了",
    "core_reloading": "内核重载中",
    "deadchain_broken": "死链结构被改",
    "config_tampered": "配置被改/锁标记丢",
    "direct_route_missing": "自家机直连路由丢",
    "daily_down": "日常出口断线（订阅全不通）",
}


@dataclass(frozen=True)
class Policy:
    """evaluate() 用到的策略参数，全部来自 config；测试里可以直接构造。"""

    ai_group: str = "Claude-Only"
    homebb_group: str = "RESIP-Claude"
    homebb_group_type: str = "Fallback"
    homebb_members: tuple[str, ...] = ("RESIP-A", "RESIP-B")  # 空 = 成员来自 provider，只查非空
    homebb_member_type: str = "Vless"  # 空 = 不查类型
    direct_ips: tuple[str, ...] = ()
    uplink_interface: str = "en0"

    @classmethod
    def from_settings(cls, s: Settings) -> "Policy":
        return cls(
            ai_group=s.deadchain.ai_group,
            homebb_group=s.deadchain.homebb_group,
            homebb_group_type=s.deadchain.homebb_group_type,
            homebb_members=tuple(s.deadchain.homebb_members),
            homebb_member_type=s.deadchain.homebb_member_type,
            direct_ips=tuple(s.probe.direct_ips),
            uplink_interface=s.probe.uplink_interface,
        )


DEFAULT_POLICY = Policy.from_settings(SETTINGS)


@dataclass(frozen=True)
class Snapshot:
    clash_up: bool
    ai_group_type: str
    ai_group_all: tuple[str, ...]
    ai_group_now: str
    tun_exclude: tuple[str, ...]
    direct_ifaces: dict[str, str]
    daily_ip: str | None
    homebb_ip: str | None
    ai_via_daily: str  # "ok" = AI 探测 URL 经日常口有响应；"fail_closed" = 没响应
    ai_chain: str | None
    homebb_type: str = "Fallback"
    homebb_all: tuple[str, ...] = DEFAULT_POLICY.homebb_members
    homebb_member_types: tuple[str, ...] = tuple(
        DEFAULT_POLICY.homebb_member_type for _ in DEFAULT_POLICY.homebb_members
    )
    lock_ok: bool = True
    lock_detail: str = ""


@dataclass(frozen=True)
class Result:
    code: str
    level: str
    detail: str = ""

    def should_alert(self, previous_code: str | None) -> bool:
        return previous_code != self.code

    def summary(self) -> str:
        return f"[{LEVEL_CN.get(self.level, self.level)}] {CODE_CN.get(self.code, self.code)}" + (
            f"：{self.detail}" if self.detail else ""
        )


def should_notify(
    result: Result,
    previous_code: str | None,
    last_alert_ts: float,
    now: float,
    alerted_code: str | None = None,
    realert_secs: int = SETTINGS.alert.realert_secs,
    debounce: tuple[str, ...] = SETTINGS.alert.debounce_codes,
) -> bool:
    """与上次告过警的状态不同就报（抖动类状态先观察一轮）；持续非正常每 realert_secs 再报一次。"""
    if alerted_code is None:
        alerted_code = previous_code
    if result.code in debounce and previous_code != result.code:
        return False
    if result.code != alerted_code:
        return True
    return result.level != "ok" and (now - last_alert_ts) >= realert_secs


def evaluate(snapshot: Snapshot, policy: Policy | None = None) -> Result:
    p = policy or DEFAULT_POLICY
    if not snapshot.clash_up:
        return Result("clash_dead", "crit")
    if not snapshot.lock_ok:
        return Result("config_tampered", "crit", snapshot.lock_detail)
    if not snapshot.ai_group_type and not snapshot.ai_group_all:
        # 组整个不在：多半是 Verge 切换配置档 / 重新生成时内核正在重载，不是被改
        return Result("core_reloading", "warn", f"{p.ai_group} 不在控制器返回的组表里")
    if snapshot.daily_ip and snapshot.homebb_ip and snapshot.daily_ip == snapshot.homebb_ip:
        return Result("leak_homebb_is_daily", "crit", f"两侧都是 {snapshot.daily_ip}")
    if snapshot.ai_via_daily == "ok" and (not snapshot.ai_chain or p.ai_group not in snapshot.ai_chain):
        return Result("leak_ai_via_daily", "crit", snapshot.ai_chain or "chain 空")
    members = tuple(snapshot.ai_group_all)
    if snapshot.ai_group_type != "Selector" or members != (p.homebb_group,):
        return Result("deadchain_broken", "crit", f"{p.ai_group} {snapshot.ai_group_type} {members}")
    hb = tuple(snapshot.homebb_all)
    members_ok = hb == tuple(p.homebb_members) if p.homebb_members else bool(hb)
    types_ok = (
        all(t == p.homebb_member_type for t in snapshot.homebb_member_types)
        if p.homebb_member_type
        else True
    )
    if snapshot.homebb_type != p.homebb_group_type or not members_ok or not types_ok:
        return Result(
            "deadchain_broken",
            "crit",
            f"{p.homebb_group} {snapshot.homebb_type} {hb} {tuple(snapshot.homebb_member_types)}",
        )
    missing: list[str] = []
    exclude = set(snapshot.tun_exclude)
    for ip in p.direct_ips:
        if f"{ip}/32" not in exclude:
            missing.append(f"{ip} 不在 exclude")
        elif snapshot.direct_ifaces.get(ip) != p.uplink_interface:
            missing.append(f"{ip}→{snapshot.direct_ifaces.get(ip) or '?'}")
    if missing:
        return Result("direct_route_missing", "crit", "; ".join(missing))
    if not snapshot.daily_ip:
        return Result("daily_down", "crit")
    if not snapshot.homebb_ip:
        return Result("homebb_down", "warn")
    return Result("ok", "ok", f"家宽 {snapshot.homebb_ip} / 日常 {snapshot.daily_ip}")


# ---------------- mihomo API ----------------
class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, sockpath: str, timeout: float = 5.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self.sockpath = sockpath

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.sockpath)
        self.sock = sock


def clash_reachable() -> bool:
    c = SETTINGS.clash
    return bool((c.socket and os.path.exists(c.socket)) or c.controller)


def api_json(path: str, timeout: float = 5.0, method: str = "GET") -> Any:
    """请求 mihomo 控制器；unix socket 优先，其次 TCP controller（带 secret）。

    空响应体（例如 PUT /providers/rules/<name> 的 204）返回 None。
    """
    c = SETTINGS.clash
    headers = {}
    if c.socket and os.path.exists(c.socket):
        conn: http.client.HTTPConnection = UnixHTTPConnection(c.socket, timeout=timeout)
    elif c.controller:
        host, _, port = c.controller.rpartition(":")
        conn = http.client.HTTPConnection(host or "127.0.0.1", int(port or 9090), timeout=timeout)
        if c.secret:
            headers["Authorization"] = f"Bearer {c.secret}"
    else:
        raise RuntimeError("没有可用的 mihomo 控制器（clash.socket / clash.controller 都为空）")
    try:
        conn.request(method, path, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        if resp.status >= 300:
            raise RuntimeError(f"{path} HTTP {resp.status}")
        text = body.decode().strip()
        return json.loads(text) if text else None
    finally:
        conn.close()


unix_json = api_json  # 兼容旧调用


# ---------------- 探针 ----------------
def _parse_ip(text: str) -> str | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        ip = ipaddress.ip_address(text.split()[0])
    except ValueError:
        return None
    if ip.is_private or ip.is_loopback or ip.is_link_local:
        return None
    return str(ip)


def curl_ip(proxy: str, timeout: int | None = None) -> str | None:
    t = timeout or SETTINGS.probe.curl_timeout
    proc = subprocess.run(
        ["curl", "-sS", "--max-time", str(t), "-x", proxy, SETTINGS.probe.ip_url],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    return _parse_ip(proc.stdout)


def curl_http_code(proxy: str, url: str, timeout: int | None = None) -> str:
    t = timeout or SETTINGS.probe.curl_timeout
    proc = subprocess.run(
        ["curl", "-sS", "--max-time", str(t), "-o", "/dev/null", "-w", "%{http_code}", "-x", proxy, url],
        capture_output=True,
        text=True,
    )
    code = (proc.stdout or "").strip()
    return code if code else "000"


def route_iface(ip: str) -> str:
    proc = subprocess.run(["route", "-n", "get", ip], capture_output=True, text=True)
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("interface:"):
            return line.split(":", 1)[1].strip()
    return ""


def ai_chain_from_connections() -> str | None:
    try:
        data = api_json("/connections")
    except Exception:
        return None
    keys = tuple(k.lower() for k in SETTINGS.probe.ai_hosts)
    for conn in reversed(data.get("connections") or []):
        meta = conn.get("metadata") or {}
        host = (meta.get("host") or meta.get("destinationIP") or "").lower()
        if not any(k in host for k in keys):
            continue
        chains = conn.get("chains") or []
        if chains:
            return "/".join(str(x) for x in chains)
    return None


def probe(policy: Policy | None = None) -> Snapshot:
    p = policy or DEFAULT_POLICY
    c = SETTINGS.clash
    clash_up = clash_reachable()
    ai_type = ""
    ai_all: tuple[str, ...] = ()
    ai_now = ""
    tun_exclude: tuple[str, ...] = ()
    hb_type = ""
    hb_all: tuple[str, ...] = ()
    hb_member_types: tuple[str, ...] = ()
    if clash_up:
        try:
            configs = api_json("/configs")
            tun_exclude = tuple(configs.get("tun", {}).get("route-exclude-address") or [])
            proxies = api_json("/proxies").get("proxies") or {}
            g = proxies.get(p.ai_group) or {}
            ai_type = str(g.get("type") or "")
            ai_all = tuple(str(x) for x in (g.get("all") or ()))
            ai_now = str(g.get("now") or "")
            hg = proxies.get(p.homebb_group) or {}
            hb_type = str(hg.get("type") or "")
            hb_all = tuple(str(x) for x in (hg.get("all") or ()))
            hb_member_types = tuple(str((proxies.get(n) or {}).get("type") or "") for n in hb_all)
        except Exception:
            clash_up = False
    lock_ok, lock_detail = check_lock()
    direct_ifaces = {ip: route_iface(ip) for ip in p.direct_ips}
    daily_ip = curl_ip(c.daily_proxy) if clash_up else None
    homebb_ip = curl_ip(c.homebb_proxy) if clash_up else None
    ai_via = "fail_closed"
    chain = None
    if clash_up:
        code = curl_http_code(c.daily_proxy, SETTINGS.probe.ai_probe_url)
        if code not in ("", "000"):
            ai_via = "ok"
        chain = ai_chain_from_connections()
    return Snapshot(
        clash_up=clash_up,
        ai_group_type=ai_type,
        ai_group_all=ai_all,
        ai_group_now=ai_now,
        tun_exclude=tun_exclude,
        direct_ifaces=direct_ifaces,
        daily_ip=daily_ip,
        homebb_ip=homebb_ip,
        ai_via_daily=ai_via,
        ai_chain=chain,
        homebb_type=hb_type,
        homebb_all=hb_all,
        homebb_member_types=hb_member_types,
        lock_ok=lock_ok,
        lock_detail=lock_detail,
    )


# ---------------- 配置锁 ----------------
def locked_files() -> tuple[Path, ...]:
    return expand_files(SETTINGS.lock.files)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def is_immutable(path: Path) -> bool:
    try:
        return bool(path.stat().st_flags & stat.UF_IMMUTABLE)
    except (OSError, AttributeError):
        return False


def check_lock() -> tuple[bool, str]:
    """lock.json 记的每个文件：必须存在、带 uchg、sha256 一致；且清单要和 config 一致。"""
    configured = locked_files()
    if not configured:
        return True, ""
    if not LOCK_PATH.exists():
        return False, "lock.json 不存在，先跑 watch.py --pin"
    try:
        expected = json.loads(LOCK_PATH.read_text()).get("files") or {}
    except json.JSONDecodeError:
        return False, "lock.json 损坏"
    if not expected:
        return False, "lock.json 没记任何文件"
    problems: list[str] = []
    if {str(p) for p in configured} != set(expected):
        problems.append("锁清单与 config 不一致，重新 --pin")
    for raw, sha in expected.items():
        p = Path(raw)
        name = p.name
        if not p.exists():
            problems.append(f"{name} 不存在")
            continue
        if not is_immutable(p):
            problems.append(f"{name} 锁标记(uchg)丢了")
        if file_sha256(p) != sha:
            problems.append(f"{name} 内容变了")
    return (not problems), "; ".join(problems)


def pin_lock() -> int:
    files: dict[str, str] = {}
    targets = locked_files()
    if not targets:
        print("config 的 [lock].files 为空，没有要锁的文件")
        return 1
    for p in targets:
        if not p.exists():
            print(f"缺文件: {p}")
            return 1
        files[str(p)] = file_sha256(p)
        os.chflags(p, p.stat().st_flags | stat.UF_IMMUTABLE)
        print(f"锁定 {p.name}  uchg=on  sha256={files[str(p)][:16]}…")
    LOCK_PATH.write_text(
        json.dumps({"pinned_at": datetime.now(timezone.utc).isoformat(), "files": files}, ensure_ascii=False, indent=2)
        + "\n"
    )
    print(f"已写 {LOCK_PATH}")
    return 0


def unpin_lock() -> int:
    for p in locked_files():
        if p.exists():
            os.chflags(p, p.stat().st_flags & ~stat.UF_IMMUTABLE)
            print(f"解锁 {p.name}  uchg=off（改完记得 --pin 重新锁）")
    return 0


# ---------------- 状态 / 日志 / 告警 ----------------
def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except json.JSONDecodeError:
        return {}


def save_state(result: Result, snap: Snapshot, last_alert_ts: float = 0.0, alerted_code: str | None = None) -> None:
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "code": result.code,
        "level": result.level,
        "detail": result.detail,
        "last_alert_ts": last_alert_ts,
        "alerted_code": alerted_code,
        "lock_ok": snap.lock_ok,
        "lock_detail": snap.lock_detail,
        "daily_ip": snap.daily_ip,
        "homebb_ip": snap.homebb_ip,
        "ai_via_daily": snap.ai_via_daily,
        "ai_chain": snap.ai_chain,
        "ai_group": {"type": snap.ai_group_type, "all": list(snap.ai_group_all), "now": snap.ai_group_now},
        "homebb_group": {
            "type": snap.homebb_type,
            "all": list(snap.homebb_all),
            "member_types": list(snap.homebb_member_types),
        },
        "tun_exclude": list(snap.tun_exclude),
        "direct_ifaces": snap.direct_ifaces,
    }
    STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def log_line(msg: str) -> None:
    with LOG_PATH.open("a") as f:
        f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


def notify_macos(title: str, body: str, modal: bool = False) -> None:
    a = SETTINGS.alert
    script = (
        "display notification " + json.dumps(body, ensure_ascii=False)
        + " with title " + json.dumps(title, ensure_ascii=False)
        + (f' sound name "{a.sound}"' if a.sound else "")
    )
    subprocess.run(["osascript", "-e", script], capture_output=True)
    if modal and a.modal:
        # 模态框 2 分钟自动消失；新会话组，免得 launchd 在本进程退出时连带杀掉它
        script2 = (
            "display alert " + json.dumps(title, ensure_ascii=False)
            + " message " + json.dumps(body, ensure_ascii=False)
            + " as critical giving up after 120"
        )
        subprocess.Popen(
            ["osascript", "-e", script2],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


def notify_telegram(text: str) -> None:
    a = SETTINGS.alert
    token = os.environ.get(a.telegram_token_env) if a.telegram_token_env else None
    chat = os.environ.get(a.telegram_chat_env) if a.telegram_chat_env else None
    if not token or not chat:
        return
    body = urllib.parse.urlencode({"chat_id": chat, "text": text[:900], "disable_web_page_preview": "true"}).encode()
    subprocess.run(
        ["curl", "-sS", "--max-time", "12", "-x", SETTINGS.clash.daily_proxy,
         f"https://api.telegram.org/bot{token}/sendMessage", "--data-binary", "@-"],
        input=body, capture_output=True, check=False,
    )


def alert(result: Result, first: bool = True) -> None:
    body = result.summary() if first else "仍未恢复：" + result.summary()
    notify_macos(SETTINGS.alert.title, body, modal=(first and result.level != "ok"))
    if result.level == "crit":
        notify_telegram(f"{SETTINGS.alert.title} {body}")


def run_once(*, as_json: bool) -> int:
    snap = probe()
    result = evaluate(snap)
    state = load_state()
    prev = state.get("code")
    alerted = state.get("alerted_code") or prev
    try:
        last_alert = float(state.get("last_alert_ts") or 0.0)
    except (TypeError, ValueError):
        last_alert = 0.0
    now = time.time()
    log_line(result.summary())
    if should_notify(result, prev, last_alert, now, alerted_code=alerted):
        first = result.code != alerted
        alert(result, first=first)
        last_alert = now
        log_line(f"alert previous={prev!r} -> {result.code}" + ("" if first else " (重复提醒)"))
        alerted = result.code
    save_state(result, snap, last_alert_ts=last_alert, alerted_code=alerted)
    if as_json:
        print(json.dumps({"result": asdict(result), "snapshot": asdict(snap), "previous": prev}, ensure_ascii=False, indent=2))
    else:
        print(result.summary())
        print(f"日常 {snap.daily_ip or '-'} | 家宽 {snap.homebb_ip or '-'} | ai/{snap.ai_via_daily} chain={snap.ai_chain or '-'}")
    return 0 if result.level != "crit" else 2


def print_config() -> int:
    print(f"config: {SETTINGS.config_path or '(内置默认)'}")
    print(json.dumps(asdict(SETTINGS), ensure_ascii=False, indent=2))
    print("locked files:", [str(p) for p in locked_files()])
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Clash「AI 走家宽」死链监控")
    p.add_argument("--json", action="store_true", help="打印完整快照 JSON")
    p.add_argument("--loop", action="store_true", help="前台循环（默认只跑一轮，给 launchd StartInterval 用）")
    p.add_argument("--interval", type=int, default=180, help="--loop 间隔秒")
    p.add_argument("--pin", action="store_true", help="给 [lock].files 打 uchg 锁并记录 sha256")
    p.add_argument("--unpin", action="store_true", help="解除 uchg 锁（改完要再 --pin）")
    p.add_argument("--print-config", action="store_true", help="打印生效配置")
    args = p.parse_args(argv)
    if args.print_config:
        return print_config()
    if args.pin:
        return pin_lock()
    if args.unpin:
        return unpin_lock()
    if args.loop:
        while True:
            run_once(as_json=args.json)
            time.sleep(max(30, args.interval))
    return run_once(as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())
