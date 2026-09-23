#!/usr/bin/env python3
"""verge.py：Verge 各版本的控制器位置、服务模式失败日志、哪些文件不能上 uchg。纯函数，不碰真 Verge。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import verge  # noqa: E402

# 2026-09-23 真实日志摘录（Verge 2.5.5，provider 副本带 uchg）
LOG_UCHG = """\
[2026-09-23 09:44:45.446] INFO [Service] running privileged service action ForceReinstall
[2026-09-23 09:44:49.526] INFO [Service] 服务已运行且版本匹配，直接使用
[2026-09-23 09:44:49.541] ERROR [Service] 启动核心失败 (code 1006): Failed to start owner core: failed to materialize the runtime generation: failed to write the runtime asset ai-homebb-providers/airport-a.yaml: Operation not permitted (os error 1)
[2026-09-23 09:44:52.319] INFO [Core] Core running mode changed: NotRunning -> Sidecar
"""
LOG_RECOVERED = LOG_UCHG + """\
[2026-09-23 07:00:53.546] INFO [Service] 服务已运行且版本匹配，直接使用
[2026-09-23 07:00:54.366] INFO [Service] 服务成功启动核心 (session generation 1)
[2026-09-23 07:00:54.366] INFO [Core] Core running mode changed: NotRunning -> Service
"""


class CandidatesTest(unittest.TestCase):
    def addrs(self, **kw):
        return [(c.kind, c.address) for c in verge.candidates(**kw)]

    def test_auto_order(self):
        conf = {"external-controller-unix": "/var/folders/x/T/verge-mihomo.sock",
                "external-controller": "127.0.0.1:9097", "secret": "s"}
        got = self.addrs(verge_conf=conf, uid=501, tmpdir="/var/folders/x/T/")
        self.assertEqual(got, [
            ("unix", "/var/run/clash-verge-service/users/501/verge-mihomo.sock"),
            ("unix", "/var/folders/x/T/verge-mihomo.sock"),  # config.yaml 和 $TMPDIR 指向同一个，只留一次
            ("unix", "/tmp/verge/verge-mihomo.sock"),
            ("tcp", "127.0.0.1:9097"),
        ])
        tcp = verge.candidates(verge_conf=conf, uid=501)[-1]
        self.assertEqual(tcp.secret, "s")

    def test_configured_first(self):
        got = self.addrs(socket="/x.sock", controller="127.0.0.1:9", uid=501)
        self.assertEqual(got[:2], [("unix", "/x.sock"), ("tcp", "127.0.0.1:9")])

    def test_auto_off_only_configured(self):
        self.assertEqual(self.addrs(socket="/x.sock", auto=False), [("unix", "/x.sock")])
        self.assertEqual(self.addrs(auto=False), [])

    def test_is_service(self):
        self.assertTrue(verge.Controller("unix", "/var/run/clash-verge-service/users/501/verge-mihomo.sock").is_service)
        self.assertTrue(verge.Controller("unix", "/private/var/run/clash-verge-service/users/501/verge-mihomo.sock").is_service)
        self.assertFalse(verge.Controller("unix", "/tmp/verge/verge-mihomo.sock").is_service)
        self.assertFalse(verge.Controller("tcp", "127.0.0.1:9097").is_service)


class ScalarsTest(unittest.TestCase):
    def test_top_level_only(self):
        text = ("mixed-port: 7897\nexternal-controller: '127.0.0.1:9097'\ntun:\n  stack: system\n"
                "secret: \"abc\"\nexternal-controller-unix: /var/folders/x/T/verge-mihomo.sock\n")
        got = verge.top_level_scalars(text)
        self.assertEqual(got["external-controller"], "127.0.0.1:9097")
        self.assertEqual(got["secret"], "abc")
        self.assertEqual(got["external-controller-unix"], "/var/folders/x/T/verge-mihomo.sock")
        self.assertNotIn("stack", got)
        self.assertNotIn("tun", got)


class ServiceFailureTest(unittest.TestCase):
    def test_uchg_failure_reported(self):
        why = verge.service_failure(LOG_UCHG)
        self.assertIn("Operation not permitted", why)
        self.assertEqual(verge.failed_asset(why), "ai-homebb-providers/airport-a.yaml")
        hint = verge.failure_hint(why)
        self.assertIn("watch.py --migrate-lock", hint)
        self.assertIn("ai-homebb-providers/airport-a.yaml", hint)

    def test_only_service_lines(self):
        log = ("[x] ERROR [Core] 启动核心失败 (code 1): 配置不对\n"
               "[x] INFO [Core] Core running mode changed: NotRunning -> Sidecar\n")
        self.assertEqual(verge.service_failure(log), "")

    def test_recovered_is_quiet(self):
        self.assertEqual(verge.service_failure(LOG_RECOVERED), "")

    def test_plain_sidecar_is_quiet(self):
        log = "[x] INFO [Core] Core running mode changed: NotRunning -> Sidecar\n"
        self.assertEqual(verge.service_failure(log), "")

    def test_english_wording(self):
        log = ("[x] ERROR [Service] Failed to start core (code 1006): boom\n"
               "[x] INFO [Core] Core running mode changed: NotRunning -> Sidecar\n")
        self.assertEqual(verge.service_failure(log), "boom")

    def test_other_reason_generic_hint(self):
        self.assertIn("服务模式", verge.failure_hint("IPC path unavailable"))

    def test_read_log_tail(self):
        d = Path(tempfile.mkdtemp())
        (d / "logs").mkdir()
        (d / "logs" / "latest.log").write_text("a" * 100 + "\nTAIL\n", encoding="utf-8")
        self.assertTrue(verge.read_log_tail(d, limit=10).endswith("TAIL\n"))
        self.assertEqual(verge.read_log_tail(d / "nope"), "")


class RuntimeAssetTest(unittest.TestCase):
    RUNTIME = """\
mixed-port: 7897
proxy-providers:
  sub-airport-a:
    type: file
    path: /V/ai-homebb-providers/airport-a.yaml
    exclude-filter: 到期|流量
  sub-url:
    type: http
    url: https://a.example/sub?x=1
    path: ./ai-homebb-providers/url.yaml
  inline-one: {type: file, path: ./ai-homebb-providers/inline.yaml}
rule-providers:
  user-direct:
    type: file
    behavior: classical
    path: ./ai-homebb-rules/user-direct.yaml
proxy-groups:
  - name: X
    type: select
"""

    def test_provider_entries(self):
        got = [(e["section"], e["name"], e["type"], e["path"]) for e in verge.provider_entries(self.RUNTIME)]
        self.assertEqual(got, [
            ("proxy-providers", "sub-airport-a", "file", "/V/ai-homebb-providers/airport-a.yaml"),
            ("proxy-providers", "sub-url", "http", "./ai-homebb-providers/url.yaml"),
            ("proxy-providers", "inline-one", "file", "./ai-homebb-providers/inline.yaml"),
            ("rule-providers", "user-direct", "file", "./ai-homebb-rules/user-direct.yaml"),
        ])

    def test_file_asset_paths(self):
        d = Path(tempfile.mkdtemp()).resolve()
        (d / "clash-verge.yaml").write_text(self.RUNTIME.replace("/V/", f"{d}/"), encoding="utf-8")
        got = verge.file_asset_paths(d)
        self.assertIn(d / "ai-homebb-providers" / "airport-a.yaml", got)
        self.assertIn(d / "ai-homebb-providers" / "inline.yaml", got)
        self.assertIn(d / "ai-homebb-rules" / "user-direct.yaml", got)
        self.assertNotIn(d / "ai-homebb-providers" / "url.yaml", got, "url 订阅缓存服务不复制")
        self.assertTrue(verge.is_runtime_asset(d / "ai-homebb-providers" / "airport-a.yaml", d, got))
        self.assertFalse(verge.is_runtime_asset(d / "ai-homebb-providers" / "url.yaml", d, got))
        self.assertIsNone(verge.file_asset_paths(d / "nope"))

    def test_classification(self):
        d = Path(tempfile.mkdtemp())
        (d / "profiles").mkdir()
        (d / "ai-homebb-providers").mkdir()
        self.assertTrue(verge.is_runtime_asset(d / "ai-homebb-providers" / "a.yaml", d))
        self.assertFalse(verge.is_runtime_asset(d / "profiles" / "Merge.yaml", d))
        self.assertFalse(verge.is_runtime_asset(Path(tempfile.mkdtemp()) / "x.yaml", d))


if __name__ == "__main__":
    unittest.main()
