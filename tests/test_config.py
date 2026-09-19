#!/usr/bin/env python3
"""config.toml 覆盖内置默认；缺省、多余键、list→tuple、路径展开。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Settings, expand_files, load_settings


class ConfigTest(unittest.TestCase):
    def test_defaults_without_file(self):
        s = load_settings(Path(tempfile.mkdtemp()) / "missing.toml")
        self.assertEqual(s.clash.daily_proxy, "http://127.0.0.1:7897")
        self.assertEqual(s.deadchain.homebb_members, ("RESIP-A", "RESIP-B"))
        self.assertEqual(s.probe.direct_ips, ())
        self.assertEqual(s.lock.files, ())
        self.assertIsInstance(s, Settings)

    def test_override_and_ignore_unknown(self):
        p = Path(tempfile.mkdtemp()) / "c.toml"
        p.write_text(
            '[clash]\ndaily_proxy = "http://127.0.0.1:7890"\nbogus = 1\n'
            '[deadchain]\nhomebb_members = ["H1"]\nhomebb_member_type = "Trojan"\n'
            '[probe]\ndirect_ips = ["203.0.113.1"]\n'
            '[alert]\nrealert_secs = 60\nmodal = false\n',
            encoding="utf-8",
        )
        s = load_settings(p)
        self.assertEqual(s.clash.daily_proxy, "http://127.0.0.1:7890")
        self.assertEqual(s.clash.homebb_proxy, "http://127.0.0.1:7901")  # 未覆盖保持默认
        self.assertEqual(s.deadchain.homebb_members, ("H1",))
        self.assertEqual(s.deadchain.homebb_member_type, "Trojan")
        self.assertEqual(s.probe.direct_ips, ("203.0.113.1",))
        self.assertEqual(s.alert.realert_secs, 60)
        self.assertFalse(s.alert.modal)
        self.assertEqual(s.config_path, str(p))

    def test_expand_files_glob_and_home(self):
        d = Path(tempfile.mkdtemp())
        (d / "a.yaml").write_text("x")
        (d / "b.yaml").write_text("y")
        (d / "c.txt").write_text("z")
        got = expand_files((str(d / "*.yaml"), str(d / "c.txt")))
        self.assertEqual([p.name for p in got], ["a.yaml", "b.yaml", "c.txt"])
        home = expand_files(("~/.definitely-missing-file",))
        self.assertEqual(str(home[0]), os.path.expanduser("~/.definitely-missing-file"))


if __name__ == "__main__":
    unittest.main()
