#!/usr/bin/env python3
"""监控（watch）和悬浮窗（float）两个 launchd 任务的查看、启用、停用。panel.py 用它，也能单独跑：

    python3 agents.py                    # 两个任务的状态
    python3 agents.py enable [watch|float]
    python3 agents.py disable [watch|float]

按 ProgramArguments 里的脚本路径认任务，不按 label：install.sh 装的 io.github.* 和早先手装的其它 label
都算同一个服务，不会再装出第二份（两份悬浮窗会互相杀）。
停用 = bootout + disable，下次登录也不会自己起来；启用 = enable + bootstrap，
一份 plist 都没有时按 launchd/*.plist.in 现装一份，和 install.sh 装出来的一样。
"""
from __future__ import annotations

import argparse
import os
import plistlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
# 和 install.sh 的 pick_python 同一串候选
PY_CANDIDATES = (
    "python3",
    "/opt/homebrew/bin/python3",
    "/usr/local/bin/python3",
    "/Library/Frameworks/Python.framework/Versions/Current/bin/python3",
)


@dataclass(frozen=True)
class Service:
    key: str
    script: str
    label: str  # install.sh 用的标准 label，现装时用它
    name: str
    periodic: bool  # StartInterval 定时跑的，平时没有进程是正常的


SERVICES = {
    "watch": Service("watch", "watch.py", "io.github.clash-ai-homebb.watch", "监控", True),
    "float": Service("float", "float.py", "io.github.clash-ai-homebb.float", "悬浮窗", False),
}

STATE_CN = {
    "running": "运行中",
    "idle": "待命",
    "starting": "启动中",
    "crashed": "退出了",
    "disabled": "已停用",
    "stopped": "未加载",
    "missing": "未安装",
}


class AgentError(RuntimeError):
    pass


@dataclass
class Status:
    svc: Service
    label: str
    plist: Path | None  # None = LaunchAgents 里没有
    loaded: bool = False
    pid: int | None = None
    disabled: bool = False
    last_exit: str = ""
    interval: int | None = None
    others: list[str] = field(default_factory=list)  # 同一个脚本另外还加载着的 label

    @property
    def state(self) -> str:
        if self.loaded:
            if self.pid:
                return "running"
            if self.svc.periodic:
                return "idle"
            code = _exit_code(self.last_exit)
            return "crashed" if code not in (None, 0) else "starting"
        if self.plist is None:
            return "missing"
        return "disabled" if self.disabled else "stopped"

    def describe(self) -> str:
        s = self.state
        text = STATE_CN[s]
        if s == "running" and self.pid:
            text += f" · PID {self.pid}"
        elif s == "idle" and self.interval:
            text += f" · 每 {max(1, self.interval // 60)} 分钟一轮"
        elif s == "crashed":
            text += f"（退出码 {_exit_code(self.last_exit)}）"
        if self.others:
            text += f" · 另有重复任务 {', '.join(self.others)}"
        return text


def _exit_code(raw: str) -> int | None:
    m = re.match(r"\s*(-?\d+)", raw or "")
    return int(m.group(1)) if m else None


def domain() -> str:
    return f"gui/{os.getuid()}"


def _lc(*args: str, timeout: float = 20) -> tuple[int, str]:
    try:
        r = subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        return 1, str(e)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


_TOP = re.compile(r"^\t([A-Za-z][\w ]*?) = (.*)$")
_DIS = re.compile(r'^\s*"([^"]+)"\s*=>\s*(\w+)')


def parse_print(text: str) -> dict[str, str]:
    """launchctl print 的顶层字段：一个 tab 缩进的 key = value；嵌套块里的同名字段不要。"""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = _TOP.match(line)
        if m and m.group(1) not in out:
            out[m.group(1)] = m.group(2).strip()
    return out


def parse_disabled(text: str) -> dict[str, bool]:
    """launchctl print-disabled：label → 是否停用。新系统写 disabled/enabled，老系统写 true/false。"""
    out: dict[str, bool] = {}
    for line in text.splitlines():
        m = _DIS.match(line)
        if m:
            out[m.group(1)] = m.group(2) in ("disabled", "true")
    return out


def _load_plist(path: Path) -> dict:
    try:
        with path.open("rb") as f:
            data = plistlib.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def find_plists(svc: Service, agents_dir: Path = AGENTS_DIR, project: Path = HERE) -> list[tuple[str, Path]]:
    """LaunchAgents 里跑本项目 svc.script 的 plist，[(label, 路径)]，标准 label 排最前。"""
    target = os.path.realpath(project / svc.script)
    found = []
    for p in sorted(agents_dir.glob("*.plist")):
        d = _load_plist(p)
        args = d.get("ProgramArguments") or ([d["Program"]] if d.get("Program") else [])
        if any(isinstance(a, str) and a.endswith(svc.script) and os.path.realpath(a) == target for a in args):
            found.append((str(d.get("Label") or p.stem), p))
    found.sort(key=lambda t: (t[0] != svc.label, t[0]))
    return found


def status(svc: Service, disabled: dict[str, bool] | None = None) -> Status:
    if disabled is None:
        disabled = parse_disabled(_lc("print-disabled", domain())[1])
    plists = find_plists(svc)
    paths = dict(plists)
    labels = [label for label, _ in plists] or [svc.label]
    loaded = []
    for label in labels:
        rc, out = _lc("print", f"{domain()}/{label}")
        if rc == 0:
            loaded.append((label, parse_print(out)))
    if loaded:
        loaded.sort(key=lambda t: (not t[1].get("pid"), t[0] != svc.label))  # 有进程的优先
        label, info = loaded[0]
        pid = info.get("pid", "")
        path = paths.get(label) or (Path(info["path"]) if info.get("path") else None)
        st = Status(svc, label, path, loaded=True, pid=int(pid) if pid.isdigit() else None,
                    disabled=disabled.get(label, False), last_exit=info.get("last exit code", ""),
                    others=[lb for lb, _ in loaded[1:]])
    else:
        label, path = plists[0] if plists else (svc.label, None)
        st = Status(svc, label, path, disabled=disabled.get(label, False))
    if st.plist is not None:
        iv = _load_plist(st.plist).get("StartInterval")
        st.interval = iv if isinstance(iv, int) else None
    return st


def statuses(svcs: list[Service] | None = None) -> list[Status]:
    disabled = parse_disabled(_lc("print-disabled", domain())[1])
    return [status(s, disabled) for s in (svcs or list(SERVICES.values()))]


def pick_python(appkit: bool) -> str | None:
    """和 install.sh 一样：要 3.11+（tomllib）；悬浮窗还要能 import AppKit。"""
    check = "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" + ("; import AppKit" if appkit else "")
    for c in PY_CANDIDATES:
        path = shutil.which(c)
        if not path:
            continue
        try:
            if subprocess.run([path, "-c", check], capture_output=True, timeout=30).returncode == 0:
                return path
        except Exception:
            continue
    return None


def render_plist(template: str, project: Path, python: str) -> str:
    return (template.replace("__PROJECT__", str(project))
            .replace("__PYTHON_HUD__", python)
            .replace("__PYTHON__", python))


def install_plist(svc: Service) -> Path:
    python = pick_python(appkit=svc.key == "float")
    if not python:
        need = "带 PyObjC 的 Python 3.11+（python3 -m pip install pyobjc-framework-Cocoa）" if svc.key == "float" else "Python 3.11+"
        raise AgentError(f"装不了{svc.name}：没找到{need}")
    tpl = (HERE / "launchd" / f"{svc.label}.plist.in").read_text()
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    dst = AGENTS_DIR / f"{svc.label}.plist"
    dst.write_text(render_plist(tpl, HERE, python))
    return dst


def enable(svc: Service) -> str:
    st = status(svc)
    if st.loaded:
        if not st.pid and not svc.periodic:  # 悬浮窗加载着却没进程：拉一下
            _lc("kickstart", f"{domain()}/{st.label}")
        return f"{svc.name}已经在跑"
    if st.plist is None:
        label, path = svc.label, install_plist(svc)
    else:
        label, path = st.label, st.plist
    _lc("enable", f"{domain()}/{label}")
    rc, out = _lc("bootstrap", domain(), str(path))
    if rc != 0 and not status(svc).loaded:
        raise AgentError(f"{svc.name}启用失败：{_last_line(out) or f'launchctl 返回 {rc}'}")
    return f"已启用{svc.name}"


def disable(svc: Service) -> str:
    labels = [label for label, _ in find_plists(svc)]
    st = status(svc)
    for label in [st.label, *st.others]:
        if st.loaded and label not in labels:
            labels.append(label)
    for label in labels:
        _lc("bootout", f"{domain()}/{label}")  # 本来就没加载会报错，不管
        _lc("disable", f"{domain()}/{label}")
    after = status(svc)
    if after.loaded:
        raise AgentError(f"{svc.name}没停下来（{after.label}）")
    return f"已停用{svc.name}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="监控 / 悬浮窗的 launchd 任务：查看、启用、停用")
    p.add_argument("action", nargs="?", default="status", choices=("status", "enable", "disable"))
    p.add_argument("which", nargs="?", default="all", choices=("all", *SERVICES))
    args = p.parse_args(argv)
    svcs = list(SERVICES.values()) if args.which == "all" else [SERVICES[args.which]]
    rc = 0
    for svc in svcs:
        try:
            if args.action == "enable":
                print(enable(svc))
            elif args.action == "disable":
                print(disable(svc))
        except AgentError as e:
            print(e, file=sys.stderr)
            rc = 1
    for st in statuses(svcs):
        print(f"{st.svc.name}\t{st.describe()}\t{st.label}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
