"""把 Clash 连接表收成前台 App 的出口摘要（家宽 / 日常 / 直连 / 混合）。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from config import SETTINGS

HOMEBB_MARKERS = frozenset(SETTINGS.deadchain.homebb_markers)
TERMINAL_NAMES = frozenset(SETTINGS.hud.terminal_names)
TERMINAL_PATH_HINTS = tuple(h.lower() for h in SETTINGS.hud.terminal_path_hints)
AI_SUFFIXES = tuple(SETTINGS.hud.ai_suffixes)
NOISE_SUFFIXES = tuple(SETTINGS.hud.noise_suffixes)
KIND_CN = {
    "homebb": "家宽",
    "daily": "日常",
    "direct": "直连",
    "mixed": "混合",
    "idle": "无连接",
    "unknown": "未知",
    "other": "其它",
}
GENERIC_PROCS = frozenset({
    "node", "nodejs", "python", "python3", "Python", "ruby", "perl", "java",
    "deno", "bun", "php", "bash", "zsh", "sh", "dash",
})


@dataclass(frozen=True)
class Summary:
    kind: str = "idle"
    kinds: tuple[str, ...] = ()
    hosts: tuple[str, ...] = ()
    chains: tuple[str, ...] = ()
    host_kinds: tuple[tuple[str, str], ...] = ()  # (host, kind)，混合时逐条标注用


def classify(chains, markers=HOMEBB_MARKERS) -> str:
    """链路里出现家宽标记 → homebb；全 DIRECT → direct；其它非空 → daily。"""
    names = [str(x) for x in chains if x]
    if not names:
        return "unknown"
    if any(n in markers for n in names):
        return "homebb"
    if all(n == "DIRECT" for n in names):
        return "direct"
    return "daily"


def _host_endswith(host: str, suffix: str) -> bool:
    h = host.lower().rstrip(".")
    s = suffix.lower()
    return h == s or h.endswith("." + s)


def is_ai_host(host: str) -> bool:
    host = (host or "").strip()
    if not host:
        return False
    return any(_host_endswith(host, s) for s in AI_SUFFIXES)


def is_noise_host(host: str) -> bool:
    host = (host or "").strip()
    if not host:
        return False
    if is_ai_host(host):
        return False
    return any(_host_endswith(host, s) for s in NOISE_SUFFIXES)


def is_terminal_app(app_name: str, app_path: str = "") -> bool:
    name = (app_name or "").strip()
    if name in TERMINAL_NAMES:
        return True
    path = (app_path or "").lower()
    return any(t in path for t in TERMINAL_PATH_HINTS)


def _norm_path(p: str) -> str:
    if not p:
        return ""
    try:
        return str(Path(p).expanduser().resolve())
    except Exception:
        return p.rstrip("/")


def _matches(meta: dict, app_path: str, app_name: str, extra_names, extra_paths=()) -> bool:
    path = str(meta.get("processPath") or "")
    proc = str(meta.get("process") or "")
    ap = (app_path or "").rstrip("/")
    if ap and (path == ap or path.startswith(ap + "/")):
        return True
    name = (app_name or "").strip()
    if name:
        if proc == name:
            return True
        if proc.startswith(name + " Helper"):
            return True
    path_n = _norm_path(path)
    extras_p = {_norm_path(x) for x in extra_paths if x}
    if path_n and path_n in extras_p:
        return True
    extras = {x for x in extra_names if x}
    if proc in extras and proc not in GENERIC_PROCS:
        return True
    base = Path(path).name if path else ""
    if base and base in extras and base not in GENERIC_PROCS:
        return True
    return False


def match_conns(conns, app_path: str, app_name: str, extra_names=(), extra_paths=()):
    out = []
    for c in conns or []:
        meta = c.get("metadata") or {}
        if _matches(meta, app_path, app_name, extra_names, extra_paths):
            out.append(c)
    return out


def summarize(conns) -> Summary:
    if not conns:
        return Summary(kind="idle")
    sig_kinds: set[str] = set()
    fallback_kinds: set[str] = set()
    hosts: list[str] = []
    host_kinds: list[tuple[str, str]] = []
    seen_host: set[str] = set()
    chain_labels: list[str] = []
    seen_chain: set[str] = set()
    has_direct = False
    for c in conns:
        meta = c.get("metadata") or {}
        chains = tuple(c.get("chains") or [])
        kind = classify(chains)
        host = (meta.get("host") or meta.get("sniffHost") or "").strip()
        if kind == "direct":
            has_direct = True
        else:
            fallback_kinds.add(kind)
            if not is_noise_host(host):
                sig_kinds.add(kind)
                if host and host not in seen_host:
                    seen_host.add(host)
                    hosts.append(host)
                    host_kinds.append((host, kind))
        label = "/".join(chains) if chains else ""
        if label and label not in seen_chain:
            seen_chain.add(label)
            chain_labels.append(label)
    use = sig_kinds or fallback_kinds
    if not use:
        kind = "direct" if has_direct else "idle"
        kinds_t: tuple[str, ...] = ("direct",) if has_direct else ()
    else:
        kinds_t = tuple(sorted(use))
        kind = kinds_t[0] if len(kinds_t) == 1 else "mixed"
    return Summary(
        kind=kind,
        kinds=kinds_t,
        hosts=tuple(hosts),
        chains=tuple(chain_labels),
        host_kinds=tuple(host_kinds),
    )


def format_hud(app_name: str, summary: Summary, limit: int = 3, app_path: str = "") -> tuple[str, str]:
    title = app_name or "未知 App"
    if summary.kind == "idle":
        if is_terminal_app(title, app_path):
            return (f"{title}\n暂无出站", "idle")
        return (f"{title}\n无连接", "idle")
    label = KIND_CN.get(summary.kind, summary.kind)
    if summary.kind == "mixed":
        # 混合：逐条标注 host 走的是家宽还是日常，家宽优先显示，每类最多 per_kind_limit 条
        parts = [KIND_CN.get(k, k) for k in summary.kinds]
        label = "混合 " + "+".join(parts)
        lines = [title, label]
        per_kind: dict[str, list[str]] = {}
        for host, kind in summary.host_kinds:
            per_kind.setdefault(kind, []).append(host)
        order = ["homebb", "daily"] + [k for k in summary.kinds if k not in ("homebb", "daily")]
        per_kind_limit = max(1, (limit + 1) // 2)
        for kind in order:
            for host in per_kind.get(kind, [])[:per_kind_limit]:
                lines.append(f"{KIND_CN.get(kind, kind)} · {host}")
        return ("\n".join(lines), "mixed")
    lines = [title, label]
    for host in summary.hosts[:limit]:
        lines.append(host)
    return ("\n".join(lines), summary.kind)
