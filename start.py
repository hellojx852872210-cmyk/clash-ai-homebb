#!/usr/bin/env python3
"""环境检测 + 引导。第一次拿到项目就运行它。

  python3 start.py          # 检测环境并打印报告，然后按检测结果一步步带你配好（每步可跳过）
  python3 start.py --check  # 只检测、只打印报告，不做任何改动（提问题时把这段贴出来）
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from config import SETTINGS, expand_path  # noqa: E402

VERGE_APP = Path("/Applications/Clash Verge.app")
VERGE_DIR = expand_path("~/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev")
SPEC = Path(os.environ.get("CLASH_AI_HOMEBB_SPEC") or HERE / "deadchain.toml")
LABEL_WATCH = "io.github.clash-ai-homebb.watch"
LABEL_FLOAT = "io.github.clash-ai-homebb.float"
PY = sys.executable


# ---------------- 检测 ----------------
def _tcp_open(hostport: str, timeout: float = 1.5) -> bool:
    u = urlsplit(hostport if "://" in hostport else "http://" + hostport)
    try:
        with socket.create_connection((u.hostname or "127.0.0.1", u.port or 80), timeout=timeout):
            return True
    except OSError:
        return False


def _curl_ip(proxy: str, url: str, timeout: int = 10) -> str:
    try:
        r = subprocess.run(["curl", "-sS", "--max-time", str(timeout), "-x", proxy, url], capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else ""
    except FileNotFoundError:
        return ""


def _launchd_loaded(label: str) -> bool:
    r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"], capture_output=True, text=True)
    return r.returncode == 0


def _system_proxy_on() -> bool | None:
    r = subprocess.run(["scutil", "--proxy"], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return "HTTPEnable : 1" in r.stdout or "HTTPSEnable : 1" in r.stdout


def _has_module(name: str) -> bool:
    r = subprocess.run([PY, "-c", f"import {name}"], capture_output=True)
    return r.returncode == 0


def detect() -> dict:
    """只读检测，返回一个扁平字典；引导和报告都基于它。"""
    st: dict = {}
    st["os"] = platform.system() == "Darwin"
    st["os_detail"] = f"{platform.system()} {platform.mac_ver()[0] or platform.release()} {platform.machine()}"
    st["python"] = sys.version_info >= (3, 11)
    st["python_detail"] = f"{platform.python_version()} @ {PY}"
    st["pyobjc"] = _has_module("AppKit")
    st["pytest"] = _has_module("pytest")
    st["curl"] = shutil.which("curl") is not None
    st["verge_app"] = VERGE_APP.exists()
    st["verge_dir"] = (VERGE_DIR / "profiles").is_dir()
    sock = SETTINGS.clash.socket
    st["core_running"] = bool(sock and os.path.exists(sock)) or bool(SETTINGS.clash.controller and _tcp_open(SETTINGS.clash.controller))
    st["core_version"] = ""
    st["tun"] = None
    if st["core_running"]:
        try:
            from watch import api_json
            st["core_version"] = str(api_json("/version").get("version", ""))
            st["tun"] = bool(api_json("/configs").get("tun", {}).get("enable"))
        except Exception:
            st["core_running"] = False
    st["daily_port"] = _tcp_open(SETTINGS.clash.daily_proxy)
    st["homebb_port"] = _tcp_open(SETTINGS.clash.homebb_proxy)
    st["system_proxy"] = _system_proxy_on()
    merge = VERGE_DIR / "profiles" / "Merge.yaml"
    st["merge_exists"] = merge.exists()
    st["deadchain_installed"] = False
    if merge.exists():
        try:
            txt = merge.read_text(encoding="utf-8", errors="replace")
            st["deadchain_installed"] = ("clash-ai-homebb" in txt) or (SETTINGS.deadchain.ai_group in txt and SETTINGS.deadchain.homebb_group in txt)
        except OSError:
            pass
    pdir = VERGE_DIR / "ai-homebb-providers"
    st["providers"] = len(list(pdir.glob("*.yaml"))) if pdir.is_dir() else 0
    st["spec"] = SPEC.exists()
    st["generated"] = (HERE / "generated" / "Merge.yaml").exists()
    st["config"] = (HERE / "config.toml").exists()
    st["lock_json"] = (HERE / "lock.json").exists()
    st["lock_ok"] = None
    if st["lock_json"]:
        try:
            from watch import check_lock
            st["lock_ok"] = check_lock()[0]
        except Exception:
            st["lock_ok"] = False
    st["launchd_watch"] = _launchd_loaded(LABEL_WATCH)
    st["launchd_float"] = _launchd_loaded(LABEL_FLOAT)
    st["daily_ip"] = _curl_ip(SETTINGS.clash.daily_proxy, SETTINGS.probe.ip_url) if st["daily_port"] else ""
    st["homebb_ip"] = _curl_ip(SETTINGS.clash.homebb_proxy, SETTINGS.probe.ip_url) if st["homebb_port"] else ""
    st["watch_code"] = ""
    sp = HERE / "state.json"
    if sp.exists():
        try:
            st["watch_code"] = str(json.loads(sp.read_text()).get("code") or "")
        except Exception:
            pass
    return st


# ---------------- 报告 ----------------
def _mark(v) -> str:
    return "✓" if v is True else ("✗" if v is False else "–")


def render(st: dict) -> str:
    L = ["== 环境检测 =="]
    L.append(f" {_mark(st['os'])} macOS                {st['os_detail']}")
    L.append(f" {_mark(st['python'])} Python ≥ 3.11        {st['python_detail']}")
    L.append(f" {_mark(st['curl'])} curl")
    L.append(f" {_mark(st['pyobjc'])} PyObjC（悬浮窗用，可选）")
    L.append(f" {_mark(st['pytest'])} pytest（可选，没有就用 unittest）")
    L.append("== Clash Verge Rev ==")
    L.append(f" {_mark(st['verge_app'])} 已安装 {VERGE_APP}")
    L.append(f" {_mark(st['verge_dir'])} 数据目录 {VERGE_DIR}")
    L.append(f" {_mark(st['core_running'])} 内核在跑{('  mihomo ' + st['core_version']) if st['core_version'] else ''}")
    L.append(f" {_mark(st['tun'])} TUN 模式（进程规则需要）")
    L.append(f" {_mark(st['system_proxy'])} 系统代理已开（浏览器把域名交给 Clash，避开 ECH 绕过）")
    L.append(f" {_mark(st['daily_port'])} 日常口 {SETTINGS.clash.daily_proxy}{('  出口 ' + st['daily_ip']) if st['daily_ip'] else ''}")
    L.append(f" {_mark(st['homebb_port'])} 家宽口 {SETTINGS.clash.homebb_proxy}{('  出口 ' + st['homebb_ip']) if st['homebb_ip'] else ('  端口在但拨不通' if st['homebb_port'] else '')}")
    L.append("== 本项目 ==")
    L.append(f" {_mark(st['deadchain_installed'])} 死链已装进 Verge 的 Merge.yaml{'' if st['merge_exists'] else '（Merge.yaml 不存在）'}")
    L.append(f" {_mark(st['providers'] > 0)} 订阅副本 ai-homebb-providers/（{st['providers']} 份）")
    L.append(f" {_mark(st['spec'])} deadchain.toml（你的家宽/订阅描述）")
    L.append(f" {_mark(st['generated'])} generated/ 已生成")
    L.append(f" {_mark(st['config'])} config.toml（监控配置）")
    L.append(f" {_mark(st['lock_ok'] if st['lock_json'] else None)} 配置锁{'（' + ('完好' if st['lock_ok'] else '异常') + '）' if st['lock_json'] else '（未上锁）'}")
    L.append(f" {_mark(st['launchd_watch'])} launchd 监控任务    {_mark(st['launchd_float'])} launchd 悬浮窗任务")
    if st["watch_code"]:
        L.append(f" –  监控最近一轮：{st['watch_code']}")
    return "\n".join(L)


def plan(st: dict) -> list[tuple[str, str]]:
    """根据检测结果给出接下来要做的步骤（key, 说明）。纯函数，方便测试。"""
    steps: list[tuple[str, str]] = []
    if not st["os"]:
        return [("stop", "本项目只支持 macOS（launchd / chflags / osascript / AppKit）")]
    if not st["python"]:
        return [("stop", "需要 Python 3.11+：brew install python，然后用 python3 重新运行")]
    if not st["curl"]:
        return [("stop", "缺 curl：xcode-select --install 或 brew install curl")]
    if not st["verge_app"]:
        return [("stop", "先安装 Clash Verge Rev：https://github.com/clash-verge-rev/clash-verge-rev/releases ，导入你的机场订阅后再回来")]
    if not st["core_running"]:
        steps.append(("core", "打开 Clash Verge，确认内核已启动（设置里开 TUN 模式和系统代理）"))
    if not st["spec"]:
        steps.append(("wizard", "添加你的家宽节点/订阅和日常订阅（引导向导）"))
    if not st["generated"] or not st["spec"]:
        steps.append(("generate", "生成 Merge.yaml / Script.js / providers / config.toml 到 generated/"))
    if not st["deadchain_installed"]:
        steps.append(("install", "把生成结果装进 Clash Verge（自动备份原文件）"))
    steps.append(("activate", "在 Clash Verge 里重新激活配置档，Proxies 组选「日常出口」"))
    steps.append(("verify", "验证：家宽口出口应是你的家宽 IP，监控一轮应为「正常」"))
    if not st["lock_json"] or st["lock_ok"] is False:
        steps.append(("pin", "上锁：给 Merge / Script / 订阅副本打不可变标记"))
    if not st["launchd_watch"]:
        steps.append(("launchd", "安装 launchd：监控每 3 分钟一轮 + 悬浮窗常驻"))
    return steps


# ---------------- 引导 ----------------
def _yes(prompt: str, default: bool = True) -> bool:
    s = input(f"{prompt} {'Y/n' if default else 'y/N'}: ").strip().lower()
    if not s:
        return default
    return s.startswith("y")


def _run(argv: list[str]) -> int:
    print("  $ " + " ".join(argv))
    return subprocess.run(argv, cwd=HERE).returncode


def guide(st: dict) -> int:
    steps = plan(st)
    if steps and steps[0][0] == "stop":
        print("\n先解决这个再继续：" + steps[0][1])
        return 2
    print("\n== 接下来 ==")
    for i, (_, text) in enumerate(steps, 1):
        print(f" {i}. {text}")
    print("\n每一步都会先问你，回车默认执行，n 跳过，Ctrl-C 随时退出。\n")
    done: list[str] = []
    try:
        for key, text in steps:
            print(f"--- {text}")
            if key == "core":
                input("  打开 Clash Verge 并启动内核后按回车重新检测…")
                st = detect()
                if not st["core_running"]:
                    print("  内核还是没起来，先在 Clash Verge 里处理好再重跑 start.py")
                    return 2
                print("  内核已在运行")
            elif key == "wizard":
                if _yes("  现在进向导添加？"):
                    import wizard
                    wizard.menu(SPEC)
                    if not SPEC.exists():
                        print("  没有保存 deadchain.toml，先到这里；下次重跑 start.py 继续")
                        return 1
            elif key == "generate":
                if _yes("  生成到 generated/？"):
                    if _run([PY, "genconfig.py", str(SPEC), "--out", "generated"]) != 0:
                        return 2
            elif key == "install":
                print("  会覆盖 Clash Verge 里的 Merge.yaml / Script.js（原文件备份为 *.bak-时间戳），并把 config.toml 放到项目目录")
                if _yes("  安装？", False):
                    if (HERE / "lock.json").exists():
                        _run([PY, "watch.py", "--unpin"])
                    if _run([PY, "genconfig.py", str(SPEC), "--out", "generated", "--install", "--yes"]) != 0:
                        return 2
                else:
                    print("  跳过。之后手动：python3 genconfig.py deadchain.toml --out generated --install --yes，或按 generated/INSTALL.md 粘贴")
            elif key == "activate":
                print("  到 Clash Verge：切换一下配置档（让它按新 Merge/Script 重新生成），然后 Proxies 组选「日常出口」。")
                input("  做完按回车…")
            elif key == "verify":
                ip = _curl_ip(SETTINGS.clash.homebb_proxy, SETTINGS.probe.ip_url)
                print(f"  家宽口出口 IP：{ip or '拨不通'}")
                _run([PY, "watch.py"])
                if not ip:
                    print("  家宽拨不通：检查节点信息、Verge 是否已重新生成、Proxies 是否选了「日常出口」；修好后重跑 start.py")
            elif key == "pin":
                if _yes("  上锁？"):
                    _run([PY, "watch.py", "--pin"])
            elif key == "launchd":
                if _yes("  安装 launchd 任务？"):
                    _run(["bash", str(HERE / "install.sh")])
            done.append(key)
            print()
    except (KeyboardInterrupt, EOFError):
        print("\n已中断。已完成：" + (", ".join(done) or "无") + "。重跑 start.py 会从当前状态继续。")
        return 1
    print("== 完成 ==")
    print(render(detect()))
    print("\n日常：python3 watch.py 看一轮；python3 wizard.py 增删节点/订阅后重新生成并安装（先 --unpin 再 --pin）。")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="环境检测 + 引导")
    ap.add_argument("--check", action="store_true", help="只检测、打印报告，不做改动")
    a = ap.parse_args(argv)
    st = detect()
    print(render(st))
    if a.check:
        print("\n== 建议 ==")
        for i, (_, text) in enumerate(plan(st), 1):
            print(f" {i}. {text}")
        return 0
    return guide(st)


if __name__ == "__main__":
    sys.exit(main())
