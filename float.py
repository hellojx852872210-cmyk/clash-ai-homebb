#!/usr/bin/env python3
"""前台 App 的 Clash 出口悬浮窗。非激活 HUD，不占 Dock。"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.realpath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from pathlib import Path

from config import SETTINGS
from egress import format_hud, is_terminal_app, match_conns, summarize
from watch import CODE_SHORT, STATE_PATH, api_json, clash_reachable

WATCH_STALE_SECS = SETTINGS.hud.stale_secs

SCRIPT_PATH = os.path.realpath(__file__)
TICK = SETTINGS.hud.tick


def _enforce_single() -> None:
    me = os.getpid()
    try:
        out = subprocess.run(
            ["pgrep", "-f", "float.py"], capture_output=True, text=True
        ).stdout
        for s in out.split():
            try:
                pid = int(s)
            except ValueError:
                continue
            if pid == me:
                continue
            cmd = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True,
                text=True,
                timeout=2,
            ).stdout
            if SCRIPT_PATH not in cmd:
                continue
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
    except Exception:
        pass


def fetch_conns():
    if not clash_reachable():
        return None
    try:
        data = api_json("/connections", timeout=2.0)
    except Exception:
        return None
    return data.get("connections") or []


def pid_exe(pid: int) -> str:
    if pid <= 0:
        return ""
    try:
        import ctypes
        from ctypes import c_int, c_uint32, create_string_buffer

        lib = ctypes.CDLL("/usr/lib/libproc.dylib")
        buf = create_string_buffer(4096)
        n = lib.proc_pidpath(c_int(pid), buf, c_uint32(len(buf)))
        if n <= 0:
            return ""
        return buf.value.decode("utf-8", "replace")
    except Exception:
        return ""


def descendant_idents(root_pid: int) -> tuple[set[str], set[str]]:
    if root_pid <= 0:
        return set(), set()
    proc = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,comm="],
        capture_output=True,
        text=True,
        timeout=2,
    )
    kids: dict[int, list[int]] = {}
    comm: dict[int, str] = {}
    for line in proc.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        kids.setdefault(ppid, []).append(pid)
        comm[pid] = parts[2].strip()
    names: set[str] = set()
    paths: set[str] = set()
    stack = [root_pid]
    seen: set[int] = set()
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        if p != root_pid:
            n = comm.get(p) or ""
            if n and n not in ("login",):
                names.add(n)
                names.add(Path(n).name)
            exe = pid_exe(p)
            if exe:
                paths.add(exe)
                try:
                    paths.add(str(Path(exe).resolve()))
                except Exception:
                    pass
        stack.extend(kids.get(p, ()))
    return names, paths


def frontmost_app() -> tuple[str, str, int]:
    from AppKit import NSWorkspace

    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        return ("", "", 0)
    name = str(app.localizedName() or "")
    url = app.bundleURL()
    path = str(url.path()) if url is not None else ""
    try:
        pid = int(app.processIdentifier())
    except Exception:
        pid = 0
    return (name, path, pid)


def watch_banner() -> str | None:
    """watch.py 的 state.json 非正常/失联时，悬浮窗常驻一行红字。"""
    import json
    from datetime import datetime, timezone

    try:
        st = json.loads(STATE_PATH.read_text())
    except Exception:
        return "⚠ 家宽监控无状态"
    try:
        ts = datetime.fromisoformat(str(st.get("ts")))
        age = (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        age = float("inf")
    if age > WATCH_STALE_SECS:
        mins = int(age // 60) if age != float("inf") else -1
        return f"⚠ 家宽监控失联 {mins} 分钟" if mins >= 0 else "⚠ 家宽监控失联"
    code = str(st.get("code") or "")
    if code and code != "ok":
        return "⚠ " + CODE_SHORT.get(code, code)
    return None


def with_banner(text: str, color: str) -> tuple[str, str]:
    b = watch_banner()
    if b is None:
        return (text, color)
    return (text + "\n" + b, "mixed")


def snapshot_text() -> tuple[str, str]:
    name, path, pid = frontmost_app()
    conns = fetch_conns()
    if conns is None:
        return with_banner(f"{name or '系统'}\nClash 未开", "mixed")
    extra_names: set[str] = set()
    extra_paths: set[str] = set()
    if is_terminal_app(name, path) and pid:
        extra_names, extra_paths = descendant_idents(pid)
    matched = match_conns(
        conns, path, name, extra_names=extra_names, extra_paths=extra_paths
    )
    return with_banner(*format_hud(name, summarize(matched), app_path=path))


def run_once() -> int:
    text, color = snapshot_text()
    print(text)
    print(f"#{color}")
    return 0


def run_hud() -> None:
    _enforce_single()
    signal.signal(signal.SIGTERM, lambda *a: os._exit(0))

    from Foundation import (
        NSAttributedString,
        NSMakeRect,
        NSMutableAttributedString,
        NSObject,
        NSTimer,
    )
    from AppKit import (
        NSApplication,
        NSApplicationActivationPolicyProhibited,
        NSBackingStoreBuffered,
        NSBox,
        NSBoxCustom,
        NSColor,
        NSFont,
        NSFontAttributeName,
        NSFontWeightRegular,
        NSFontWeightSemibold,
        NSForegroundColorAttributeName,
        NSPanel,
        NSTextField,
        NSView,
        NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorStationary,
        NSWindowStyleMaskBorderless,
        NSWindowStyleMaskNonactivatingPanel,
    )
    try:
        from AppKit import NSStatusWindowLevel as _WIN_LEVEL
    except ImportError:
        _WIN_LEVEL = 25

    colors = {
        "homebb": (0.22, 0.92, 0.50),
        "daily": (0.40, 0.72, 1.00),
        "direct": (1.00, 0.84, 0.20),
        "mixed": (1.00, 0.36, 0.32),
        "idle": (0.78, 0.78, 0.82),
        "unknown": (0.78, 0.78, 0.82),
        "other": (0.86, 0.62, 1.00),
    }
    pad_x, pad_y, stripe_w = 16, 12, 6
    shared = {"text": "检测中…", "color": "idle"}
    lock = threading.Lock()
    title_font = NSFont.systemFontOfSize_weight_(17, NSFontWeightSemibold)
    body_font = NSFont.systemFontOfSize_weight_(14, NSFontWeightRegular)
    white = NSColor.colorWithWhite_alpha_(1.0, 1.0)

    def styled_text(text: str):
        s = NSMutableAttributedString.alloc().init()
        lines = text.split("\n")
        for i, line in enumerate(lines):
            chunk = line + ("\n" if i < len(lines) - 1 else "")
            font = title_font if i == 0 else body_font
            part = NSAttributedString.alloc().initWithString_attributes_(
                chunk,
                {
                    NSFontAttributeName: font,
                    NSForegroundColorAttributeName: white,
                },
            )
            s.appendAttributedString_(part)
        return s

    def poller() -> None:
        while True:
            try:
                text, color = snapshot_text()
                with lock:
                    shared["text"] = text
                    shared["color"] = color
            except Exception:
                with lock:
                    shared["text"] = "出口窗异常"
                    shared["color"] = "mixed"
            time.sleep(TICK)

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyProhibited)
    panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(24, 72, 320, 88),
        NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
        NSBackingStoreBuffered,
        False,
    )
    panel.setLevel_(_WIN_LEVEL)
    panel.setOpaque_(False)
    panel.setBackgroundColor_(NSColor.clearColor())
    panel.setHasShadow_(True)
    panel.setMovableByWindowBackground_(True)
    panel.setHidesOnDeactivate_(False)
    panel.setFloatingPanel_(True)
    panel.setCollectionBehavior_(
        NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorStationary
    )
    panel.setReleasedWhenClosed_(False)

    root = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 320, 88))
    bg = NSBox.alloc().initWithFrame_(NSMakeRect(0, 0, 320, 88))
    bg.setBoxType_(NSBoxCustom)
    bg.setBorderWidth_(0)
    bg.setCornerRadius_(14.0)
    bg.setTitle_("")
    bg.setFillColor_(NSColor.colorWithCalibratedWhite_alpha_(0.09, 0.97))
    bg.setContentViewMargins_((0, 0))
    bg.setAutoresizingMask_(18)
    root.addSubview_(bg)
    panel.setContentView_(root)

    stripe = NSBox.alloc().initWithFrame_(NSMakeRect(0, 0, stripe_w, 88))
    stripe.setBoxType_(NSBoxCustom)
    stripe.setBorderWidth_(0)
    stripe.setCornerRadius_(0)
    stripe.setContentViewMargins_((0, 0))
    root.addSubview_(stripe)

    lbl = NSTextField.labelWithString_("检测中…")
    lbl.setFont_(title_font)
    lbl.setTextColor_(white)
    lbl.setMaximumNumberOfLines_(0)
    root.addSubview_(lbl)

    import objc

    class FloatUI(NSObject):
        def init(self):
            self = objc.super(FloatUI, self).init()
            self.last = ("", "")
            return self

        def tick_(self, timer):
            with lock:
                t, color = shared["text"], shared["color"]
            if (t, color) == self.last:
                if not panel.isVisible():
                    panel.orderFrontRegardless()
                return
            self.last = (t, color)
            attr = styled_text(t)
            lbl.setAttributedStringValue_(attr)
            r, g, b = colors.get(color, colors["idle"])
            stripe.setFillColor_(NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, 1.0))
            size = lbl.fittingSize()
            w = max(220, size.width + stripe_w + pad_x * 2)
            h = max(64, size.height + pad_y * 2)
            fr = panel.frame()
            panel.setFrame_display_(NSMakeRect(fr.origin.x, fr.origin.y, w, h), True)
            root.setFrame_(NSMakeRect(0, 0, w, h))
            bg.setFrame_(NSMakeRect(0, 0, w, h))
            stripe.setFrame_(NSMakeRect(0, 0, stripe_w, h))
            lbl.setFrame_(NSMakeRect(stripe_w + pad_x, pad_y, size.width, size.height))
            panel.orderFrontRegardless()

    ui = FloatUI.alloc().init()
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        0.8, ui, "tick:", None, True
    )
    threading.Thread(target=poller, daemon=True).start()
    app.run()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="前台 App Clash 出口悬浮窗")
    p.add_argument("--once", action="store_true", help="打印一次前台出口，不启动窗口")
    args = p.parse_args(argv)
    if args.once:
        return run_once()
    run_hud()
    return 0


if __name__ == "__main__":
    sys.exit(main())
