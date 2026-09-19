#!/usr/bin/env python3
"""引导脚本（wizard.py）：分享链接解析、TOML 写出往返、增删逻辑。不碰真实文件系统之外的东西。"""
from __future__ import annotations

import base64
import json
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wizard import (
    DEFAULT_SPEC, add_daily, add_home_node, clear_home_sub, describe, dump_toml, load_spec,
    parse_node, remove_daily, remove_home_node, save_spec, set_home_sub,
)


class ParseNodeTest(unittest.TestCase):
    def test_shorthand_host_port_user_pass_defaults_socks5(self):
        d = parse_node("203.0.113.5:1080:user:pass", default_name="h")
        self.assertEqual((d["type"], d["server"], d["port"], d["username"], d["password"]), ("socks5", "203.0.113.5", 1080, "user", "pass"))
        self.assertEqual(parse_node("203.0.113.5:1080", default_name="h", prefer_http=True)["type"], "http")

    def test_shorthand_bad(self):
        with self.assertRaises(ValueError):
            parse_node("203.0.113.5:1080:onlyuser", default_name="h")

    def test_socks5_url_with_encoded_password_and_name(self):
        d = parse_node("socks5://u:p%40ss@203.0.113.5:1080#家宽A")
        self.assertEqual((d["name"], d["username"], d["password"]), ("家宽A", "u", "p@ss"))

    def test_vless_reality(self):
        d = parse_node("vless://uuid-1@e.example.com:443?security=reality&sni=swdist.apple.com&fp=chrome&pbk=PK&sid=ab12&flow=xtls-rprx-vision&type=tcp#B")
        self.assertEqual(d["type"], "vless")
        self.assertTrue(d["tls"])
        self.assertEqual(d["servername"], "swdist.apple.com")
        self.assertEqual(d["reality-opts"], {"public-key": "PK", "short-id": "ab12"})
        self.assertEqual(d["flow"], "xtls-rprx-vision")

    def test_ss_sip002_and_legacy(self):
        sip = "ss://" + base64.urlsafe_b64encode(b"aes-128-gcm:secret").decode().rstrip("=") + "@198.51.100.9:8388#S"
        d = parse_node(sip)
        self.assertEqual((d["cipher"], d["password"], d["port"]), ("aes-128-gcm", "secret", 8388))
        legacy = "ss://" + base64.b64encode(b"chacha20-ietf-poly1305:pw@198.51.100.9:8389").decode() + "#L"
        d2 = parse_node(legacy)
        self.assertEqual((d2["cipher"], d2["password"], d2["port"]), ("chacha20-ietf-poly1305", "pw", 8389))

    def test_trojan_ws(self):
        d = parse_node("trojan://pw@t.example.com:443?sni=t.example.com&type=ws&path=%2Fws&host=t.example.com#T")
        self.assertEqual(d["network"], "ws")
        self.assertEqual(d["ws-opts"]["path"], "/ws")

    def test_vmess(self):
        raw = {"v": "2", "ps": "VM", "add": "v.example.com", "port": "443", "id": "uuid-x", "aid": "0", "net": "ws", "host": "v.example.com", "path": "/ws", "tls": "tls"}
        d = parse_node("vmess://" + base64.b64encode(json.dumps(raw).encode()).decode())
        self.assertEqual((d["name"], d["uuid"], d["network"], d["tls"]), ("VM", "uuid-x", "ws", True))

    def test_hysteria2(self):
        d = parse_node("hysteria2://auth@h.example.com:8443?sni=h.example.com&insecure=1#H")
        self.assertEqual((d["type"], d["password"], d["skip-cert-verify"]), ("hysteria2", "auth", True))

    def test_json_node(self):
        d = parse_node('{"type":"socks5","server":"203.0.113.7","port":1080}', default_name="J")
        self.assertEqual(d["name"], "J")
        with self.assertRaises(ValueError):
            parse_node('{"name":"x"}')

    def test_unknown_scheme(self):
        with self.assertRaises(ValueError):
            parse_node("gopher://x@y:1")


class TomlTest(unittest.TestCase):
    def test_round_trip_full_spec(self):
        spec = json.loads(json.dumps(DEFAULT_SPEC))
        spec["homebb"]["nodes"] = [
            parse_node("vless://u@e.example.com:443?security=reality&pbk=PK&sid=1&sni=s&fp=chrome#A"),
            parse_node("203.0.113.5:1080:u:p", default_name="B"),
        ]
        spec["homebb"]["subscription"] = {"url": "https://home.example/sub"}
        spec["daily"]["subscriptions"] = [{"name": "a", "url": "https://a/sub"}, {"name": "b", "file": "~/b.yaml"}]
        spec["direct"]["ips"] = ["203.0.113.10"]
        self.assertEqual(tomllib.loads(dump_toml(spec)), spec)

    def test_strings_with_quotes_and_unicode(self):
        spec = {"names": {"daily_group": '日常"出口"\\x'}}
        self.assertEqual(tomllib.loads(dump_toml(spec)), spec)

    def test_save_and_load_with_defaults_filled(self):
        p = Path(tempfile.mkdtemp()) / "spec.toml"
        self.assertEqual(load_spec(p)["names"]["ai_group"], "Claude-Only")  # 文件不存在 → 默认
        spec = {"homebb": {"nodes": [parse_node("203.0.113.5:1080", default_name="x")]}, "daily": {"subscriptions": []}}
        save_spec(spec, p)
        back = load_spec(p)
        self.assertEqual(back["homebb"]["nodes"][0]["name"], "x")
        self.assertIn("ai", back)  # 缺的段被补齐
        self.assertEqual(back["dns"]["via_homebb"], True)


class EditTest(unittest.TestCase):
    def spec(self):
        return json.loads(json.dumps(DEFAULT_SPEC))

    def test_add_home_dedupes_names(self):
        s = self.spec()
        add_home_node(s, parse_node("203.0.113.5:1080", default_name="h"))
        add_home_node(s, parse_node("203.0.113.6:1080", default_name="h"))
        self.assertEqual([n["name"] for n in s["homebb"]["nodes"]], ["h", "h-2"])
        self.assertTrue(remove_home_node(s, "h-2"))
        self.assertFalse(remove_home_node(s, "nope"))

    def test_daily_add_replaces_same_name(self):
        s = self.spec()
        add_daily(s, "a", "https://a/1")
        add_daily(s, "a", "https://a/2")
        add_daily(s, "b", "~/b.yaml")
        self.assertEqual(s["daily"]["subscriptions"], [{"name": "a", "url": "https://a/2"}, {"name": "b", "file": "~/b.yaml"}])
        self.assertTrue(remove_daily(s, "a"))
        self.assertFalse(remove_daily(s, "a"))

    def test_home_sub_url_vs_file(self):
        s = self.spec()
        set_home_sub(s, "https://h/sub")
        self.assertEqual(s["homebb"]["subscription"], {"url": "https://h/sub"})
        set_home_sub(s, "~/h.yaml")
        self.assertEqual(s["homebb"]["subscription"], {"file": "~/h.yaml"})
        self.assertTrue(clear_home_sub(s))
        self.assertFalse(clear_home_sub(s))

    def test_describe_mentions_everything(self):
        s = self.spec()
        add_home_node(s, parse_node("203.0.113.5:1080", default_name="h"))
        add_daily(s, "a", "https://a/1")
        text = describe(s)
        for needle in ("h(socks5 203.0.113.5:1080)", "a(url)", "7901"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
