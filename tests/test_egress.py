#!/usr/bin/env python3
"""前台 App 的 Clash 连接 → 家宽 / 日常 / 直连 / 混合。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from egress import classify, format_hud, match_conns, summarize


def conn(process, path, host, chains):
    return {
        "metadata": {
            "process": process,
            "processPath": path,
            "host": host,
        },
        "chains": chains,
    }


CHATGPT_APP = "/Applications/ChatGPT.app"
CODEX = conn(
    "Codex (Service)",
    CHATGPT_APP + "/Contents/Frameworks/Codex Framework.framework/Helpers/x",
    "ab.chatgpt.com",
    ["RESIP-Claude", "Claude-Only"],
)
EDGE_HOME = conn(
    "Microsoft Edge Helper",
    "/Applications/Microsoft Edge.app/Contents/Frameworks/Microsoft Edge Framework.framework/Versions/x/Helpers/Microsoft Edge Helper.app/Contents/MacOS/Microsoft Edge Helper",
    "chatgpt.com",
    ["RESIP-Claude", "Claude-Only"],
)
EDGE_FLOWER = conn(
    "Microsoft Edge Helper",
    "/Applications/Microsoft Edge.app/Contents/Frameworks/Microsoft Edge Framework.framework/Versions/x/Helpers/Microsoft Edge Helper.app/Contents/MacOS/Microsoft Edge Helper",
    "www.youtube.com",
    ["🇭🇰 香港标准 IEPL 专线 2", "Proxies"],
)
CURSOR = conn(
    "Cursor Helper (Plugin)",
    "/Applications/Cursor.app/Contents/Frameworks/Cursor Helper (Plugin).app/Contents/MacOS/Cursor Helper (Plugin)",
    "api2.cursor.sh",
    ["🇭🇰 香港标准 IEPL 专线 2", "Proxies"],
)
TG = conn(
    "Telegram",
    "/Applications/Telegram.app/Contents/MacOS/Telegram",
    "",
    ["🇭🇰 香港标准 IEPL 专线 2", "Proxies", "Telegram"],
)
DOU = conn(
    "DoubaoIme",
    "/Library/Input Methods/DoubaoIme.app/Contents/MacOS/DoubaoIme",
    "",
    ["DIRECT"],
)


class ClassifyTest(unittest.TestCase):
    def test_resip_chain_is_homebb(self):
        self.assertEqual(classify(["RESIP-Claude", "Claude-Only"]), "homebb")

    def test_flower_chain(self):
        self.assertEqual(classify(["🇭🇰 香港标准 IEPL 专线 2", "Proxies"]), "daily")

    def test_direct(self):
        self.assertEqual(classify(["DIRECT"]), "direct")

    def test_empty_unknown(self):
        self.assertEqual(classify([]), "unknown")


class MatchTest(unittest.TestCase):
    def test_chatgpt_app_matches_codex_helper_path(self):
        got = match_conns([CODEX, CURSOR], CHATGPT_APP, "ChatGPT")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["metadata"]["host"], "ab.chatgpt.com")

    def test_edge_name_matches_helper(self):
        got = match_conns([EDGE_HOME, CURSOR], "/Applications/Microsoft Edge.app", "Microsoft Edge")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["metadata"]["host"], "chatgpt.com")

    def test_cursor_does_not_eat_chatgpt(self):
        got = match_conns([CODEX, CURSOR], "/Applications/Cursor.app", "Cursor")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["metadata"]["host"], "api2.cursor.sh")


class SummarizeTest(unittest.TestCase):
    def test_single_homebb(self):
        s = summarize(match_conns([CODEX], CHATGPT_APP, "ChatGPT"))
        self.assertEqual(s.kind, "homebb")
        self.assertIn("ab.chatgpt.com", s.hosts)

    def test_edge_mixed_homebb_and_flower(self):
        s = summarize(match_conns([EDGE_HOME, EDGE_FLOWER], "/Applications/Microsoft Edge.app", "Microsoft Edge"))
        self.assertEqual(s.kind, "mixed")
        self.assertEqual(s.kinds, ("daily", "homebb"))

    def test_chatgpt_hcaptcha_on_homebb_is_homebb(self):
        captcha = conn(
            "Codex (Service)",
            CHATGPT_APP + "/Contents/MacOS/x",
            "dd740826e933.w.hcaptcha.com",
            ["RESIP-Claude", "Claude-Only"],
        )
        s = summarize([CODEX, captcha])
        self.assertEqual(s.kind, "homebb")
        self.assertIn("dd740826e933.w.hcaptcha.com", s.hosts)
        google = conn(
            "Codex (Service)",
            CHATGPT_APP + "/Contents/MacOS/x",
            "optimizationguide-pa.googleapis.com",
            ["🇭🇰 香港标准 IEPL 专线 2", "Proxies", "Google"],
        )
        direct = conn(
            "Codex (Service)",
            CHATGPT_APP + "/Contents/MacOS/x",
            "safebrowsing.googleapis.com",
            ["DIRECT"],
        )
        s = summarize([CODEX, google, direct])
        self.assertEqual(s.kind, "homebb")
        self.assertIn("ab.chatgpt.com", s.hosts)
        self.assertNotIn("optimizationguide-pa.googleapis.com", s.hosts)

    def test_homebb_plus_direct_is_homebb(self):
        s = summarize([CODEX, DOU])
        self.assertEqual(s.kind, "homebb")

    def test_terminal_extra_names_match_curl(self):
        curl = conn("curl", "/usr/bin/curl", "api.ipify.org", ["RESIP-Claude", "Claude-Only"])
        got = match_conns(
            [curl, CURSOR],
            "/Applications/Terminal.app",
            "Terminal",
            extra_names=("curl", "zsh"),
        )
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["metadata"]["process"], "curl")

    def test_pi_node_matches_by_binary_path_not_all_node(self):
        pi_node = conn(
            "node",
            "/opt/homebrew/Cellar/node/25.8.1/bin/node",
            "chatgpt.com",
            ["RESIP-Claude", "Claude-Only"],
        )
        cursor_node = conn(
            "node",
            "/Users/me/.cursor/agent-cli/node",
            "api2direct.cursor.sh",
            ["🇭🇰 香港标准 IEPL 专线 2", "Proxies"],
        )
        got = match_conns(
            [pi_node, cursor_node],
            "/Applications/Terminal.app",
            "Terminal",
            extra_names=("pi", "zsh"),
            extra_paths=("/opt/homebrew/Cellar/node/25.8.1/bin/node",),
        )
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["metadata"]["host"], "chatgpt.com")

    def test_generic_node_name_does_not_steal_other_node(self):
        cursor_node = conn(
            "node",
            "/Users/me/.cursor/agent-cli/node",
            "api2direct.cursor.sh",
            ["🇭🇰 香港标准 IEPL 专线 2", "Proxies"],
        )
        got = match_conns(
            [cursor_node],
            "/Applications/Terminal.app",
            "Terminal",
            extra_names=("node", "pi"),
        )
        self.assertEqual(got, [])

    def test_telegram_flower(self):
        s = summarize(match_conns([TG, DOU], "/Applications/Telegram.app", "Telegram"))
        self.assertEqual(s.kind, "daily")

    def test_idle_app(self):
        s = summarize(match_conns([CURSOR], CHATGPT_APP, "ChatGPT"))
        self.assertEqual(s.kind, "idle")
        self.assertEqual(s.hosts, ())


class FormatTest(unittest.TestCase):
    def test_hud_homebb(self):
        text, color = format_hud("ChatGPT", summarize([CODEX]))
        self.assertIn("ChatGPT", text)
        self.assertIn("家宽", text)
        self.assertIn("ab.chatgpt.com", text)
        self.assertEqual(color, "homebb")

    def test_hud_mixed(self):
        text, color = format_hud("Microsoft Edge", summarize([EDGE_HOME, EDGE_FLOWER]))
        self.assertIn("混合", text)
        self.assertEqual(color, "mixed")

    def test_hud_mixed_tags_each_host_with_its_egress(self):
        s = summarize([EDGE_HOME, EDGE_FLOWER])
        text, _ = format_hud("Microsoft Edge", s)
        lines = text.split("\n")[2:]
        home = [l for l in lines if l.startswith("家宽 · ")]
        daily = [l for l in lines if l.startswith("日常 · ")]
        self.assertTrue(home and daily, text)
        # 家宽在前
        self.assertTrue(lines[0].startswith("家宽 · "), text)
        # 标注的 host 必须来自对应链路
        home_hosts = {h for h, k in s.host_kinds if k == "homebb"}
        daily_hosts = {h for h, k in s.host_kinds if k == "daily"}
        self.assertIn(home[0][len("家宽 · "):], home_hosts)
        self.assertIn(daily[0][len("日常 · "):], daily_hosts)

    def test_hud_single_kind_has_no_tags(self):
        text, _ = format_hud("ChatGPT", summarize([CODEX]))
        self.assertNotIn(" · ", text)

    def test_hud_mixed_per_kind_limit(self):
        def conn(host, chains):
            return {"metadata": {"host": host, "process": "Microsoft Edge Helper"}, "chains": chains}
        conns = [conn(f"h{i}.example.com", ["RESIP-A", "RESIP-Claude", "Claude-Only"]) for i in range(5)]
        conns += [conn(f"d{i}.example.com", ["X", "Proxies"]) for i in range(5)]
        text, _ = format_hud("Microsoft Edge", summarize(conns), limit=3)
        lines = text.split("\n")[2:]
        self.assertEqual(len([l for l in lines if l.startswith("家宽 · ")]), 2)
        self.assertEqual(len([l for l in lines if l.startswith("日常 · ")]), 2)

    def test_hud_idle(self):
        text, color = format_hud("Notes", summarize([]))
        self.assertIn("无连接", text)
        self.assertEqual(color, "idle")

    def test_hud_terminal_idle_zh(self):
        text, color = format_hud("终端", summarize([]), app_path="/Applications/Terminal.app")
        self.assertIn("暂无出站", text)
        self.assertEqual(color, "idle")


if __name__ == "__main__":
    unittest.main()
