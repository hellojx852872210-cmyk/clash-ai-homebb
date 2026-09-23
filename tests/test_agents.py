#!/usr/bin/env python3
"""launchd 任务：launchctl 输出解析、按脚本路径认任务、状态判断、模板渲染、启停时调了哪些 launchctl。"""
from __future__ import annotations

import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agents as A

WATCH, FLOAT = A.SERVICES["watch"], A.SERVICES["float"]

PRINT_RUNNING = """gui/501/com.x.float = {
\tactive count = 1
\tpath = /Users/u/Library/LaunchAgents/com.x.float.plist
\ttype = LaunchAgent
\tstate = running

\tprogram = /usr/bin/python3
\targuments = {
\t\t/usr/bin/python3
\t\t/p/float.py
\t}

\tpid = 71413
\tlast exit code = (never exited)
\tspawn type = interactive (4)
\tproperties = {
\t\tstate = active
\t}
}
"""

PRINT_DISABLED = """\tdisabled services = {
\t\t"com.google.keystone.agent" => disabled
\t\t"com.x.watch" => enabled
\t\t"com.old.style" => true
\t\t"com.old.enabled" => false
\t}
"""


def write_plist(d: Path, name: str, data: dict, fmt=plistlib.FMT_XML) -> Path:
    p = d / f"{name}.plist"
    p.write_bytes(plistlib.dumps(data, fmt=fmt))
    return p


class ParseTest(unittest.TestCase):
    def test_print_keeps_top_level_only(self):
        info = A.parse_print(PRINT_RUNNING)
        self.assertEqual(info["state"], "running")  # 不是嵌套块里的 active
        self.assertEqual(info["pid"], "71413")
        self.assertEqual(info["last exit code"], "(never exited)")
        self.assertEqual(info["path"], "/Users/u/Library/LaunchAgents/com.x.float.plist")
        self.assertNotIn("/p/float.py", info.values())

    def test_disabled_new_and_old_format(self):
        dis = A.parse_disabled(PRINT_DISABLED)
        self.assertTrue(dis["com.google.keystone.agent"])
        self.assertFalse(dis["com.x.watch"])
        self.assertTrue(dis["com.old.style"])
        self.assertFalse(dis["com.old.enabled"])

    def test_exit_code(self):
        self.assertEqual(A._exit_code("1"), 1)
        self.assertEqual(A._exit_code("-9"), -9)
        self.assertEqual(A._exit_code("15: Terminated: 15"), 15)
        self.assertIsNone(A._exit_code("(never exited)"))
        self.assertIsNone(A._exit_code(""))


class FindPlistsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.project, self.agents = base / "proj", base / "LaunchAgents"
        self.project.mkdir()
        self.agents.mkdir()
        for f in ("watch.py", "float.py"):
            (self.project / f).write_text("")

    def tearDown(self):
        self.tmp.cleanup()

    def test_matches_by_script_path_not_label(self):
        write_plist(self.agents, "com.me.homebb-watch",
                    {"Label": "com.me.homebb-watch", "ProgramArguments": ["/usr/bin/python3", str(self.project / "watch.py")]})
        write_plist(self.agents, "com.other", {"Label": "com.other", "ProgramArguments": ["/usr/bin/true"]})
        write_plist(self.agents, "com.elsewhere",  # 别的目录里同名脚本不算
                    {"Label": "com.elsewhere", "ProgramArguments": ["/usr/bin/python3", "/somewhere/else/watch.py"]})
        (self.agents / "broken.plist").write_text("<plist>不是合法的")
        found = A.find_plists(WATCH, self.agents, self.project)
        self.assertEqual([label for label, _ in found], ["com.me.homebb-watch"])
        self.assertEqual(A.find_plists(FLOAT, self.agents, self.project), [])

    def test_canonical_label_first_and_binary_plist(self):
        args = {"ProgramArguments": ["/opt/homebrew/bin/python3", str(self.project / "float.py")]}
        write_plist(self.agents, "aaa.legacy", {"Label": "aaa.legacy", **args})
        write_plist(self.agents, FLOAT.label, {"Label": FLOAT.label, **args}, fmt=plistlib.FMT_BINARY)
        found = A.find_plists(FLOAT, self.agents, self.project)
        self.assertEqual([label for label, _ in found], [FLOAT.label, "aaa.legacy"])

    def test_program_key_and_symlinked_project(self):
        link = Path(self.tmp.name) / "proj-link"
        link.symlink_to(self.project)
        write_plist(self.agents, "com.prog", {"Label": "com.prog", "Program": str(link / "float.py")})
        found = A.find_plists(FLOAT, self.agents, self.project)
        self.assertEqual([label for label, _ in found], ["com.prog"])


class StatusStateTest(unittest.TestCase):
    def st(self, svc, **kw):
        kw.setdefault("plist", Path("/x.plist"))
        return A.Status(svc, "lbl", **kw)

    def test_states(self):
        self.assertEqual(self.st(FLOAT, loaded=True, pid=5).state, "running")
        self.assertEqual(self.st(WATCH, loaded=True).state, "idle")  # 定时任务两轮之间没进程是正常的
        self.assertEqual(self.st(FLOAT, loaded=True, last_exit="1").state, "crashed")
        self.assertEqual(self.st(FLOAT, loaded=True, last_exit="(never exited)").state, "starting")
        self.assertEqual(self.st(FLOAT, loaded=True, last_exit="0").state, "starting")
        self.assertEqual(self.st(FLOAT, disabled=True).state, "disabled")
        self.assertEqual(self.st(FLOAT).state, "stopped")
        self.assertEqual(self.st(FLOAT, plist=None).state, "missing")
        # 加载着但 plist 被删了：还是按加载状态报
        self.assertEqual(self.st(FLOAT, plist=None, loaded=True, pid=9).state, "running")

    def test_describe(self):
        self.assertEqual(self.st(FLOAT, loaded=True, pid=71413).describe(), "运行中 · PID 71413")
        self.assertEqual(self.st(WATCH, loaded=True, interval=180).describe(), "待命 · 每 3 分钟一轮")
        self.assertEqual(self.st(FLOAT, loaded=True, last_exit="1").describe(), "退出了（退出码 1）")
        self.assertIn("另有重复任务 com.dup", self.st(FLOAT, loaded=True, pid=1, others=["com.dup"]).describe())


class RenderTest(unittest.TestCase):
    def test_templates_render_to_valid_plists(self):
        for svc in A.SERVICES.values():
            tpl = (ROOT / "launchd" / f"{svc.label}.plist.in").read_text()
            data = plistlib.loads(A.render_plist(tpl, Path("/p q"), "/opt/py").encode())
            self.assertEqual(data["Label"], svc.label)
            self.assertEqual(data["ProgramArguments"], ["/opt/py", f"/p q/{svc.script}"])
            self.assertEqual(data["WorkingDirectory"], "/p q")
            self.assertNotIn("__", plistlib.dumps(data).decode())


class FakeLaunchctl:
    """记下调用；loaded 里的 label 算已加载，bootstrap / bootout 会改它。"""

    def __init__(self, loaded=(), disabled=()):
        self.calls: list[tuple[str, ...]] = []
        self.loaded = set(loaded)
        self.disabled = set(disabled)

    def __call__(self, *args, timeout=20):
        self.calls.append(args)
        cmd = args[0]
        if cmd == "print-disabled":
            return 0, "".join(f'\t\t"{lb}" => disabled\n' for lb in self.disabled)
        if cmd == "print":
            label = args[1].split("/", 2)[2]
            return (0, "\tstate = running\n\tpid = 42\n") if label in self.loaded else (113, "Could not find service")
        if cmd == "bootstrap":
            self.loaded.add(plistlib.loads(Path(args[2]).read_bytes())["Label"])
            return 0, ""
        if cmd == "bootout":
            label = args[1].split("/", 2)[2]
            if label in self.loaded:
                self.loaded.discard(label)
                return 0, ""
            return 3, "No such process"
        if cmd == "disable":
            self.disabled.add(args[1].split("/", 2)[2])
        if cmd == "enable":
            self.disabled.discard(args[1].split("/", 2)[2])
        return 0, ""

    def names(self):
        return [c[0] for c in self.calls if c[0] not in ("print", "print-disabled")]


class EnableDisableTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.project, self.agents = base / "proj", base / "LaunchAgents"
        self.project.mkdir()
        self.agents.mkdir()
        (self.project / "float.py").write_text("")
        self.legacy = write_plist(self.agents, "com.me.float", {
            "Label": "com.me.float", "ProgramArguments": ["/usr/bin/python3", str(self.project / "float.py")]})
        real_find = A.find_plists
        patches = [
            mock.patch.object(A, "find_plists", lambda svc, agents_dir=None, project=None: real_find(svc, self.agents, self.project)),
            mock.patch.object(A, "AGENTS_DIR", self.agents),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_enable_reuses_existing_plist_and_clears_disable(self):
        lc = FakeLaunchctl(disabled={"com.me.float"})
        with mock.patch.object(A, "_lc", lc):
            self.assertEqual(A.enable(FLOAT), "已启用悬浮窗")
        self.assertEqual(lc.names(), ["enable", "bootstrap"])  # 先 enable，否则停用过的 bootstrap 会失败
        self.assertEqual(lc.calls[-1][2], str(self.legacy))
        self.assertFalse((self.agents / f"{FLOAT.label}.plist").exists())  # 不另装一份标准 label

    def test_enable_when_already_running_is_noop(self):
        lc = FakeLaunchctl(loaded={"com.me.float"})
        with mock.patch.object(A, "_lc", lc):
            self.assertEqual(A.enable(FLOAT), "悬浮窗已经在跑")
        self.assertEqual(lc.names(), [])

    def test_enable_installs_canonical_plist_when_none(self):
        self.legacy.unlink()
        lc = FakeLaunchctl()
        with mock.patch.object(A, "_lc", lc), mock.patch.object(A, "pick_python", return_value="/opt/py"):
            A.enable(FLOAT)
        dst = self.agents / f"{FLOAT.label}.plist"
        self.assertTrue(dst.exists())
        self.assertEqual(plistlib.loads(dst.read_bytes())["ProgramArguments"][0], "/opt/py")
        self.assertEqual(lc.names(), ["enable", "bootstrap"])
        self.assertIn(FLOAT.label, lc.loaded)

    def test_enable_without_python_raises(self):
        self.legacy.unlink()
        with mock.patch.object(A, "_lc", FakeLaunchctl()), mock.patch.object(A, "pick_python", return_value=None):
            with self.assertRaises(A.AgentError):
                A.enable(FLOAT)

    def test_enable_reports_bootstrap_failure(self):
        lc = FakeLaunchctl()
        real = lc.__call__

        def failing(*args, timeout=20):
            if args[0] == "bootstrap":
                lc.calls.append(args)
                return 5, "Bootstrap failed: 5: Input/output error"
            return real(*args, timeout=timeout)

        with mock.patch.object(A, "_lc", failing):
            with self.assertRaisesRegex(A.AgentError, "Input/output error"):
                A.enable(FLOAT)

    def test_disable_boots_out_and_disables_every_copy(self):
        write_plist(self.agents, FLOAT.label, {
            "Label": FLOAT.label, "ProgramArguments": ["/usr/bin/python3", str(self.project / "float.py")]})
        lc = FakeLaunchctl(loaded={"com.me.float", FLOAT.label})
        with mock.patch.object(A, "_lc", lc):
            self.assertEqual(A.disable(FLOAT), "已停用悬浮窗")
        self.assertEqual(lc.loaded, set())
        self.assertEqual(lc.disabled, {"com.me.float", FLOAT.label})  # 下次登录也不会自己起来

    def test_status_prefers_running_copy_and_lists_duplicates(self):
        write_plist(self.agents, FLOAT.label, {
            "Label": FLOAT.label, "ProgramArguments": ["/usr/bin/python3", str(self.project / "float.py")]})
        lc = FakeLaunchctl(loaded={"com.me.float", FLOAT.label})
        with mock.patch.object(A, "_lc", lc):
            st = A.status(FLOAT)
        self.assertEqual(st.label, FLOAT.label)
        self.assertEqual(st.others, ["com.me.float"])
        self.assertEqual(st.state, "running")


if __name__ == "__main__":
    unittest.main()
