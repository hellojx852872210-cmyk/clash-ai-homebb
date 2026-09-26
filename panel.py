#!/usr/bin/env python3
"""桌面控制面板：看家宽 / 日常出口和监控判级，一键启用、停用监控和悬浮窗。

scripts/build-app.sh 把它包成「家宽选择器.app」；也可以直接 python3 panel.py。
面板只负责看和开关，干活的还是 launchd 里的 watch.py / float.py：关掉面板它们照跑，
停用它们也不改 Clash 配置，AI 仍然只走家宽。
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.realpath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import agents as A

APP_NAME = "家宽选择器"
SCRIPT_PATH = os.path.realpath(__file__)
STATE_PATH = os.path.join(HERE, "state.json")
POLL = 2.0
AUTO_CHECK_AFTER = 300  # 打开面板时上次检查早于这么多秒，自动跑一轮
SERVICE_TEXT = {  # key → (标题, 说明)；顺序即面板里的顺序
    "watch": ("监控", "定时查家宽 / 日常出口和死链，出事弹通知"),
    "float": ("悬浮窗 · 出口选择器", "显示前台 App 走哪个出口，点它就能改"),
    "guard": ("出口守护 · 自动切换", "出口不通时分析原因，自动切到能通的出口"),
}


def watch_tables() -> tuple[dict, dict, int]:
    """watch.py 的判级文案和过期阈值。config 坏了面板也得能开，所以兜底。"""
    try:
        from config import SETTINGS
        from watch import CODE_CN, CODE_SHORT

        return CODE_SHORT, CODE_CN, SETTINGS.hud.stale_secs
    except Exception:
        return {}, {}, 900


def ago(secs: float) -> str:
    secs = max(0.0, secs)
    if secs < 60:
        return "刚刚"
    if secs < 3600:
        return f"{int(secs // 60)} 分钟前"
    if secs < 86400:
        return f"{int(secs // 3600)} 小时前"
    return f"{int(secs // 86400)} 天前"


def load_state(path: str = STATE_PATH) -> dict | None:
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def state_age(st: dict | None, now: float) -> float | None:
    try:
        ts = datetime.fromisoformat(str((st or {})["ts"]))
    except Exception:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return now - ts.timestamp()


def summarize(st: dict | None, now: float, stale_secs: int, short: dict, long: dict,
              monitor_on: bool = True) -> dict:
    """state.json → 面板顶部：{level, title, detail, rows}。level 取 ok / warn / crit / stale / none。

    monitor_on=False：监控停了，结果不会再更新，哪怕还没过期也按过期显示，别让人以为一直是「正常」。"""
    if not st:
        return {"level": "none", "title": "还没有检查结果", "detail": "点「立即检查」跑一轮", "rows": []}
    code = str(st.get("code") or "")
    level = str(st.get("level") or "")
    if level not in ("ok", "warn", "crit"):
        level = "warn"
    detail = long.get(code, "")
    if code != "ok" and st.get("detail"):
        detail = f"{detail}：{st['detail']}" if detail else str(st["detail"])
    age = state_age(st, now)
    if not monitor_on:
        level = "stale"
        detail = "监控已停用，这是停用前最后一次的结果。" + detail
    elif age is None or age > stale_secs:
        level = "stale"
        detail = "结果过期了，监控可能没在跑。" + detail
    chain = str(st.get("ai_chain") or "")
    lock_ok = st.get("lock_ok")
    rows = [
        ("家宽出口", str(st.get("homebb_ip") or "—")),
        ("日常出口", str(st.get("daily_ip") or "—")),
        ("AI 链路", " → ".join(chain.split("/")) if chain else "—"),
        ("配置锁", "正常" if lock_ok is True else (f"异常：{st.get('lock_detail') or '?'}" if lock_ok is False else "—")),
        ("上次检查", ago(age) if age is not None else "—"),
    ]
    return {"level": level, "title": short.get(code, code or "未知"), "detail": detail, "rows": rows}


def other_instance() -> int | None:
    me = os.getpid()
    try:
        out = subprocess.run(["pgrep", "-f", "panel.py"], capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return None
    for s in out.split():
        if not s.isdigit() or int(s) == me:
            continue
        try:
            cmd = subprocess.run(["ps", "-p", s, "-o", "command="], capture_output=True, text=True, timeout=2).stdout
        except Exception:
            continue
        if SCRIPT_PATH in cmd:
            return int(s)
    return None


def check_now(short: dict) -> str:
    """跑一轮 watch.py：和 launchd 定时跑的是同一段逻辑，state.json 会更新，该告警照样告警。"""
    r = subprocess.run([sys.executable, os.path.join(HERE, "watch.py"), "--json"],
                       capture_output=True, text=True, timeout=120, cwd=HERE)
    try:
        code = json.loads(r.stdout)["result"]["code"]
    except Exception:
        tail = A._last_line(r.stderr) or A._last_line(r.stdout) or f"退出码 {r.returncode}"
        raise RuntimeError(f"检查失败：{tail}")
    return f"检查完成：{short.get(code, code)}"


def enable_all() -> str:
    errs = []
    for svc in A.SERVICES.values():
        try:
            A.enable(svc)
        except A.AgentError as e:
            errs.append(str(e))
    if errs:
        raise RuntimeError("；".join(errs))
    return "已全部启用，监控马上跑一轮"


def disable_all() -> str:
    errs = []
    for svc in A.SERVICES.values():
        try:
            A.disable(svc)
        except A.AgentError as e:
            errs.append(str(e))
    if errs:
        raise RuntimeError("；".join(errs))
    return "已全部停用。Clash 配置没动，AI 仍只走家宽"


def run_panel(icon: str | None = None, snapshot: str | None = None, appearance: str | None = None) -> None:
    """snapshot：不弹窗，窗口放屏幕外，数据到齐后把界面存成 PNG 就退出（README 截图、验收用）。"""
    import objc  # noqa: F401  确保 PyObjC 在
    from AppKit import (
        NSApplication,
        NSApplicationActivationPolicyRegular,
        NSBackingStoreBuffered,
        NSBox,
        NSBoxCustom,
        NSButton,
        NSColor,
        NSControlStateValueOff,
        NSControlStateValueOn,
        NSFont,
        NSFontWeightRegular,
        NSFontWeightSemibold,
        NSImage,
        NSMenu,
        NSMenuItem,
        NSProgressIndicator,
        NSRunningApplication,
        NSSwitch,
        NSTextAlignmentRight,
        NSTextField,
        NSView,
        NSWindow,
        NSWindowStyleMaskClosable,
        NSWindowStyleMaskMiniaturizable,
        NSWindowStyleMaskTitled,
    )
    from Foundation import NSBundle, NSMakeRect, NSObject, NSRunLoop, NSRunLoopCommonModes, NSTimer

    # 进程其实是 Python，改掉菜单栏里的名字（要在 NSApplication 创建前）
    try:
        b = NSBundle.mainBundle()
        info = b.localizedInfoDictionary() or b.infoDictionary()
        if info is not None:
            info["CFBundleName"] = APP_NAME
    except Exception:
        pass

    app = NSApplication.sharedApplication()
    other = None if snapshot else other_instance()
    if other:  # 已经开着一个：把它叫到前台，自己退出
        print(f"面板已经开着（pid {other}），切过去", file=sys.stderr)
        ra = NSRunningApplication.runningApplicationWithProcessIdentifier_(other)
        if ra is not None:
            if hasattr(app, "yieldActivationToApplication_"):
                app.yieldActivationToApplication_(ra)
            ra.activateWithOptions_(1 << 0)  # NSApplicationActivateAllWindows
        return

    # 信号交给系统默认处理，立刻结束。Python 层的处理函数要等主循环回到 Python 才跑，
    # 窗口在后台被 App Nap 节流时会拖好几秒，这期间再打开 App 会把前台交给一个正在退出的实例
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    app.setActivationPolicy_(1 if snapshot else NSApplicationActivationPolicyRegular)  # 1 = Accessory：截图时不进程序坞
    if icon and os.path.exists(icon):
        img = NSImage.alloc().initWithContentsOfFile_(icon)
        if img is not None:
            app.setApplicationIconImage_(img)

    short, long, stale_secs = watch_tables()
    shared = {"state": None, "mtime": None, "agents": {}, "busy": "", "msg": "", "msg_level": "info", "msg_until": 0.0}
    lock = threading.Lock()
    wake = threading.Event()

    # ---------- 后台：读 state.json、查 launchd ----------
    def refresh_state() -> None:
        try:
            m = os.stat(STATE_PATH).st_mtime
        except OSError:
            m = None
        with lock:
            if m == shared["mtime"] and shared["state"] is not None:
                return
        st = load_state()
        with lock:
            shared["state"], shared["mtime"] = st, m

    def refresh_agents() -> None:
        try:
            sts = {s.svc.key: s for s in A.statuses()}
        except Exception:
            return
        with lock:
            shared["agents"] = sts

    def poller() -> None:
        while True:
            refresh_state()
            refresh_agents()
            wake.wait(POLL)
            wake.clear()

    def act(busy_text: str, fn) -> None:
        with lock:
            if shared["busy"]:
                return
            shared["busy"] = busy_text

        def work() -> None:
            try:
                msg, lvl = fn(), "ok"
            except Exception as e:
                msg, lvl = str(e) or type(e).__name__, "err"
            refresh_agents()  # 先把新状态查回来再解除忙碌，开关才不会闪回旧位置
            refresh_state()
            with lock:
                shared.update(busy="", msg=msg, msg_level=lvl, msg_until=time.time() + 10)

        threading.Thread(target=work, daemon=True).start()
        safe_redraw()

    # ---------- 界面 ----------
    W, PAD = 460, 22
    ROW0, ROW_H = 232, 56  # 任务开关：第一行的位置和行高，行数跟着 SERVICE_TEXT 走
    SEP2 = ROW0 + len(SERVICE_TEXT) * ROW_H + 4  # 开关下面那条分隔线
    MSG_Y, BTN_Y = SEP2 + 14, SEP2 + 44
    H = BTN_Y + 60

    class FlippedView(NSView):
        def isFlipped(self):
            return True

    def label(text: str, frame, size: float = 13, weight=NSFontWeightRegular, color=None, selectable: bool = False):
        f = NSTextField.labelWithString_(text)
        f.setFrame_(frame)
        f.setFont_(NSFont.systemFontOfSize_weight_(size, weight))
        f.setTextColor_(color or NSColor.labelColor())
        f.setSelectable_(selectable)
        content.addSubview_(f)
        return f

    def separator(y: float) -> None:
        box = NSBox.alloc().initWithFrame_(NSMakeRect(PAD, y, W - 2 * PAD, 1))
        box.setBoxType_(2)  # NSBoxSeparator
        content.addSubview_(box)

    style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable
    win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, W, H), style, NSBackingStoreBuffered, False
    )
    win.setTitle_(APP_NAME)
    win.setReleasedWhenClosed_(False)
    if appearance:
        from AppKit import NSAppearance

        win.setAppearance_(NSAppearance.appearanceNamed_("NSAppearanceNameDarkAqua" if appearance == "dark" else "NSAppearanceNameAqua"))
    content = FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, W, H))
    win.setContentView_(content)

    # 顶部：判级
    dot = NSBox.alloc().initWithFrame_(NSMakeRect(PAD, 30, 12, 12))
    dot.setBoxType_(NSBoxCustom)
    dot.setBorderWidth_(0)
    dot.setCornerRadius_(6)
    dot.setTitle_("")
    content.addSubview_(dot)
    title = label("检测中…", NSMakeRect(PAD + 20, 20, W - 2 * PAD - 20, 30), 22, NSFontWeightSemibold)
    detail = NSTextField.wrappingLabelWithString_("")
    detail.setFrame_(NSMakeRect(PAD, 56, W - 2 * PAD, 34))
    detail.setFont_(NSFont.systemFontOfSize_(12.5))
    detail.setTextColor_(NSColor.secondaryLabelColor())
    detail.setMaximumNumberOfLines_(2)
    detail.cell().setTruncatesLastVisibleLine_(True)
    content.addSubview_(detail)

    mono = NSFont.monospacedDigitSystemFontOfSize_weight_(13, NSFontWeightRegular)
    row_widgets = []
    for i in range(5):
        y = 98 + i * 22
        k = label("", NSMakeRect(PAD, y, 76, 20), 13, color=NSColor.secondaryLabelColor())
        v = label("", NSMakeRect(PAD + 80, y, W - 2 * PAD - 80, 20), 13, selectable=True)
        v.setFont_(mono)
        v.setLineBreakMode_(5)  # NSLineBreakByTruncatingMiddle
        row_widgets.append((k, v))
    separator(218)

    # 中间：两个任务的开关
    class Handler(NSObject):
        def toggle_(self, sender):
            key = SVC_KEYS[int(sender.tag())]
            svc = A.SERVICES[key]
            if sender.state() == NSControlStateValueOn:
                act(f"正在启用{svc.name}…", lambda: A.enable(svc))
            else:
                act(f"正在停用{svc.name}…", lambda: A.disable(svc))

        def primary_(self, sender):
            with lock:
                sts = dict(shared["agents"])
            if sts and all(s.loaded for s in sts.values()):
                act("正在停用…", disable_all)
            else:
                act("正在启用…", enable_all)

        def check_(self, sender):
            act("正在检查（约 3 秒）…", lambda: check_now(short))

        def tick_(self, timer):
            safe_redraw()

        def snap_(self, timer):
            safe_redraw()
            frame_view = content.superview() or content  # 连标题栏一起
            rect = frame_view.bounds()
            rep = frame_view.bitmapImageRepForCachingDisplayInRect_(rect)
            frame_view.cacheDisplayInRect_toBitmapImageRep_(rect, rep)
            rep.representationUsingType_properties_(4, {}).writeToFile_atomically_(snapshot, True)  # 4 = PNG
            print(f"已保存 {snapshot}", flush=True)
            os._exit(0)

    handler = Handler.alloc().init()
    SVC_KEYS = list(SERVICE_TEXT)
    svc_widgets = {}
    for i, key in enumerate(SVC_KEYS):
        y = ROW0 + i * ROW_H
        name, sub = SERVICE_TEXT[key]
        label(name, NSMakeRect(PAD, y, 190, 20), 14.5, NSFontWeightSemibold)
        label(sub, NSMakeRect(PAD, y + 23, W - 2 * PAD - 60, 16), 11.5, color=NSColor.secondaryLabelColor())
        st_lbl = label("…", NSMakeRect(PAD + 194, y + 2, W - 2 * PAD - 194 - 56, 18), 12,
                       color=NSColor.secondaryLabelColor())
        st_lbl.setAlignment_(NSTextAlignmentRight)
        st_lbl.setLineBreakMode_(4)  # NSLineBreakByTruncatingTail
        sw = NSSwitch.alloc().initWithFrame_(NSMakeRect(W - PAD - 42, y + 8, 42, 24))
        sw.setTag_(i)
        sw.setTarget_(handler)
        sw.setAction_("toggle:")
        sw.setEnabled_(False)
        content.addSubview_(sw)
        svc_widgets[key] = (st_lbl, sw)
    separator(SEP2)

    # 底部：提示行 + 按钮
    spinner = NSProgressIndicator.alloc().initWithFrame_(NSMakeRect(PAD, MSG_Y + 1, 16, 16))
    spinner.setStyle_(1)  # NSProgressIndicatorStyleSpinning
    spinner.setControlSize_(1)  # NSControlSizeSmall
    spinner.setDisplayedWhenStopped_(False)
    content.addSubview_(spinner)
    msg_lbl = label("", NSMakeRect(PAD, MSG_Y, W - 2 * PAD, 18), 12, color=NSColor.secondaryLabelColor())
    msg_lbl.setLineBreakMode_(4)

    check_btn = NSButton.buttonWithTitle_target_action_("立即检查", handler, "check:")
    check_btn.setFrame_(NSMakeRect(PAD - 6, BTN_Y, 116, 36))
    content.addSubview_(check_btn)
    primary = NSButton.buttonWithTitle_target_action_("一键启用", handler, "primary:")
    primary.setFrame_(NSMakeRect(W - PAD - 150 + 6, BTN_Y, 150, 36))
    primary.setEnabled_(False)
    content.addSubview_(primary)
    try:
        for b in (check_btn, primary):
            b.setControlSize_(3)  # NSControlSizeLarge
    except Exception:
        pass

    level_color = {
        "ok": NSColor.systemGreenColor(),
        "warn": NSColor.systemOrangeColor(),
        "crit": NSColor.systemRedColor(),
    }
    state_color = {
        "running": NSColor.systemGreenColor(),
        "idle": NSColor.systemGreenColor(),
        "crashed": NSColor.systemRedColor(),
    }
    last: dict = {}

    def setv(key: str, value, apply) -> None:
        if last.get(key) != value:
            last[key] = value
            apply(value)

    def redraw() -> None:
        """定时器每 0.25 秒调一次，操作后也直接调。只在主线程调用，只改有变化的控件。"""
        now = time.time()
        with lock:
            st, sts, busy = shared["state"], dict(shared["agents"]), shared["busy"]
            msg, msg_level, msg_until = shared["msg"], shared["msg_level"], shared["msg_until"]
        watch_st = sts.get("watch")
        view = summarize(st, now, stale_secs, short, long, monitor_on=watch_st is None or watch_st.loaded)
        lvl = view["level"]
        setv("dot", lvl, lambda v: dot.setFillColor_(level_color.get(v, NSColor.systemGrayColor())))
        setv("title", view["title"], title.setStringValue_)
        setv("detail", view["detail"], detail.setStringValue_)
        for i, (k, v) in enumerate(row_widgets):
            kv = view["rows"][i] if i < len(view["rows"]) else ("", "")
            setv(f"k{i}", kv[0], k.setStringValue_)
            setv(f"v{i}", kv[1], v.setStringValue_)
            if i == 3:
                setv("lockc", kv[1].startswith("异常"),
                     lambda bad: v.setTextColor_(NSColor.systemRedColor() if bad else NSColor.labelColor()))

        for key, (st_lbl, sw) in svc_widgets.items():
            s = sts.get(key)
            text = s.describe() if s else "检测中…"
            setv(f"st_{key}", text, st_lbl.setStringValue_)
            setv(f"stc_{key}", s.state if s else "",
                 lambda v, lb=st_lbl: lb.setTextColor_(state_color.get(v, NSColor.secondaryLabelColor())))
            setv(f"sw_en_{key}", bool(s) and not busy, sw.setEnabled_)
            if s and not busy:
                sw.setState_(NSControlStateValueOn if s.loaded else NSControlStateValueOff)

        all_on = bool(sts) and all(s.loaded for s in sts.values())
        setv("primary", "全部停用" if all_on else "一键启用", primary.setTitle_)
        setv("primary_key", "" if all_on else "\r", primary.setKeyEquivalent_)  # 只有「启用」是回车默认按钮
        setv("primary_en", bool(sts) and not busy, primary.setEnabled_)
        setv("check_en", not busy, check_btn.setEnabled_)

        if busy:
            line, color, spin = busy, "info", True
        elif msg and now < msg_until:
            line, color, spin = msg, msg_level, False
        else:
            line, color, spin = "", "info", False
        setv("spin", spin, lambda on: spinner.startAnimation_(None) if on else spinner.stopAnimation_(None))
        setv("msg_x", spin, lambda on: msg_lbl.setFrame_(NSMakeRect(PAD + (22 if on else 0), MSG_Y, W - 2 * PAD - 22, 18)))
        setv("msg", line, msg_lbl.setStringValue_)
        setv("msgc", color, lambda c: msg_lbl.setTextColor_(
            {"ok": NSColor.systemGreenColor(), "err": NSColor.systemRedColor()}.get(c, NSColor.secondaryLabelColor())))

    def safe_redraw() -> None:
        try:
            redraw()
        except Exception as e:  # ObjC 回调里漏出去的异常会把 App 带崩，记一笔就好
            print(f"redraw: {e!r}", file=sys.stderr)

    # 菜单：⌘W 关窗、⌘Q 退出
    menubar = NSMenu.alloc().init()
    app_item = NSMenuItem.alloc().init()
    menubar.addItem_(app_item)
    app_menu = NSMenu.alloc().init()
    app_menu.addItemWithTitle_action_keyEquivalent_("关闭窗口", "performClose:", "w")
    app_menu.addItem_(NSMenuItem.separatorItem())
    app_menu.addItemWithTitle_action_keyEquivalent_(f"退出 {APP_NAME}", "terminate:", "q")
    app_item.setSubmenu_(app_menu)
    app.setMainMenu_(menubar)

    class AppDelegate(NSObject):
        def applicationShouldTerminateAfterLastWindowClosed_(self, sender):
            return True

        def applicationDidBecomeActive_(self, note):  # 再点一次 App 图标：窗口回到前面
            if win.isMiniaturized():
                win.deminiaturize_(None)
            win.makeKeyAndOrderFront_(None)

    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)

    refresh_state()
    safe_redraw()
    timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(0.25, handler, "tick:", None, True)
    NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
    threading.Thread(target=poller, daemon=True).start()
    if snapshot:
        win.setFrameOrigin_((-20000, -20000))
        win.orderFrontRegardless()
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(3.0, handler, "snap:", None, False)
        app.run()
        return
    win.center()
    win.makeKeyAndOrderFront_(None)
    if hasattr(app, "activate"):
        app.activate()
    else:
        app.activateIgnoringOtherApps_(True)
    age = state_age(load_state(), time.time())
    if age is None or age > AUTO_CHECK_AFTER:
        act("上次检查太久了，正在重新检查…", lambda: check_now(short))
    app.run()


def print_summary() -> int:
    short, long, stale_secs = watch_tables()
    sts = A.statuses()
    watch_st = next((s for s in sts if s.svc.key == "watch"), None)
    view = summarize(load_state(), time.time(), stale_secs, short, long, monitor_on=watch_st is None or watch_st.loaded)
    print(f"[{view['level']}] {view['title']}" + (f"  {view['detail']}" if view["detail"] else ""))
    for k, v in view["rows"]:
        print(f"  {k}\t{v}")
    for s in sts:
        print(f"  {s.svc.name}\t{s.describe()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=f"{APP_NAME}：状态面板，一键启用 / 停用监控和悬浮窗")
    p.add_argument("--icon", help="Dock 图标（.icns），App 启动脚本会传")
    p.add_argument("--summary", action="store_true", help="只在终端打印面板内容，不开窗口")
    p.add_argument("--snapshot", metavar="PNG", help="不弹窗，把面板存成图片就退出（README 截图、验收用）")
    p.add_argument("--appearance", choices=("light", "dark"), help="配合 --snapshot：浅色 / 深色")
    args = p.parse_args(argv)
    if args.summary:
        return print_summary()
    try:
        run_panel(icon=args.icon, snapshot=args.snapshot, appearance=args.appearance)
    except ImportError as e:  # 从 App 启动时没有终端，缺 PyObjC 要弹框说清楚
        msg = f"缺 PyObjC（{e}）。在终端运行：{sys.executable} -m pip install pyobjc-framework-Cocoa"
        subprocess.run(["osascript", "-e", f"display alert {json.dumps(APP_NAME + '打不开', ensure_ascii=False)}"
                        f" message {json.dumps(msg, ensure_ascii=False)} as critical"], capture_output=True)
        print(msg, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
