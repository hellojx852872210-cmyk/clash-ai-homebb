#!/usr/bin/env python3
"""控制面板顶部的文案：state.json → 判级、说明、各行；不开窗口，不需要 PyObjC。"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import panel as P

SHORT = {"ok": "正常", "homebb_down": "家宽断线，AI 已断（未漏）", "config_tampered": "配置被改/锁标记丢"}
LONG = {"ok": "家宽与日常出口不同，死链完整", "homebb_down": "家宽不通，AI 已 fail-closed（没有掉到日常出口）",
        "config_tampered": "上锁的配置被改或锁标记丢了"}
NOW = datetime(2026, 9, 23, 4, 0, tzinfo=timezone.utc)


def state(age_secs: float = 30, **kw) -> dict:
    st = {
        "ts": (NOW - timedelta(seconds=age_secs)).isoformat(),
        "code": "ok", "level": "ok", "detail": "家宽 203.0.113.5 / 日常 198.51.100.7",
        "homebb_ip": "203.0.113.5", "daily_ip": "198.51.100.7",
        "ai_chain": "RESIP-A/RESIP-Claude/Claude-Only", "lock_ok": True, "lock_detail": "",
    }
    st.update(kw)
    return st


def view(st, stale=900):
    return P.summarize(st, NOW.timestamp(), stale, SHORT, LONG)


class SummarizeTest(unittest.TestCase):
    def test_no_state(self):
        v = view(None)
        self.assertEqual(v["level"], "none")
        self.assertEqual(v["rows"], [])

    def test_ok(self):
        v = view(state())
        self.assertEqual((v["level"], v["title"]), ("ok", "正常"))
        self.assertEqual(v["detail"], "家宽与日常出口不同，死链完整")  # ok 时 detail 就是 IP，下面各行已经有了
        self.assertEqual(dict(v["rows"]), {
            "家宽出口": "203.0.113.5", "日常出口": "198.51.100.7",
            "AI 链路": "RESIP-A → RESIP-Claude → Claude-Only", "配置锁": "正常", "上次检查": "刚刚",
        })

    def test_problem_keeps_level_and_appends_detail(self):
        v = view(state(code="config_tampered", level="crit", detail="Merge.yaml sha256 变了", lock_ok=False,
                       lock_detail="Merge.yaml sha256 变了"))
        self.assertEqual((v["level"], v["title"]), ("crit", "配置被改/锁标记丢"))
        self.assertEqual(v["detail"], "上锁的配置被改或锁标记丢了：Merge.yaml sha256 变了")
        self.assertEqual(dict(v["rows"])["配置锁"], "异常：Merge.yaml sha256 变了")

    def test_missing_fields_show_dash(self):
        v = view(state(code="homebb_down", level="warn", detail="", homebb_ip=None, ai_chain="", lock_ok=None))
        rows = dict(v["rows"])
        self.assertEqual(v["detail"], LONG["homebb_down"])
        self.assertEqual((rows["家宽出口"], rows["AI 链路"], rows["配置锁"]), ("—", "—", "—"))

    def test_stale_overrides_level(self):
        v = view(state(age_secs=1800))
        self.assertEqual(v["level"], "stale")
        self.assertTrue(v["detail"].startswith("结果过期了"))
        self.assertEqual(dict(v["rows"])["上次检查"], "30 分钟前")

    def test_monitor_off_marks_fresh_result_stale(self):
        v = P.summarize(state(age_secs=30), NOW.timestamp(), 900, SHORT, LONG, monitor_on=False)
        self.assertEqual((v["level"], v["title"]), ("stale", "正常"))
        self.assertTrue(v["detail"].startswith("监控已停用"))

    def test_bad_ts_is_stale_and_unknown_code_passes_through(self):
        v = view(state(ts="昨天", code="new_code", level="weird"))
        self.assertEqual((v["level"], v["title"]), ("stale", "new_code"))
        self.assertEqual(dict(v["rows"])["上次检查"], "—")


class HelpersTest(unittest.TestCase):
    def test_ago(self):
        self.assertEqual(P.ago(-5), "刚刚")
        self.assertEqual(P.ago(59), "刚刚")
        self.assertEqual(P.ago(61), "1 分钟前")
        self.assertEqual(P.ago(7200), "2 小时前")
        self.assertEqual(P.ago(3 * 86400), "3 天前")

    def test_naive_ts_is_utc(self):
        st = {"ts": "2026-09-23T03:59:00"}
        self.assertAlmostEqual(P.state_age(st, NOW.timestamp()), 60, places=3)
        self.assertIsNone(P.state_age({}, time.time()))
        self.assertIsNone(P.state_age(None, time.time()))

    def test_load_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "state.json"
            self.assertIsNone(P.load_state(str(p)))
            p.write_text("{半截")
            self.assertIsNone(P.load_state(str(p)))
            p.write_text("[1]")
            self.assertIsNone(P.load_state(str(p)))
            p.write_text(json.dumps({"code": "ok"}))
            self.assertEqual(P.load_state(str(p)), {"code": "ok"})


if __name__ == "__main__":
    unittest.main()
