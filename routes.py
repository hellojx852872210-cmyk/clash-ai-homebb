"""出口覆盖规则：记住「哪个 App / 域名走家宽还是直连还是代理」，并渲染成 mihomo 规则集。

记忆存在项目目录的 routes.toml；渲染出三个 classical 规则集文件：
  <rules_dir>/user-homebb.yaml  → AI 组（家宽）
  <rules_dir>/user-direct.yaml  → DIRECT
  <rules_dir>/user-daily.yaml   → 日常出口
内核通过 http 型 rule-provider 读它们：改动时本项目临时在 127.0.0.1 上提供文件并让内核现拉（见 provider_defs），
不用重载整份配置、不用解锁 Merge.yaml，Verge 服务模式下也能即时生效。

三条 RULE-SET 规则排在 AI 死链规则之后，所以覆盖规则永远改不动 AI 的出口（实测：
把 claude.ai 写进 user-direct 仍然走家宽）。因此 validate() 直接拒绝这类无效规则。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import urllib.request
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from config import SETTINGS, expand_path
from egress import is_ai_host

TARGETS = ("homebb", "direct", "daily")
TARGET_CN = {"homebb": "家宽", "direct": "直连", "daily": "代理"}
TARGET_ALIAS = {
    "家宽": "homebb", "homebb": "homebb", "home": "homebb", "h": "homebb",
    "直连": "direct", "direct": "direct", "d": "direct",
    "代理": "daily", "日常": "daily", "daily": "daily", "proxy": "daily", "p": "daily",
}
PROVIDER = {"homebb": "user-homebb", "direct": "user-direct", "daily": "user-daily"}
# 能写进 classical 规则集、且日常够用的匹配类型
RULE_TYPES = (
    "DOMAIN", "DOMAIN-SUFFIX", "DOMAIN-KEYWORD", "DOMAIN-REGEX",
    "IP-CIDR", "IP-CIDR6", "IP-SUFFIX",
    "PROCESS-NAME", "PROCESS-PATH", "PROCESS-NAME-REGEX", "PROCESS-PATH-REGEX",
    "DST-PORT", "NETWORK",
)
_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}(/\d{1,2})?$")


@dataclass(frozen=True)
class Route:
    match: str          # 完整规则，例 "PROCESS-NAME,Telegram"
    target: str         # homebb / direct / daily
    note: str = ""

    @property
    def kind(self) -> str:
        return self.match.split(",", 1)[0]

    @property
    def payload(self) -> str:
        return self.match.split(",", 1)[1] if "," in self.match else ""

    def describe(self) -> str:
        what = "进程" if self.kind.startswith("PROCESS") else ("网段" if self.kind.startswith("IP") else "域名")
        return f"{what} {self.payload}  →  {TARGET_CN.get(self.target, self.target)}"


def parse_target(raw: str) -> str:
    t = TARGET_ALIAS.get((raw or "").strip().lower())
    if not t:
        raise ValueError(f"不认识的出口：{raw}（只能是 家宽 / 直连 / 代理）")
    return t


def normalize_match(raw: str) -> str:
    """把用户输入变成一条完整规则。
    "Telegram" → PROCESS-NAME,Telegram；"github.com" → DOMAIN-SUFFIX,github.com；
    "1.2.3.4" → IP-CIDR,1.2.3.4/32,no-resolve；已经是完整规则就规范化类型名。
    """
    s = (raw or "").strip()
    if not s:
        raise ValueError("不能为空")
    head, sep, rest = s.partition(",")
    head_up = head.strip().upper()
    if sep and head_up in RULE_TYPES:
        rest = rest.strip()
        if not rest:
            raise ValueError(f"{head_up} 后面没有内容")
        return f"{head_up},{rest}"
    if sep and head_up.replace("_", "-") in RULE_TYPES:
        return f"{head_up.replace('_', '-')},{rest.strip()}"
    if _IPV4.match(s):
        return f"IP-CIDR,{s if '/' in s else s + '/32'},no-resolve"
    if "." in s and " " not in s and not s.endswith("."):
        return f"DOMAIN-SUFFIX,{s.lstrip('*.').lstrip('.')}"
    return f"PROCESS-NAME,{s}"


def validate(match: str, target: str) -> str | None:
    """返回错误说明；None 表示可以添加。"""
    if target not in TARGETS:
        return f"不认识的出口：{target}"
    kind, _, payload = match.partition(",")
    if kind not in RULE_TYPES:
        return f"不支持的匹配类型：{kind}"
    if not payload.strip():
        return "匹配内容不能为空"
    if target != "homebb" and kind.startswith("DOMAIN") and is_ai_host(payload.split(",")[0]):
        return (f"{payload} 是 AI 域名，死链规则排在覆盖规则前面，改成{TARGET_CN[target]}不会生效"
                "（这是有意的：AI 只能走家宽）")
    return None


# ---------------- 记忆文件 ----------------
def routes_path() -> Path:
    p = expand_path(SETTINGS.routes.file)
    return p if p.is_absolute() else Path(__file__).resolve().parent / p


def rules_dir() -> Path:
    return expand_path(SETTINGS.routes.dir)


def load_routes(path: Path | None = None) -> list[Route]:
    p = path or routes_path()
    if not p.exists():
        return []
    with p.open("rb") as f:
        data = tomllib.load(f)
    out: list[Route] = []
    for r in data.get("route") or []:
        m, t = str(r.get("match") or ""), str(r.get("target") or "")
        if m and t in TARGETS:
            out.append(Route(m, t, str(r.get("note") or "")))
    return out


def save_routes(routes: list[Route], path: Path | None = None) -> Path:
    p = path or routes_path()
    lines = ["# 出口覆盖规则，由 route.py 维护。每条记「什么 → 走哪」。", ""]
    for r in routes:
        lines.append("[[route]]")
        lines.append(f"match = {json.dumps(r.match, ensure_ascii=False)}")
        lines.append(f"target = {json.dumps(r.target)}")
        if r.note:
            lines.append(f"note = {json.dumps(r.note, ensure_ascii=False)}")
        lines.append("")
    text = "\n".join(lines).rstrip("\n") + "\n"
    tomllib.loads(text)  # 写出前自检
    p.write_text(text, encoding="utf-8")
    return p


def upsert(routes: list[Route], match: str, target: str, note: str = "") -> tuple[list[Route], str | None]:
    """同一个 match 只保留一条；返回 (新列表, 被替换掉的旧 target)。"""
    old = next((r.target for r in routes if r.match == match), None)
    kept = [r for r in routes if r.match != match]
    kept.append(Route(match, target, note or f"{date.today().isoformat()} 设为{TARGET_CN[target]}"))
    return kept, old


def remove(routes: list[Route], match: str) -> tuple[list[Route], bool]:
    kept = [r for r in routes if r.match != match]
    return kept, len(kept) != len(routes)


# ---------------- 渲染规则集 ----------------
def render_payload(routes: list[Route], target: str) -> str:
    items = [r.match for r in routes if r.target == target]
    head = f"# 由 route.py 生成，对应出口：{TARGET_CN[target]}。手改会在下次 route.py 操作时被覆盖。\n"
    if not items:
        return head + "payload: []\n"
    return head + "payload:\n" + "".join(f"  - {json.dumps(i, ensure_ascii=False)}\n" for i in items)


def write_providers(routes: list[Route], directory: Path | None = None) -> dict[str, Path]:
    d = directory or rules_dir()
    d.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path] = {}
    for t in TARGETS:
        p = d / f"{PROVIDER[t]}.yaml"
        p.write_text(render_payload(routes, t), encoding="utf-8")
        out[t] = p
    return out


# ---------------- 让 mihomo 生效 ----------------
# 规则集用 http 型，由本项目在改动的那一刻临时在 127.0.0.1 上提供文件，PUT 让内核现拉：
# Verge 2.5.5 服务模式会把 file 型规则集复制成副本再给内核用，改源文件 + PUT 读到的还是旧副本（实测条数不变）；
# http 型 Verge 不复制，交给内核自己拉，缓存放在独立目录，和这里写的源文件互不覆盖。
# interval 0：只在内核启动且没缓存、或我们 PUT 时才拉；proxy DIRECT：拉取别被规则送进代理。
CACHE_SUBDIR = "ai-homebb-rules-cache"


def provider_defs(port: int | None = None) -> dict[str, dict]:
    """三个覆盖规则集的 mihomo 定义（genconfig 和 route.py hook 都用它）。"""
    port = port or SETTINGS.routes.serve_port
    return {
        PROVIDER[t]: {
            "type": "http", "behavior": "classical",
            "url": f"http://127.0.0.1:{port}/{PROVIDER[t]}.yaml",
            "path": f"./{CACHE_SUBDIR}/{PROVIDER[t]}.yaml",
            "proxy": "DIRECT", "interval": 0,
        }
        for t in TARGETS
    }


@contextmanager
def serving(directory: Path, port: int | None = None) -> Iterator[bool]:
    """在 127.0.0.1:port 上临时提供三个规则集文件，退出即关。

    端口已被占用时 yield False 并照常往下走：多半是悬浮窗和监控同时在推同一份文件；
    若占端口的是别的程序，内核拉到的内容不对，apply() 的条数核对会报出来。
    """
    port = port or SETTINGS.routes.serve_port
    names = {f"/{PROVIDER[t]}.yaml" for t in TARGETS}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path not in names:
                self.send_error(404)
                return
            try:
                body = (directory / self.path[1:]).read_bytes()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/yaml; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        yield False
        return
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield True
    finally:
        srv.shutdown()
        srv.server_close()


def _providers(timeout: float = 5.0) -> dict[str, dict]:
    from watch import api_json

    return {str(n): p for n, p in ((api_json("/providers/rules", timeout=timeout) or {}).get("providers") or {}).items()}


LEGACY_FILE_HINT = ("覆盖规则集还是 file 型，Verge 2.5.5 服务模式下改了不生效：重新生成安装"
                    "（python3 genconfig.py deadchain.toml --install --yes），手工配置的按 python3 route.py hook 换成新片段")


def _file_type_broken(providers: dict[str, dict]) -> bool:
    """file 型规则集只在 Verge 服务模式下失效（内核读的是副本）；sidecar / 旧版 Verge 仍然能用。"""
    from watch import active_controller

    ctl = active_controller()
    return ctl is not None and ctl.is_service and any((providers.get(n) or {}).get("vehicleType") == "File" for n in PROVIDER.values())


def hook_installed(timeout: float = 5.0) -> tuple[bool, str]:
    """Clash 里是否已经有这三个覆盖规则集，且改了能生效。"""
    from watch import clash_reachable

    if not clash_reachable():
        return False, "mihomo 没在跑"
    try:
        providers = _providers(timeout)
    except Exception:
        return False, "读不到 mihomo 的规则集列表"
    missing = [n for n in PROVIDER.values() if n not in providers]
    if missing:
        return False, "Clash 里还没有覆盖规则集：" + ", ".join(missing)
    if _file_type_broken(providers):
        return False, LEGACY_FILE_HINT
    return True, ""


def want_counts(routes: list[Route]) -> dict[str, int]:
    return {PROVIDER[t]: sum(1 for r in routes if r.target == t) for t in TARGETS}


def payload_digest(routes: list[Route]) -> str:
    return hashlib.sha256("".join(render_payload(routes, t) for t in TARGETS).encode("utf-8")).hexdigest()


def pushed_path() -> Path:
    """上次成功推给内核的内容指纹和内核报回的条数；resync 据此判断要不要重推。"""
    return routes_path().with_name(".routes-pushed.json")


def load_pushed() -> dict:
    try:
        return json.loads(pushed_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _same_content(directory: Path, port: int) -> bool:
    """占着端口的是不是也在提供同一份规则集（多半是悬浮窗和监控同时在推）。"""
    for t in TARGETS:
        name = f"{PROVIDER[t]}.yaml"
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/{name}", timeout=2) as r:
                if r.read() != (directory / name).read_bytes():
                    return False
        except Exception:
            return False
    return True


def push(directory: Path | None = None, want: dict[str, int] | None = None, digest: str = "") -> tuple[bool, str]:
    """让内核重读三个规则集（http 型先临时提供文件），再核对条数；成功后记下指纹，见 pushed_path。"""
    import urllib.parse

    from watch import api_json

    d = directory or rules_dir()
    port = SETTINGS.routes.serve_port
    try:
        before = _providers()
    except Exception:
        before = {}
    failed = []
    with serving(d, port) as mine:
        if not mine and not _same_content(d, port):
            return False, (f"端口 {port} 被别的程序占着，内核拉不到正确的规则集：换一个 [routes] serve_port，"
                           "再重新生成安装（Merge 里的 url 要跟着变）")
        for name in PROVIDER.values():
            try:
                api_json(f"/providers/rules/{urllib.parse.quote(name)}", method="PUT")
            except Exception as e:
                failed.append(f"{name}（{e}）")
    if failed:
        return False, "已写文件，但让 mihomo 重读失败：" + "; ".join(failed)
    if want is None:
        return True, "已生效"
    try:
        got = {n: p.get("ruleCount") for n, p in _providers().items() if n in want}
    except Exception:
        return True, "已生效"
    if digest:
        try:
            pushed_path().write_text(json.dumps({"digest": digest, "got": got}) + "\n", encoding="utf-8")
        except OSError:
            pass
    if got != want:
        if _file_type_broken(before):
            return False, LEGACY_FILE_HINT
        return False, f"生效条数对不上：Clash 里 {got}，应为 {want}"
    return True, "已生效：" + "  ".join(f"{TARGET_CN[t]} {want[PROVIDER[t]]} 条" for t in TARGETS)


def apply(routes: list[Route], directory: Path | None = None) -> tuple[bool, str]:
    """写规则集文件 + 让 mihomo 立刻重读。返回 (是否已生效, 给人看的一句话)。"""
    from watch import clash_reachable

    d = directory or rules_dir()
    write_providers(routes, d)
    if not clash_reachable():
        return False, "已记下，mihomo 没在跑，下次启动后读取"
    return push(d, want_counts(routes), payload_digest(routes))


def resync(routes: list[Route] | None = None) -> str:
    """内核里的覆盖规则和记忆对不上时重新推送。给 watch.py 每轮调用。

    该推：记忆在上次成功推送之后改过（例如 Clash 没开时改的），或内核报的条数和上次推完时不同（缓存丢了、内核换了目录）。
    不推：没有 routes.toml（不能拿「空」去覆盖已有规则）、没装钩子、file 型（推了也没用）、和上次推完时一致
    （内核认不了的规则会让条数永远少几条，按上次的结果比，不会每轮重推）。
    """
    if routes is None and not routes_path().exists():
        return ""
    try:
        providers = _providers(timeout=3.0)
    except Exception:
        return ""
    if any(n not in providers for n in PROVIDER.values()):
        return ""
    if any((providers[n].get("vehicleType") != "HTTP") for n in PROVIDER.values()):
        return ""
    rs = load_routes() if routes is None else routes
    want = want_counts(rs)
    got = {n: providers[n].get("ruleCount") for n in PROVIDER.values()}
    last = load_pushed()
    if last:
        if last.get("digest") == payload_digest(rs) and last.get("got") == got:
            return ""
    elif got == want:
        return ""
    ok, msg = apply(rs)
    return f"覆盖规则集与记忆不一致（内核 {got}，记忆 {want}），已重新推送：{msg}" if ok else f"覆盖规则集重推失败：{msg}"
