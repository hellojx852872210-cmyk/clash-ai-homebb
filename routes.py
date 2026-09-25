"""出口覆盖规则：记住「哪个 App / 域名走家宽还是直连还是代理」，并渲染成 mihomo 规则集。

记忆存在项目目录的 routes.toml；渲染出三个 classical 文件型 rule-provider：
  <rules_dir>/user-homebb.yaml  → AI 组（家宽）
  <rules_dir>/user-direct.yaml  → DIRECT
  <rules_dir>/user-daily.yaml   → 日常出口
mihomo 会监听这三个文件，改完即生效，不用重载整份配置、不用解锁 Merge.yaml。

三条 RULE-SET 规则排在 AI 死链规则之后，所以覆盖规则永远改不动 AI 的出口（实测：
把 claude.ai 写进 user-direct 仍然走家宽）。因此 validate() 直接拒绝这类无效规则。
"""
from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from datetime import date
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
def hook_installed(timeout: float = 5.0) -> tuple[bool, str]:
    """Clash 里是否已经有这三个覆盖规则集。"""
    from watch import api_json, clash_reachable

    if not clash_reachable():
        return False, "mihomo 没在跑"
    try:
        names = {str(n) for n in ((api_json("/providers/rules", timeout=timeout) or {}).get("providers") or {})}
    except Exception:
        return False, "读不到 mihomo 的规则集列表"
    missing = [n for n in PROVIDER.values() if n not in names]
    if missing:
        return False, "Clash 里还没有覆盖规则集：" + ", ".join(missing)
    return True, ""


def apply(routes: list[Route], directory: Path | None = None) -> tuple[bool, str]:
    """写规则集文件 + 让 mihomo 立刻重读。返回 (是否已生效, 给人看的一句话)。

    mihomo 也会自己监听文件（几秒内），PUT 只是让它立刻发生，这样函数一返回新连接就按新出口走。
    """
    import urllib.parse

    from watch import api_json, clash_reachable

    write_providers(routes, directory)
    if not clash_reachable():
        return False, "已记下，mihomo 没在跑，下次启动后读取"
    failed = []
    for name in PROVIDER.values():
        try:
            api_json(f"/providers/rules/{urllib.parse.quote(name)}", method="PUT")
        except Exception as e:
            failed.append(f"{name}（{e}）")
    if failed:
        return False, "已写文件，但让 mihomo 重读失败：" + "; ".join(failed)
    want = {PROVIDER[t]: sum(1 for r in routes if r.target == t) for t in TARGETS}
    try:
        got = {n: p.get("ruleCount") for n, p in ((api_json("/providers/rules") or {}).get("providers") or {}).items() if n in want}
    except Exception:
        return True, "已生效"
    if got != want:
        return False, f"生效条数对不上：Clash 里 {got}，应为 {want}"
    return True, "已生效：" + "  ".join(f"{TARGET_CN[t]} {want[PROVIDER[t]]} 条" for t in TARGETS)
