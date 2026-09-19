#!/usr/bin/env python3
"""start.py：根据检测结果给步骤（纯函数）与报告渲染。不做真实检测。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from start import plan, render


def state(**kw) -> dict:
    base = dict(
        os=True, os_detail="Darwin 15.0 arm64", python=True, python_detail="3.12", pyobjc=True, pytest=False, curl=True,
        verge_app=True, verge_dir=True, core_running=True, core_version="v1.19", tun=True, system_proxy=True,
        daily_port=True, homebb_port=True, merge_exists=True, deadchain_installed=True, providers=2,
        spec=True, generated=True, config=True, lock_json=True, lock_ok=True,
        launchd_watch=True, launchd_float=True, daily_ip="198.51.100.1", homebb_ip="192.0.2.1", watch_code="ok",
    )
    base.update(kw)
    return base


class PlanTest(unittest.TestCase):
    def keys(self, **kw):
        return [k for k, _ in plan(state(**kw))]

    def test_all_good_only_activate_and_verify(self):
        self.assertEqual(self.keys(), ["activate", "verify"])

    def test_blockers_stop_everything(self):
        for kw in (dict(os=False), dict(python=False), dict(curl=False), dict(verge_app=False)):
            self.assertEqual(self.keys(**kw), ["stop"], kw)

    def test_fresh_machine_full_path(self):
        self.assertEqual(
            self.keys(core_running=False, spec=False, generated=False, deadchain_installed=False, lock_json=False, launchd_watch=False),
            ["core", "wizard", "generate", "install", "activate", "verify", "pin", "launchd"],
        )

    def test_spec_exists_but_not_generated(self):
        self.assertEqual(self.keys(generated=False, deadchain_installed=False), ["generate", "install", "activate", "verify"])

    def test_lock_broken_asks_to_pin(self):
        self.assertIn("pin", self.keys(lock_ok=False))
        self.assertNotIn("pin", self.keys(lock_ok=True))


class RenderTest(unittest.TestCase):
    def test_marks_and_details(self):
        text = render(state(tun=None, homebb_ip="", pytest=False))
        self.assertIn("✓ macOS", text)
        self.assertIn("✗ pytest", text)
        self.assertIn("– TUN", text)
        self.assertIn("端口在但拨不通", text)
        self.assertIn("监控最近一轮：ok", text)
        self.assertIn("出口 198.51.100.1", text)
