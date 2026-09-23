#!/usr/bin/env python3
"""Verge 2.5.5 兼容：控制器自动发现、订阅副本不上 uchg、http 覆盖规则集的推送与自检、生成器输出。

控制器用临时 unix socket 上的假 HTTP 服务模拟；「内核」用假 api_json 模拟：PUT 时真去 url 拉规则集并数条数。
不碰真 Verge、真内核。
"""
from __future__ import annotations

import dataclasses
import http.server
import json
import os
import socket
import socketserver
import stat
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import routes as R  # noqa: E402
import verge  # noqa: E402
import watch  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _UnixHTTP(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def serve_version(path: str) -> _UnixHTTP:
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = json.dumps({"version": "v-test"} if self.path == "/version" else {"path": self.path}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = _UnixHTTP(path, H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def dead_socket(path: str) -> None:
    """留下一个没人监听的 socket 文件（sidecar 退出后就是这样）。"""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(path)
    s.close()


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(dir="/tmp")  # unix socket 路径不能超过 104 字节
        self.real = watch.controller_candidates
        watch._ACTIVE = None
        self.servers: list[_UnixHTTP] = []

    def tearDown(self):
        watch.controller_candidates = self.real
        watch._ACTIVE = None
        for s in self.servers:
            s.shutdown()
            s.server_close()

    def use(self, *paths: str) -> None:
        cands = [verge.Controller("unix", p, source=f"t{i}") for i, p in enumerate(paths)]
        watch.controller_candidates = lambda: cands

    def test_skips_stale_socket_file(self):
        dead, live = f"{self.dir}/dead.sock", f"{self.dir}/live.sock"
        dead_socket(dead)
        self.servers.append(serve_version(live))
        self.use(dead, live)
        self.assertTrue(os.path.exists(dead))
        self.assertEqual(watch.find_controller().address, live)
        self.assertTrue(watch.clash_reachable())
        self.assertEqual(watch.api_json("/x"), {"path": "/x"})

    def test_nothing_alive(self):
        dead_socket(f"{self.dir}/dead.sock")
        self.use(f"{self.dir}/dead.sock", f"{self.dir}/missing.sock")
        self.assertFalse(watch.clash_reachable())
        with self.assertRaises(RuntimeError):
            watch.api_json("/version")

    def test_rediscovers_after_core_moves(self):
        a, b = f"{self.dir}/a.sock", f"{self.dir}/b.sock"
        sa = serve_version(a)
        self.use(a, b)
        self.assertEqual(watch.find_controller().address, a)
        # Verge 从 sidecar 切到服务模式：旧 socket 没了，新的在别处
        sa.shutdown()
        sa.server_close()
        os.unlink(a)
        self.servers.append(serve_version(b))
        self.assertEqual(watch.api_json("/y"), {"path": "/y"})
        self.assertEqual(watch.active_controller().address, b)


@unittest.skipUnless(hasattr(os, "chflags"), "uchg 只有 macOS/BSD 有")
class LockTest(unittest.TestCase):
    def setUp(self):
        self.verge = Path(tempfile.mkdtemp())
        (self.verge / "profiles").mkdir()
        (self.verge / "ai-homebb-providers").mkdir()
        self.merge = self.verge / "profiles" / "Merge.yaml"
        self.sub = self.verge / "ai-homebb-providers" / "a.yaml"
        self.merge.write_text("merge\n")
        self.sub.write_text("proxies: []\n")
        self.saved = (watch.SETTINGS, watch.LOCK_PATH)
        s = watch.SETTINGS
        watch.SETTINGS = dataclasses.replace(
            s,
            clash=dataclasses.replace(s.clash, verge_dir=str(self.verge)),
            lock=dataclasses.replace(s.lock, files=(str(self.merge), str(self.verge / "ai-homebb-providers" / "*.yaml"))),
        )
        watch.LOCK_PATH = self.verge / "lock.json"

    def tearDown(self):
        for p in (self.merge, self.sub):
            if p.exists():
                os.chflags(p, p.stat().st_flags & ~stat.UF_IMMUTABLE)
        watch.SETTINGS, watch.LOCK_PATH = self.saved

    def uchg(self, p: Path, on: bool) -> None:
        f = p.stat().st_flags
        os.chflags(p, (f | stat.UF_IMMUTABLE) if on else (f & ~stat.UF_IMMUTABLE))

    def test_pin_migrates_old_lock(self):
        self.uchg(self.sub, True)  # 旧版本给订阅副本上的锁
        self.assertEqual(watch.pin_lock(), 0)
        self.assertTrue(watch.is_immutable(self.merge))
        self.assertFalse(watch.is_immutable(self.sub), "订阅副本必须解开 uchg，否则 Verge 服务起不来内核")
        self.assertEqual(watch.check_lock(), (True, ""))
        self.assertEqual(set(json.loads(watch.LOCK_PATH.read_text())["files"]), {str(self.merge), str(self.sub)})

    def test_uchg_on_runtime_asset_is_flagged(self):
        watch.pin_lock()
        self.uchg(self.sub, True)
        ok, detail = watch.check_lock()
        self.assertFalse(ok)
        self.assertIn("a.yaml 带 uchg", detail)
        self.assertIn("--migrate-lock", detail)

    def test_runtime_asset_still_hashed(self):
        watch.pin_lock()
        self.sub.write_text("proxies: [changed]\n")
        ok, detail = watch.check_lock()
        self.assertFalse(ok)
        self.assertIn("a.yaml 内容变了", detail)

    def write_runtime_config(self):
        """Verge 生成的运行配置：a 是 file 型副本（服务会复制），b 是 url 订阅的缓存（服务不复制）。"""
        (self.verge / "ai-homebb-providers" / "b.yaml").write_text("proxies: [url-cache]\n")
        (self.verge / "clash-verge.yaml").write_text(
            "mixed-port: 7897\nproxy-providers:\n"
            "  sub-a:\n    type: file\n    path: ./ai-homebb-providers/a.yaml\n"
            "  sub-b:\n    type: http\n    url: https://b.example/sub\n    path: ./ai-homebb-providers/b.yaml\n"
            "proxy-groups: []\n")
        return self.verge / "ai-homebb-providers" / "b.yaml"

    def test_url_cache_keeps_old_lock(self):
        b = self.write_runtime_config()
        try:
            watch.pin_lock()
            self.assertFalse(watch.is_immutable(self.sub))
            self.assertTrue(watch.is_immutable(b), "url 订阅缓存服务不复制，保持原来的 uchg 锁，免得订阅刷新误报被改")
            self.assertEqual(watch.check_lock(), (True, ""))
        finally:
            self.uchg(b, False)

    def test_migrate_clears_uchg_without_reapproving(self):
        watch.pin_lock()
        self.uchg(self.sub, True)  # 老用户：锁在旧版本下打的
        before = watch.LOCK_PATH.read_bytes()
        self.assertEqual(watch.migrate_lock(), 0)
        self.assertFalse(watch.is_immutable(self.sub))
        self.assertTrue(watch.is_immutable(self.merge))
        self.assertEqual(watch.LOCK_PATH.read_bytes(), before, "迁移不重算 sha256")
        self.assertEqual(watch.check_lock(), (True, ""))

    def test_migrate_refuses_tampered_file(self):
        watch.pin_lock()
        self.sub.write_text("proxies: [tampered]\n")
        self.uchg(self.sub, True)
        self.assertEqual(watch.migrate_lock(), 1)
        self.assertTrue(watch.is_immutable(self.sub), "内容对不上就不动，留给人核对")

    def test_profile_lock_still_required(self):
        watch.pin_lock()
        self.uchg(self.merge, False)
        ok, detail = watch.check_lock()
        self.assertFalse(ok)
        self.assertIn("Merge.yaml 锁标记(uchg)丢了", detail)


class FakeCore:
    """假内核：GET /providers/rules 报条数；PUT 时像 mihomo 一样按 url 去拉（http 型）或什么都不做（file 型）。"""

    def __init__(self, port: int, vehicle: str = "HTTP"):
        self.port = port
        self.vehicle = vehicle
        self.counts = {n: 0 for n in R.PROVIDER.values()}
        self.puts: list[str] = []

    def api_json(self, path: str, timeout: float = 5.0, method: str = "GET"):
        if path == "/providers/rules" and method == "GET":
            return {"providers": {n: {"ruleCount": c, "vehicleType": self.vehicle} for n, c in self.counts.items()}}
        if path.startswith("/providers/rules/") and method == "PUT":
            name = path.rsplit("/", 1)[1]
            self.puts.append(name)
            if self.vehicle == "HTTP":
                try:
                    text = urllib.request.urlopen(f"http://127.0.0.1:{self.port}/{name}.yaml", timeout=3).read().decode()
                except urllib.error.URLError as e:
                    raise RuntimeError(f"{path} HTTP 503") from e
                self.counts[name] = sum(1 for line in text.splitlines() if line.startswith("  - "))
            return None
        raise AssertionError(path)


class HttpRulesTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.port = free_port()
        self.saved = (R.SETTINGS, watch.api_json, watch.clash_reachable, watch._ACTIVE, R.load_routes)
        R.SETTINGS = dataclasses.replace(R.SETTINGS, routes=dataclasses.replace(
            R.SETTINGS.routes, dir=str(self.dir), file=str(self.dir / "routes.toml"), serve_port=self.port))
        self.core = FakeCore(self.port)
        watch.api_json = self.core.api_json
        watch.clash_reachable = lambda: True

    def tearDown(self):
        R.SETTINGS, watch.api_json, watch.clash_reachable, watch._ACTIVE, R.load_routes = self.saved

    def routes(self):
        return [R.Route("PROCESS-NAME,Telegram", "direct"), R.Route("DOMAIN-SUFFIX,github.com", "daily"),
                R.Route("PROCESS-NAME,Foo", "daily")]

    def test_provider_defs(self):
        defs = R.provider_defs()
        self.assertEqual(list(defs), ["user-homebb", "user-direct", "user-daily"])
        d = defs["user-direct"]
        self.assertEqual(d["type"], "http")
        self.assertEqual(d["url"], f"http://127.0.0.1:{self.port}/user-direct.yaml")
        self.assertEqual(d["proxy"], "DIRECT")
        self.assertEqual(d["interval"], 0)
        self.assertTrue(d["path"].startswith(f"./{R.CACHE_SUBDIR}/"), "缓存要和 route.py 写的源文件分开")

    def test_serving_only_three_files_and_closes(self):
        R.write_providers(self.routes(), self.dir)
        (self.dir / "secret.txt").write_text("x")
        with R.serving(self.dir) as mine:
            self.assertTrue(mine)
            body = urllib.request.urlopen(f"http://127.0.0.1:{self.port}/user-direct.yaml").read().decode()
            self.assertIn("Telegram", body)
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/secret.txt")
            with R.serving(self.dir) as second:
                self.assertFalse(second, "端口已被占用时不抛错")
        with self.assertRaises(urllib.error.URLError):
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/user-direct.yaml", timeout=1)

    def test_apply_pushes_via_http(self):
        ok, msg = R.apply(self.routes())
        self.assertTrue(ok, msg)
        self.assertEqual(self.core.counts, {"user-homebb": 0, "user-direct": 1, "user-daily": 2})
        self.assertIn("直连 1 条", msg)

    def test_resync_only_when_out_of_sync(self):
        R.save_routes(self.routes())
        self.assertIn("已重新推送", R.resync())  # 内核里是 0 条（例如重启后没缓存），按记忆重推
        self.assertEqual(self.core.counts["user-daily"], 2)
        n = len(self.core.puts)
        self.assertEqual(R.resync(), "")
        self.assertEqual(len(self.core.puts), n, "一致时不该再推")
        self.core.counts = {k: 0 for k in self.core.counts}  # 内核换了 runtime 目录、缓存丢了
        self.assertIn("已重新推送", R.resync())
        self.assertEqual(self.core.counts["user-direct"], 1)

    def test_resync_after_edit_while_clash_off(self):
        R.save_routes(self.routes())
        R.apply(self.routes())
        # Clash 关着时把 Foo 从「代理」改成「直连」：条数恰好不变（1→2 / 2→1 换个位置也可能一样），只看条数会漏
        watch.clash_reachable = lambda: False
        rs, _ = R.upsert(self.routes()[:2], "PROCESS-NAME,Bar", "daily")
        R.save_routes(rs)
        self.assertFalse(R.apply(rs)[0])
        watch.clash_reachable = lambda: True
        self.assertEqual(self.core.counts, {"user-homebb": 0, "user-direct": 1, "user-daily": 2})
        self.assertIn("已重新推送", R.resync(), "记忆在上次推送之后改过，要重推")
        body = (self.dir / "user-daily.yaml").read_text(encoding="utf-8")
        self.assertIn("Bar", body)

    def test_resync_no_loop_on_rules_core_rejects(self):
        rs = self.routes() + [R.Route("IP-CIDR,999.1.1.1/32,no-resolve", "direct")]
        R.save_routes(rs)
        real = self.core.api_json

        def picky(path, timeout=5.0, method="GET"):  # 内核不认的规则不计数
            out = real(path, timeout, method)
            if method == "PUT" and path.endswith("user-direct"):
                self.core.counts["user-direct"] -= 1
            return out
        watch.api_json = picky
        ok, _ = R.apply(rs)
        self.assertFalse(ok)
        n = len(self.core.puts)
        self.assertEqual(R.resync(), "")
        self.assertEqual(len(self.core.puts), n, "和上次推完时一致就不重推，免得每轮空转")

    def test_resync_never_pushes_without_memory(self):
        self.core.counts["user-direct"] = 5
        self.assertFalse((self.dir / "routes.toml").exists())
        self.assertEqual(R.resync(), "")
        self.assertEqual(self.core.puts, [], "没有 routes.toml 时不能拿空规则覆盖内核")

    def test_resync_ignores_file_type(self):
        self.core.vehicle = "File"
        R.save_routes(self.routes())
        self.assertEqual(R.resync(), "")
        self.assertEqual(self.core.puts, [])

    def test_port_taken_by_stranger_aborts(self):
        R.write_providers(self.routes(), self.dir)
        other = Path(tempfile.mkdtemp())
        (other / "user-direct.yaml").write_text("payload:\n  - DOMAIN,evil.example\n")
        with R.serving(other):  # 别的程序占着端口、给的内容不一样
            ok, msg = R.push(self.dir, R.want_counts(self.routes()))
        self.assertFalse(ok)
        self.assertIn("被别的程序占着", msg)
        self.assertEqual(self.core.puts, [], "内容不对就不能让内核去拉")

    def test_port_taken_by_same_content_proceeds(self):
        R.write_providers(self.routes(), self.dir)
        with R.serving(self.dir):  # 悬浮窗和监控同时在推同一份
            ok, msg = R.push(self.dir, R.want_counts(self.routes()))
        self.assertTrue(ok, msg)

    def test_file_type_under_service_mode_explains(self):
        self.core.vehicle = "File"
        watch._ACTIVE = verge.Controller("unix", "/var/run/clash-verge-service/users/501/verge-mihomo.sock")
        ok, why = R.hook_installed()
        self.assertFalse(ok)
        self.assertIn("file 型", why)
        ok, msg = R.apply(self.routes())
        self.assertFalse(ok)
        self.assertIn("genconfig.py", msg)

    def test_file_type_in_sidecar_is_fine(self):
        self.core.vehicle = "File"
        watch._ACTIVE = verge.Controller("unix", "/var/folders/x/T/verge-mihomo.sock")
        self.assertEqual(R.hook_installed(), (True, ""))


class GeneratedTest(unittest.TestCase):
    def build(self, direct_ips=()):
        import genconfig
        spec = {
            "homebb": {"nodes": [{"name": "H1", "type": "socks5", "server": "203.0.113.5", "port": 1080}]},
            "daily": {"subscriptions": [{"name": "a", "url": "https://a.example/sub"}]},
            "direct": {"ips": list(direct_ips)},
        }
        files, _ = genconfig.build(spec)
        return files

    def test_rule_providers_are_http(self):
        files = self.build()
        line = next(l for l in files["Merge.yaml"].splitlines() if l.strip().startswith('"user-direct"'))
        d = json.loads(line.split(":", 1)[1])
        self.assertEqual(d["type"], "http")
        self.assertEqual(d["proxy"], "DIRECT")
        self.assertIn(f'serve_port = {R.SETTINGS.routes.serve_port}', files["config.toml"])
        self.assertIn('"type": "http"', files["Script.js"])

    def test_install_md_mentions_verge_tun_exclude(self):
        self.assertNotIn("排除自定义网段", self.build()["INSTALL.md"])
        md = self.build(["203.0.113.10"])["INSTALL.md"]
        self.assertIn("排除自定义网段", md)
        self.assertIn("203.0.113.10/32", md)


if __name__ == "__main__":
    unittest.main()
