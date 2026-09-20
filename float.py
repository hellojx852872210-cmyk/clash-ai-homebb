#!/usr/bin/env python3
"""前台 App 的 Clash 出口悬浮窗。非激活 HUD，不占 Dock。"""
from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.realpath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from pathlib import Path

import routes as R
from config import SETTINGS
from egress import KIND_CN, format_hud, is_terminal_app, match_conns, summarize
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


def app_matcher(app_name: str, app_path: str) -> str | None:
    """把前台 App 变成一条 mihomo 匹配规则。

    GUI App 用 bundle 路径正则，这样它的各种 Helper 进程一并覆盖；
    没有 bundle 路径的（终端里跑的命令等）退回进程名。
    """
    p = (app_path or "").rstrip("/")
    if p.endswith(".app"):
        base = Path(p).name[:-4]
        if base:
            # 只转义正则元字符：Go 的 RE2 不接受 \空格 这种多余转义，而 re.escape 会转义空格
            esc = re.sub(r"([.^$*+?()\[\]{}|\\])", r"\\\1", base)
            return "PROCESS-PATH-REGEX,.*/" + esc + r"\.app/"
    name = (app_name or "").strip()
    return f"PROCESS-NAME,{name}" if name else None


def pinned_by_ai(summary) -> bool:
    """这些连接是不是被 AI 死链规则钉在家宽上（链路里出现 AI 组名）。"""
    ai = SETTINGS.deadchain.ai_group
    return any(ai in str(c) for c in (summary.chains or ()))


def choose_egress(match: str, tag: int, ai_pinned: bool = False) -> str:
    """悬浮窗里点下某个按钮之后真正做的事。返回一句给人看的话（空串=什么也没做）。

    tag：1/2/3 = 家宽/直连/代理，4 = 清除覆盖，5 = 取消。
    """
    if tag == 5 or not match:
        return ""
    rs = R.load_routes()
    if tag == 4:
        rs, hit = R.remove(rs, match)
        if not hit:
            return "本来就没有覆盖"
        R.save_routes(rs)
        ok, msg = R.apply(rs)
        return "已清除覆盖，回到默认分流" if ok else msg
    if not 1 <= tag <= len(R.TARGETS):
        return ""
    target = R.TARGETS[tag - 1]
    err = R.validate(match, target)
    if err:
        return err
    rs, _old = R.upsert(rs, match, target)
    R.save_routes(rs)
    ok, msg = R.apply(rs)
    if not ok:
        return msg
    if target != "homebb" and ai_pinned:
        return f"已设为{R.TARGET_CN[target]}，但 AI 规则优先，可能不生效"
    return f"已设为{R.TARGET_CN[target]}，新连接即刻生效"


def snapshot_text() -> tuple[str, str, dict]:
    name, path, pid = frontmost_app()
    conns = fetch_conns()
    ctx = {"app": name, "path": path, "match": app_matcher(name, path), "kind": "", "ai": False}
    if conns is None:
        return (*with_banner(f"{name or '系统'}\nClash 未开", "mixed"), ctx)
    extra_names: set[str] = set()
    extra_paths: set[str] = set()
    if is_terminal_app(name, path) and pid:
        extra_names, extra_paths = descendant_idents(pid)
    matched = match_conns(
        conns, path, name, extra_names=extra_names, extra_paths=extra_paths
    )
    summary = summarize(matched)
    ctx["kind"] = summary.kind
    ctx["ai"] = pinned_by_ai(summary)
    return (*with_banner(*format_hud(name, summary, app_path=path)), ctx)


def run_once() -> int:
    text, color, _ = snapshot_text()
    print(text)
    print(f"#{color}")
    return 0


def run_hud() -> None:
    _enforce_single()
    signal.signal(signal.SIGTERM, lambda *a: os._exit(0))

    import objc
    from Foundation import (
        NSAttributedString,
        NSMakePoint,
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
        NSButton,
        NSColor,
        NSEvent,
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
    BTN_H, BTN_GAP = 24, 6
    DRAG_SLOP = 5  # 小于它的位移算手抖，不当拖动
    # (标题, tag, 宽度)：1/2/3 对应三个出口，4 清除覆盖，5 取消
    BTN_SPEC = [("家宽", 1, 62), ("直连", 2, 62), ("代理", 3, 62), ("清除覆盖", 4, 90), ("取消", 5, 62)]
    ROW1_W = sum(w for _, t, w in BTN_SPEC if t <= 3) + BTN_GAP * 2

    shared = {"text": "检测中…", "color": "idle", "ctx": {}, "hook": None}
    state = {"mode": "hud", "ctx": {}, "flash": "", "flash_until": 0.0, "pick_until": 0.0}
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
                {NSFontAttributeName: font, NSForegroundColorAttributeName: white},
            )
            s.appendAttributedString_(part)
        return s

    def flash(msg: str, secs: float = 2.8) -> None:
        state["flash"] = msg
        state["flash_until"] = time.time() + secs

    def poller() -> None:
        n = 0
        while True:
            try:
                text, color, ctx = snapshot_text()
                with lock:
                    shared["text"], shared["color"], shared["ctx"] = text, color, ctx
            except Exception:
                with lock:
                    shared["text"], shared["color"] = "出口窗异常", "mixed"
            if n % 10 == 0:  # 覆盖规则集装没装，后台顺手查，点击时就不用等网络了
                try:
                    with lock:
                        shared["hook"] = R.hook_installed(timeout=1.5)
                except Exception:
                    with lock:
                        shared["hook"] = None
            n += 1
            time.sleep(TICK)

    # ---------- 点一下改出口 ----------
    def current_override(match: str) -> str:
        try:
            for r in R.load_routes():
                if r.match == match:
                    return R.TARGET_CN[r.target]
        except Exception:
            pass
        return ""

    def open_picker() -> None:
        with lock:
            ctx = dict(shared.get("ctx") or {})
            hook = shared.get("hook")
        if not ctx.get("match"):
            flash("这个窗口不支持改出口")
            redraw()
            return
        if hook is not None and not hook[0]:
            flash(hook[1][:30] + "（见 route.py hook）")
            redraw()
            return
        state["ctx"] = ctx
        state["mode"] = "pick"
        state["pick_until"] = time.time() + 12
        state["flash"] = ""
        redraw()  # 立刻展开，不等定时器

    def do_choose(tag: int) -> None:
        ctx = dict(state.get("ctx") or {})
        state["mode"] = "hud"
        if tag == 5:
            redraw()
            return

        def work() -> None:  # 写文件 + 让 mihomo 重读，放后台，别卡住界面
            try:
                msg = choose_egress(ctx.get("match") or "", tag, bool(ctx.get("ai")))
            except Exception as e:
                msg = f"出错：{e}"
            if msg:
                flash(msg[:36])

        flash("处理中…", 10.0)
        redraw()  # 先收起按钮并给出反馈，写入在后台做
        threading.Thread(target=work, daemon=True).start()

    class RootView(NSView):
        def acceptsFirstMouse_(self, ev):
            return True

        def hitTest_(self, pt):
            # HUD 模式：整块面板都能点/拖；选择模式：交给按钮自己处理
            if state["mode"] == "hud":
                return self
            return objc.super(RootView, self).hitTest_(pt)

        def mouseDown_(self, ev):
            loc = NSEvent.mouseLocation()
            fr = panel.frame()
            self._start = (loc.x, loc.y)
            self._origin = (fr.origin.x, fr.origin.y)
            self._moved = False

        def mouseDragged_(self, ev):
            loc = NSEvent.mouseLocation()
            dx, dy = loc.x - self._start[0], loc.y - self._start[1]
            if not self._moved:
                # 没越过阈值就当手抖，窗口纹丝不动，免得「点一下却漂了几像素」
                if abs(dx) <= DRAG_SLOP and abs(dy) <= DRAG_SLOP:
                    return
                # 刚越过阈值：以此刻为新基准，之后跟手移动，不会突然跳 DRAG_SLOP 像素
                self._moved = True
                fr = panel.frame()
                self._start = (loc.x, loc.y)
                self._origin = (fr.origin.x, fr.origin.y)
                return
            panel.setFrameOrigin_(NSMakePoint(self._origin[0] + dx, self._origin[1] + dy))

        def mouseUp_(self, ev):
            if not getattr(self, "_moved", False):
                open_picker()

    class HudButton(NSButton):
        def acceptsFirstMouse_(self, ev):
            return True

    class Handler(NSObject):
        def choose_(self, sender):
            do_choose(int(sender.tag()))

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
    panel.setMovableByWindowBackground_(False)  # 自己处理拖动，好区分「点一下」和「拖一下」
    panel.setHidesOnDeactivate_(False)
    panel.setFloatingPanel_(True)
    panel.setCollectionBehavior_(
        NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorStationary
    )
    panel.setReleasedWhenClosed_(False)

    root = RootView.alloc().initWithFrame_(NSMakeRect(0, 0, 320, 88))
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

    handler = Handler.alloc().init()
    buttons = []
    for title, tag, width in BTN_SPEC:
        b = HudButton.alloc().initWithFrame_(NSMakeRect(0, 0, width, BTN_H))
        b.setTitle_(title)
        b.setBezelStyle_(1)  # NSBezelStyleRounded
        b.setFont_(NSFont.systemFontOfSize_(12))
        b.setTarget_(handler)
        b.setAction_("choose:")
        b.setTag_(tag)
        b.setHidden_(True)
        root.addSubview_(b)
        buttons.append(b)

    def pick_text() -> str:
        ctx = state.get("ctx") or {}
        cur = KIND_CN.get(ctx.get("kind") or "", "未知")
        had = current_override(ctx.get("match") or "")
        second = f"现在 {cur}" + (f" · 已设为{had}" if had else "") + " · 选新出口"
        return f"{ctx.get('app') or '未知 App'}\n{second}"

    ui_state = {"last": None}

    def redraw() -> None:
        """重画面板。定时器每 0.3 秒调一次；点击时也直接调，保证跟手。只在主线程调用。"""
        now = time.time()
        if state["mode"] == "pick" and now > state["pick_until"]:
            state["mode"] = "hud"
        picking = state["mode"] == "pick"
        if picking:
            text, color = pick_text(), "other"
        else:
            with lock:
                text, color = shared["text"], shared["color"]
            if state["flash"]:
                if now < state["flash_until"]:
                    text, color = text + "\n" + state["flash"], "other"
                else:
                    state["flash"] = ""
        key = (text, color, picking)
        if key == ui_state["last"]:
            if not panel.isVisible():
                panel.orderFrontRegardless()
            return
        ui_state["last"] = key
        for b in buttons:
            b.setHidden_(not picking)
        lbl.setAttributedStringValue_(styled_text(text))
        size = lbl.fittingSize()
        extra = (BTN_H + BTN_GAP) * 2 if picking else 0
        w = max(220, size.width + stripe_w + pad_x * 2)
        if picking:
            w = max(w, ROW1_W + stripe_w + pad_x * 2)
        h = max(64, size.height + pad_y * 2 + extra)
        fr = panel.frame()
        panel.setFrame_display_(NSMakeRect(fr.origin.x, fr.origin.y, w, h), True)
        root.setFrame_(NSMakeRect(0, 0, w, h))
        bg.setFrame_(NSMakeRect(0, 0, w, h))
        stripe.setFrame_(NSMakeRect(0, 0, stripe_w, h))
        lbl.setFrame_(NSMakeRect(stripe_w + pad_x, pad_y + extra, size.width, size.height))
        if picking:
            x, y_top = stripe_w + pad_x, pad_y + BTN_H + BTN_GAP
            for b, (_t, tag, width) in zip(buttons, BTN_SPEC):
                if tag == 4:
                    x = stripe_w + pad_x
                b.setFrame_(NSMakeRect(x, y_top if tag <= 3 else pad_y, width, BTN_H))
                x += width + BTN_GAP
        r, g, bl = colors.get(color, colors["idle"])
        stripe.setFillColor_(NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, bl, 1.0))
        panel.orderFrontRegardless()

    class FloatUI(NSObject):
        def tick_(self, timer):
            redraw()

    ui = FloatUI.alloc().init()
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        0.3, ui, "tick:", None, True
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
