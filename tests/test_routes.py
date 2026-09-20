#!/usr/bin/env python3
"""出口覆盖：输入归一化、安全校验、记忆读写、规则集渲染，以及生成的规则顺序。"""
from __future__ import annotations

import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import routes as R


class NormalizeTest(unittest.TestCase):
    def test_bare_name_is_process(self):
        self.assertEqual(R.normalize_match("Telegram"), "PROCESS-NAME,Telegram")
        self.assertEqual(R.normalize_match("Google Chrome"), "PROCESS-NAME,Google Chrome")

    def test_domain_becomes_suffix(self):
        self.assertEqual(R.normalize_match("github.com"), "DOMAIN-SUFFIX,github.com")
        self.assertEqual(R.normalize_match("*.github.com"), "DOMAIN-SUFFIX,github.com")
        self.assertEqual(R.normalize_match(".github.com"), "DOMAIN-SUFFIX,github.com")

    def test_ip_becomes_cidr_no_resolve(self):
        self.assertEqual(R.normalize_match("203.0.113.9"), "IP-CIDR,203.0.113.9/32,no-resolve")
        self.assertEqual(R.normalize_match("203.0.113.0/24"), "IP-CIDR,203.0.113.0/24,no-resolve")

    def test_full_rule_passes_through_and_uppercases(self):
        self.assertEqual(R.normalize_match("domain-suffix,a.com"), "DOMAIN-SUFFIX,a.com")
        self.assertEqual(R.normalize_match("PROCESS-PATH-REGEX,.*/foo.app/"), "PROCESS-PATH-REGEX,.*/foo.app/")

    def test_empty_and_typeless_comma(self):
        with self.assertRaises(ValueError):
            R.normalize_match("  ")
        # 逗号前不是已知类型 → 当成进程名整体
        self.assertEqual(R.normalize_match("weird,thing"), "PROCESS-NAME,weird,thing")


class TargetTest(unittest.TestCase):
    def test_aliases(self):
        for raw, want in (("家宽", "homebb"), ("直连", "direct"), ("代理", "daily"), ("日常", "daily"),
                          ("HOMEBB", "homebb"), ("d", "direct"), ("proxy", "daily")):
            self.assertEqual(R.parse_target(raw), want, raw)

    def test_unknown(self):
        with self.assertRaises(ValueError):
            R.parse_target("月球")


class ValidateTest(unittest.TestCase):
    def test_ok(self):
        self.assertIsNone(R.validate("PROCESS-NAME,Telegram", "homebb"))
        self.assertIsNone(R.validate("DOMAIN-SUFFIX,github.com", "direct"))

    def test_ai_domain_cannot_leave_homebb(self):
        for t in ("direct", "daily"):
            msg = R.validate("DOMAIN-SUFFIX,claude.ai", t)
            self.assertIsNotNone(msg)
            self.assertIn("AI", msg)
        # 反过来：把 AI 域名设成家宽是允许的（虽然本来就是）
        self.assertIsNone(R.validate("DOMAIN-SUFFIX,claude.ai", "homebb"))

    def test_bad_type_and_empty_payload(self):
        self.assertIn("不支持", R.validate("GEOIP,CN", "direct"))
        self.assertIn("不能为空", R.validate("DOMAIN-SUFFIX,", "direct"))


class MemoryTest(unittest.TestCase):
    def path(self) -> Path:
        return Path(tempfile.mkdtemp()) / "routes.toml"

    def test_round_trip(self):
        p = self.path()
        rs = [R.Route("PROCESS-NAME,Telegram", "homebb", "备注"), R.Route("DOMAIN-SUFFIX,a.com", "direct")]
        R.save_routes(rs, p)
        back = R.load_routes(p)
        self.assertEqual([(r.match, r.target) for r in back], [(r.match, r.target) for r in rs])
        self.assertEqual(back[0].note, "备注")
        tomllib.loads(p.read_text(encoding="utf-8"))  # 合法 TOML

    def test_missing_file_is_empty(self):
        self.assertEqual(R.load_routes(self.path()), [])

    def test_upsert_replaces_same_match(self):
        rs: list[R.Route] = []
        rs, old = R.upsert(rs, "PROCESS-NAME,X", "homebb")
        self.assertIsNone(old)
        rs, old = R.upsert(rs, "PROCESS-NAME,X", "direct")
        self.assertEqual(old, "homebb")
        self.assertEqual(len(rs), 1)
        self.assertEqual(rs[0].target, "direct")

    def test_remove(self):
        rs = [R.Route("PROCESS-NAME,X", "homebb")]
        rs, hit = R.remove(rs, "PROCESS-NAME,X")
        self.assertTrue(hit)
        self.assertEqual(rs, [])
        _, hit = R.remove(rs, "PROCESS-NAME,X")
        self.assertFalse(hit)

    def test_bad_rows_ignored(self):
        p = self.path()
        p.write_text('[[route]]\nmatch = "PROCESS-NAME,A"\ntarget = "homebb"\n\n[[route]]\nmatch = ""\ntarget = "homebb"\n\n[[route]]\nmatch = "X"\ntarget = "月球"\n', encoding="utf-8")
        self.assertEqual([r.match for r in R.load_routes(p)], ["PROCESS-NAME,A"])


class RenderTest(unittest.TestCase):
    def test_payload_per_target(self):
        rs = [R.Route("PROCESS-NAME,A", "homebb"), R.Route("DOMAIN-SUFFIX,b.com", "homebb"), R.Route("DOMAIN-SUFFIX,c.com", "direct")]
        homebb = R.render_payload(rs, "homebb")
        self.assertIn('"PROCESS-NAME,A"', homebb)
        self.assertIn('"DOMAIN-SUFFIX,b.com"', homebb)
        self.assertNotIn("c.com", homebb)
        self.assertIn("payload: []", R.render_payload(rs, "daily"))

    def test_write_providers_creates_three_files(self):
        d = Path(tempfile.mkdtemp()) / "rules"
        got = R.write_providers([R.Route("PROCESS-NAME,A", "homebb")], d)
        self.assertEqual(sorted(p.name for p in got.values()), ["user-daily.yaml", "user-direct.yaml", "user-homebb.yaml"])
        self.assertTrue(all(p.exists() for p in got.values()))
        self.assertIn("PROCESS-NAME,A", got["homebb"].read_text(encoding="utf-8"))


class GeneratedRuleOrderTest(unittest.TestCase):
    """生成的配置里，覆盖规则必须排在 AI 规则之后 —— 这是 AI 改不了出口的保证。"""

    def build(self):
        import genconfig
        spec = {
            "names": {"ai_group": "Claude-Only", "homebb_group": "HB", "daily_group": "日常出口"},
            "homebb": {"nodes": [{"name": "H1", "type": "socks5", "server": "203.0.113.5", "port": 1080}]},
            "daily": {"subscriptions": [{"name": "a", "url": "https://a.example/sub"}]},
        }
        files, _ = genconfig.build(spec)
        return files["Merge.yaml"]

    def test_rule_providers_and_order(self):
        merge = self.build()
        for name in ("user-homebb", "user-direct", "user-daily"):
            self.assertIn(name, merge)
        lines = merge.splitlines()
        ai_last = max(i for i, l in enumerate(lines) if "Claude-Only" in l and "DOMAIN-SUFFIX" in l)
        rs_first = min(i for i, l in enumerate(lines) if "RULE-SET,user-" in l)
        self.assertLess(ai_last, rs_first, "覆盖规则必须排在 AI 域名规则之后")

    def test_targets_point_at_right_groups(self):
        merge = self.build()
        self.assertIn('"RULE-SET,user-homebb,Claude-Only"', merge)
        self.assertIn('"RULE-SET,user-direct,DIRECT"', merge)
        self.assertIn('"RULE-SET,user-daily,日常出口"', merge)


if __name__ == "__main__":
    unittest.main()
