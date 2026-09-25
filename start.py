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
import verge  # noqa: E402
from config import SETTINGS, expand_path  # noqa: E402

VERGE_APP = Path("/Applications/Clash Verge.app")
VERGE_DIR = expand_path(SETTINGS.clash.verge_dir)
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
    st["core_version"] = ""
    st["controller"] = ""
    st["tun"] = None
    try:
        from watch import api_json, find_controller
        ctl = find_controller()
    except Exception:
        ctl = None
    st["core_running"] = ctl is not None
    st["verge_error"] = verge.service_failure(verge.read_log_tail(VERGE_DIR))
    if ctl is not None:
        st["controller"] = ctl.label()
        if ctl.is_service:
            st["verge_error"] = ""  # 内核就在服务模式，日志里的失败是旧事
        try:
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
import ui  # noqa: E402


def _mark(v) -> str:
    return ui.mark(v)


def render(st: dict) -> str:
    """检测报告。✗ 的项目后面直接写该怎么办。"""
    L: list[str] = []
    m = ui.mark

    def row(v, label, ok_detail="", bad_detail=""):
        d = ok_detail if v else bad_detail
        L.append(f"  {m(v)} {label}" + (ui.dim(f"  {d}") if d else ""))

    L.append(ui.bold("  环境"))
    row(st["os"], "macOS", st["os_detail"], "本项目只支持 macOS")
    row(st["python"], "Python 3.11+", st["python_detail"], f"当前 {st['python_detail']}，brew install python")
    row(st["curl"], "curl", "", "xcode-select --install")
    row(st["pyobjc"] or None, "悬浮窗依赖 PyObjC", "", "可选：pip install pyobjc-framework-Cocoa")
    row(st["pytest"] or None, "pytest", "", "可选，没有就用 unittest")
    L.append("")
    L.append(ui.bold("  Clash Verge Rev"))
    row(st["verge_app"], "已安装", str(VERGE_APP), "去 github.com/clash-verge-rev/clash-verge-rev/releases 下载")
    row(st["core_running"], "内核在跑", f"mihomo {st['core_version']}  {st.get('controller', '')}".strip(), "打开 Clash Verge 并启动内核")
    if st.get("verge_error"):
        row(False, "服务模式启动内核", "", f"{st['verge_error'][:120]}；{verge.failure_hint(st['verge_error'])}")
    row(st["tun"], "TUN 模式", "", "设置里打开，按进程分流需要它")
    row(st["system_proxy"], "系统代理", "", "设置里打开，浏览器会把域名交给 Clash")
    row(st["daily_port"], f"日常出口 {SETTINGS.clash.daily_proxy}", f"出口 IP {st['daily_ip']}" if st["daily_ip"] else "端口在，暂时拨不通", "端口没开")
    row(st["homebb_port"], f"家宽入口 {SETTINGS.clash.homebb_proxy}", f"出口 IP {st['homebb_ip']}" if st["homebb_ip"] else "端口在，家宽暂时拨不通", "还没装死链，端口不存在")
    L.append("")
    L.append(ui.bold("  本项目"))
    row(st["deadchain_installed"], "死链已装进 Clash Verge", "", "还没装" if st["merge_exists"] else "Merge.yaml 不存在")
    row(st["providers"] > 0 or None, "订阅副本", f"{st['providers']} 份" if st["providers"] else "", "")
    row(st["spec"], "deadchain.toml", "", "还没记录你的家宽和订阅")
    row(st["generated"], "generated/", "已生成", "还没生成")
    row(st["config"], "config.toml", "", "安装时会自动放好")
    row(st["lock_ok"] if st["lock_json"] else None, "配置锁", "完好" if st["lock_ok"] else "", "锁异常，重新 --pin" if st["lock_json"] else "未上锁")
    row(st["launchd_watch"], "后台监控 launchd", "", "未安装")
    row(st["launchd_float"] or None, "悬浮窗 launchd", "", "未安装（可选）")
    if st["watch_code"]:
        L.append(f"  {ui.NA} 监控最近一轮" + ui.dim(f"  {st['watch_code']}"))
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
    if st.get("verge_error"):
        steps.append(("service", "Verge 服务模式起不来内核，TUN 用不了：" + verge.failure_hint(st["verge_error"])))
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


# 把细步骤归成人看得懂的几大步：(标题, 一句话说明, 包含的细步骤)
STAGES: list[tuple[str, str, tuple[str, ...]]] = [
    ("准备 Clash Verge", "确认内核已经启动，设置里打开 TUN 模式和系统代理。", ("core", "service")),
    ("添加家宽和订阅", "打开向导，把你的家宽 IP/节点和平时用的机场订阅记下来。", ("wizard",)),
    ("生成并安装", "按记录生成 Clash 配置，装进 Clash Verge（原文件会备份），再在 Verge 里激活。", ("generate", "install", "activate")),
    ("验证", "看家宽入口的出口 IP 是不是你的家宽，监控跑一轮应为「正常」。", ("verify",)),
    ("上锁与后台监控", "给配置打不可变标记防误改，装上每 3 分钟一轮的监控和悬浮窗。", ("pin", "launchd")),
]


def group_steps(steps: list[tuple[str, str]]) -> list[tuple[str, str, list[str]]]:
    """按 STAGES 顺序把 plan() 的细步骤归组，空的组不出现。"""
    keys = [k for k, _ in steps]
    out = []
    for title, desc, members in STAGES:
        present = [k for k in members if k in keys]
        if present:
            out.append((title, desc, present))
    return out


# ---------------- 引导 ----------------
def _run(argv: list[str]) -> int:
    ui.note("$ " + " ".join(argv))
    sys.stdout.flush()  # 子进程直接写 fd，先把我们的输出刷出去，顺序才对
    return subprocess.run(argv, cwd=HERE).returncode


def _do(key: str, st: dict) -> tuple[bool, dict]:
    """执行一个细步骤；返回 (是否继续引导, 最新检测结果)。"""
    if key == "core":
        ui.pause("打开 Clash Verge 并启动内核后，按回车重新检测")
        st = detect()
        if not st["core_running"]:
            ui.fail("内核还是没起来。先在 Clash Verge 里处理好，再重跑 python3 start.py")
            return False, st
        ui.success("内核已在运行")
    elif key == "service":
        ui.fail("Verge 服务模式起不来内核：" + st["verge_error"][:160])
        if "Operation not permitted" in st["verge_error"]:
            ui.note("推荐 y：只解开 Verge 服务要复制的订阅副本上的 uchg，sha256 校验照旧，Merge/Script 仍然上锁")
            if ui.confirm("迁移配置锁", True):
                _run([PY, "watch.py", "--migrate-lock"])
        ui.info("然后在 Clash Verge 里重新打开「虚拟网卡模式」（不行就 设置 → 服务模式 → 重装）")
        ui.pause("做完按回车")
    elif key == "wizard":
        import wizard
        wizard.menu(SPEC)
        if not SPEC.exists():
            ui.note("没有保存 deadchain.toml，先到这里。下次重跑 python3 start.py 会接着来")
            return False, st
    elif key == "generate":
        if _run([PY, "genconfig.py", str(SPEC), "--out", "generated"]) != 0:
            return False, st
    elif key == "install":
        ui.note("会覆盖 Clash Verge 里的 Merge.yaml / Script.js，原文件备份为 *.bak-时间戳，并把 config.toml 放到项目目录。")
        first_time = not st.get("deadchain_installed")
        ui.note("第一次装，推荐 y" if first_time else "Verge 里已有一份死链配置，确认要覆盖再 y")
        if ui.confirm("现在安装", first_time):
            if (HERE / "lock.json").exists():
                _run([PY, "watch.py", "--unpin"])
            if _run([PY, "genconfig.py", str(SPEC), "--out", "generated", "--install", "--yes"]) != 0:
                return False, st
        else:
            ui.note("跳过。之后可以手动：python3 genconfig.py deadchain.toml --out generated --install --yes")
    elif key == "activate":
        ui.info("到 Clash Verge：切换一下配置档（让它按新配置重新生成），然后在 Proxies 组里选「日常出口」。")
        ui.pause("做完按回车")
    elif key == "verify":
        ip = _curl_ip(SETTINGS.clash.homebb_proxy, SETTINGS.probe.ip_url)
        if ip:
            ui.success(f"家宽入口出口 IP：{ip}")
        else:
            ui.fail("家宽入口拨不通。检查节点信息、Verge 是否已重新生成、Proxies 是否选了「日常出口」")
        _run([PY, "watch.py"])
    elif key == "pin":
        ui.note("推荐 y：锁住后误改会被监控发现；要改配置时 python3 watch.py --unpin")
        if ui.confirm("上锁"):
            _run([PY, "watch.py", "--pin"])
    elif key == "launchd":
        ui.note("推荐 y：每 3 分钟检查一轮，出问题弹通知；悬浮窗显示前台 App 走的是家宽还是日常")
        if ui.confirm("安装后台监控和悬浮窗"):
            _run(["bash", str(HERE / "install.sh")])
    return True, st


def guide(st: dict) -> int:
    steps = plan(st)
    if steps and steps[0][0] == "stop":
        print()
        ui.fail(steps[0][1])
        return 2
    stages = group_steps(steps)
    ui.section(f"接下来要做 {len(stages)} 件事")
    for i, (title, _, _) in enumerate(stages, 1):
        ui.info(f"{i}. {title}")
    ui.note("每一步都会先说明，回车开始，s 跳过，q 退出；Ctrl-C 也可以随时退出，重跑会从当前状态接着来。")
    try:
        ui.pause("准备好了按回车")
        done: list[str] = []
        for i, (title, desc, keys) in enumerate(stages, 1):
            ui.title(f"第 {i} 步 / 共 {len(stages)} 步  {title}")
            ui.note(desc)
            c = ui.ask("回车开始（推荐），s 跳过，q 退出", allow_back=False).lower()
            if c == "q":
                ui.note("已退出。重跑 python3 start.py 会从当前状态接着来")
                return 1
            if c == "s":
                ui.note("跳过")
                continue
            for k in keys:
                ok, st = _do(k, st)
                if not ok:
                    return 2
            done.append(title)
    except (KeyboardInterrupt, EOFError):
        print()
        ui.note("已中断。重跑 python3 start.py 会从当前状态接着来")
        return 1
    ui.title("完成")
    print(render(detect()))
    print()
    ui.note("以后：python3 watch.py 看一轮；python3 route.py 把某个 App 改到家宽/直连/代理；")
    ui.note("      python3 wizard.py 增删节点或订阅后重新生成并安装（先 --unpin，装完 --pin）。")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="环境检测 + 引导")
    ap.add_argument("--check", action="store_true", help="只检测、打印报告，不做改动")
    a = ap.parse_args(argv)
    ui.title("clash-ai-homebb 引导")
    ui.note("先看看你的环境。这一步只读，不会改任何东西。")
    print()
    st = detect()
    print(render(st))
    if a.check:
        stages = group_steps(plan(st))
        steps = plan(st)
        ui.section("建议")
        if steps and steps[0][0] == "stop":
            ui.fail(steps[0][1])
        else:
            for i, (title, desc, _) in enumerate(stages, 1):
                ui.info(f"{i}. {title}" + ui.dim(f"  {desc}"))
        return 0
    return guide(st)


if __name__ == "__main__":
    sys.exit(main())
