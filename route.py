#!/usr/bin/env python3
"""看当前出口，改出口，并记住。

  python3 route.py              # 列出正在联网的 App 走的是家宽/直连/代理，选一个改掉
  python3 route.py list         # 已记住的覆盖规则
  python3 route.py set Telegram 家宽
  python3 route.py set github.com 直连
  python3 route.py remove Telegram
  python3 route.py apply        # 重新渲染规则集并让 mihomo 立刻生效
  python3 route.py check        # 检查 Clash 里有没有装好覆盖规则集
  python3 route.py hook         # 打印要贴进 Merge.yaml 的片段（手工配置的人用）

改动写进 routes.toml（记忆）和三个 mihomo 规则集文件，mihomo 监听文件即时生效，
不用解锁 Merge.yaml，也不会断开现有连接。AI 域名始终走家宽，覆盖不了。
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import routes as R  # noqa: E402
import ui  # noqa: E402
from config import SETTINGS  # noqa: E402
from egress import KIND_CN, classify, is_noise_host  # noqa: E402
from watch import api_json, clash_reachable  # noqa: E402


# ---------------- 当前出口 ----------------
def live_apps() -> list[dict]:
    """从连接表汇总：每个进程当前走的出口、样本域名。"""
    try:
        conns = api_json("/connections").get("connections") or []
    except Exception:
        return []
    by: dict[str, dict] = {}
    for c in conns:
        m = c.get("metadata") or {}
        proc = (m.get("process") or "").strip() or "(未知进程)"
        host = (m.get("host") or m.get("sniffHost") or m.get("destinationIP") or "").strip()
        kind = classify(c.get("chains") or [])
        e = by.setdefault(proc, {"process": proc, "kinds": collections.Counter(), "hosts": [], "n": 0})
        e["n"] += 1
        e["kinds"][kind] += 1
        if host and host not in e["hosts"] and not is_noise_host(host):
            e["hosts"].append(host)
    out = list(by.values())
    for e in out:
        ks = [k for k, _ in e["kinds"].most_common()]
        e["kind"] = ks[0] if len(ks) == 1 else "mixed"
        e["kind_cn"] = KIND_CN.get(e["kind"], e["kind"])
    out.sort(key=lambda e: -e["n"])
    return out


def installed() -> tuple[bool, str]:
    """Clash 里是否已经装好三条 RULE-SET 覆盖规则。"""
    if not clash_reachable():
        return False, "mihomo 没在跑"
    try:
        names = {str(p) for p in (api_json("/providers/rules").get("providers") or {})}
    except Exception:
        return False, "读不到 mihomo 的规则集列表"
    missing = [n for n in R.PROVIDER.values() if n not in names]
    if missing:
        return False, "Clash 里还没有覆盖规则集：" + ", ".join(missing)
    return True, ""


def apply_now(rs: list[R.Route]) -> None:
    """写规则集文件 + 让 mihomo 立刻重读。

    mihomo 也会自己监听文件变化（几秒内），PUT 只是把这件事立刻做掉，
    这样命令一返回、新连接就按新出口走。
    """
    files = R.write_providers(rs)
    ui.note(f"已写 {files['homebb'].parent}")
    if not clash_reachable():
        ui.note("mihomo 没在跑，下次启动后自动读取")
        return
    failed = []
    for name in R.PROVIDER.values():
        try:
            api_json(f"/providers/rules/{urllib.parse.quote(name)}", method="PUT")
        except Exception as e:
            failed.append(f"{name}（{e}）")
    if failed:
        ui.fail("让 mihomo 重读失败：" + "; ".join(failed))
        ui.note("文件已经写好，mihomo 几秒内会自己读到")
        return
    counts = {}
    try:
        for name, p in ((api_json("/providers/rules") or {}).get("providers") or {}).items():
            if name in R.PROVIDER.values():
                counts[name] = p.get("ruleCount")
    except Exception:
        pass
    want = {R.PROVIDER[t]: sum(1 for x in rs if x.target == t) for t in R.TARGETS}
    if counts == want:
        ui.success("已生效：" + "  ".join(f"{R.TARGET_CN[t]} {want[R.PROVIDER[t]]} 条" for t in R.TARGETS))
    elif counts:
        ui.fail(f"生效条数对不上：Clash 里 {counts}，应为 {want}")
    


# ---------------- 命令 ----------------
def cmd_list(rs: list[R.Route]) -> int:
    ui.title("已记住的出口覆盖")
    if not rs:
        ui.note("还没有。跑 python3 route.py 选一个 App 改掉，或 route.py set <App或域名> <家宽|直连|代理>")
        return 0
    for t in R.TARGETS:
        items = [r for r in rs if r.target == t]
        if items:
            ui.section(R.TARGET_CN[t])
            for r in items:
                ui.info(r.describe() + (ui.dim(f"   {r.note}") if r.note else ""))
    ok, why = installed()
    print()
    (ui.success if ok else ui.fail)("Clash 已装好覆盖规则集" if ok else why)
    if not ok:
        ui.note("跑 python3 route.py hook 看怎么装")
    return 0


def cmd_set(rs: list[R.Route], raw_match: str, raw_target: str) -> int:
    match = R.normalize_match(raw_match)
    target = R.parse_target(raw_target)
    err = R.validate(match, target)
    if err:
        ui.fail(err)
        return 2
    rs, old = R.upsert(rs, match, target)
    R.save_routes(rs)
    ui.success(f"{match}  →  {R.TARGET_CN[target]}" + (ui.dim(f"（原来是{R.TARGET_CN[old]}）") if old else ""))
    apply_now(rs)
    return 0


def cmd_remove(rs: list[R.Route], raw_match: str) -> int:
    match = R.normalize_match(raw_match)
    rs, hit = R.remove(rs, match)
    if not hit:
        ui.note(f"没有这条：{match}")
        return 1
    R.save_routes(rs)
    ui.success(f"已删除 {match}，恢复成默认分流")
    apply_now(rs)
    return 0


def cmd_status() -> int:
    ui.title("现在谁走哪")
    apps = live_apps()
    if not apps:
        ui.note("连接表是空的（mihomo 没在跑，或暂时没有出站）")
        return 1
    for e in apps[:20]:
        ui.info(f"{e['kind_cn']:<4} {e['process']:<28}" + ui.dim("  " + "  ".join(e["hosts"][:2])))
    return 0


def cmd_hook() -> int:
    ui.title("装进 Clash 的片段")
    ui.note("用 genconfig.py 生成配置的人不用管，生成器已经带上了。手工写 Merge.yaml 的人：")
    ui.note("把下面这段合进 Merge.yaml，三条 RULE-SET 必须排在 AI 域名规则之后，其余规则之前。")
    d = R.rules_dir()
    ai = SETTINGS.deadchain.ai_group
    print(f"""
proxy-providers 同级加：
rule-providers:
  user-homebb: {{type: file, behavior: classical, path: {d}/user-homebb.yaml}}
  user-direct: {{type: file, behavior: classical, path: {d}/user-direct.yaml}}
  user-daily:  {{type: file, behavior: classical, path: {d}/user-daily.yaml}}

prepend-rules 里，紧跟在 AI 规则之后：
- RULE-SET,user-homebb,{ai}
- RULE-SET,user-direct,DIRECT
- RULE-SET,user-daily,日常出口
""")
    ui.note(f"规则集文件在 {d}（route.py 会自动建）。Script.js 若过滤 RULE-SET，要放行 user-* 这三条。")
    return 0


def cmd_interactive(rs: list[R.Route]) -> int:
    ui.title("改出口")
    ok, why = installed()
    if not ok:
        ui.fail(why)
        ui.note("先按 python3 route.py hook 装好覆盖规则集，改了才会生效")
        if not ui.confirm("还是继续（只记下来，之后再生效）", False):
            return 2
    apps = live_apps()
    if not apps:
        ui.note("连接表是空的。可以直接用命令：route.py set <App或域名> <家宽|直连|代理>")
        return 1
    overridden = {r.payload: r for r in rs if r.kind == "PROCESS-NAME"}
    labels = []
    for e in apps[:20]:
        tag = ""
        if e["process"] in overridden:
            tag = f"  ← 已设为{R.TARGET_CN[overridden[e['process']].target]}"
        labels.append(f"{e['kind_cn']:<4} {e['process']:<26}{'  '.join(e['hosts'][:2])[:38]}{tag}")
    try:
        i = ui.choose(labels, "App")
        app = apps[i]
        hosts = app["hosts"][:6]
        what = ["整个 " + app["process"]] + [f"只改域名 {h}" for h in hosts] + ["手动输入域名"]
        j = ui.choose(what, "范围")
        if j == 0:
            raw = app["process"]
        elif j <= len(hosts):
            raw = hosts[j - 1]
        else:
            raw = ui.ask("域名", required=True)
        match = R.normalize_match(raw)
        cur = next((r for r in rs if r.match == match), None)
        ui.note(f"现在：{app['kind_cn']}" + (f"（已有覆盖：{R.TARGET_CN[cur.target]}）" if cur else ""))
        k = ui.choose([f"{R.TARGET_CN[t]}" for t in R.TARGETS], "出口")
        target = R.TARGETS[k]
        err = R.validate(match, target)
        if err:
            ui.fail(err)
            return 2
        ui.info(f"{match}  →  {R.TARGET_CN[target]}")
        if not ui.confirm("确认"):
            ui.note("没有改动")
            return 0
        rs, old = R.upsert(rs, match, target)
        R.save_routes(rs)
        ui.success("已记住" + (f"（原来是{R.TARGET_CN[old]}）" if old else ""))
        apply_now(rs)
        ui.note("已有连接不受影响，新连接立刻按新出口走。App 重连一下就看得到。")
        return 0
    except ui.Back:
        ui.note("已取消")
        return 0
    except (KeyboardInterrupt, EOFError):
        print()
        ui.note("已取消")
        return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="看当前出口、改出口、记住它")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("set", help="设定某个 App / 域名走哪个出口")
    p.add_argument("match", help="App 进程名、域名、IP，或完整规则如 PROCESS-NAME,Telegram")
    p.add_argument("target", help="家宽 / 直连 / 代理")
    sub.add_parser("remove", help="取消覆盖").add_argument("match")
    sub.add_parser("list", help="列出已记住的覆盖")
    sub.add_parser("status", help="现在谁走哪")
    sub.add_parser("apply", help="重新渲染规则集并生效")
    sub.add_parser("check", help="检查 Clash 里装好没有")
    sub.add_parser("hook", help="打印要贴进 Merge.yaml 的片段")
    a = ap.parse_args(argv)
    try:
        rs = R.load_routes()
        if a.cmd == "set":
            return cmd_set(rs, a.match, a.target)
        if a.cmd == "remove":
            return cmd_remove(rs, a.match)
        if a.cmd == "list":
            return cmd_list(rs)
        if a.cmd == "status":
            return cmd_status()
        if a.cmd == "apply":
            apply_now(rs)
            return 0
        if a.cmd == "check":
            ok, why = installed()
            (ui.success if ok else ui.fail)("已装好覆盖规则集" if ok else why)
            return 0 if ok else 1
        if a.cmd == "hook":
            return cmd_hook()
        return cmd_interactive(rs)
    except ValueError as e:
        ui.fail(str(e))
        return 2


if __name__ == "__main__":
    sys.exit(main())
