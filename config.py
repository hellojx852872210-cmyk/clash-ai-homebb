"""配置加载。

所有和机器/策略相关的值都在 config.toml 里（可选文件，缺省用内置默认），
代码本身不写死任何 IP、组名、路径。查找顺序：
  1. 环境变量 CLASH_AI_HOMEBB_CONFIG 指向的文件
  2. 项目目录下的 config.toml
"""
from __future__ import annotations

import glob
import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from verge import VERGE_DIR_DEFAULT

HERE = Path(__file__).resolve().parent
ENV_CONFIG = "CLASH_AI_HOMEBB_CONFIG"


@dataclass(frozen=True)
class ClashCfg:
    # mihomo 控制器：写了就先试它；auto_discover 时再按 Verge 各版本的位置自动找（见 verge.py），
    # 以 /version 有响应为准。一般留空即可。
    socket: str = ""
    controller: str = ""
    secret: str = ""
    auto_discover: bool = True
    verge_dir: str = VERGE_DIR_DEFAULT
    # 日常出口入站口（mixed-port）与家宽专用入站口（listeners 里指向家宽组的口）
    daily_proxy: str = "http://127.0.0.1:7897"
    homebb_proxy: str = "http://127.0.0.1:7901"


@dataclass(frozen=True)
class DeadchainCfg:
    # AI 规则指向的组：必须是 select 且只含 homebb_group（死链，没有 DIRECT/日常可回落）
    ai_group: str = "Claude-Only"
    # 家宽组：必须是 fallback，成员就是家宽入口节点
    homebb_group: str = "RESIP-Claude"
    homebb_group_type: str = "Fallback"
    # 家宽组成员名；为空表示成员来自订阅 provider（名字不固定），只检查组非空
    homebb_members: tuple[str, ...] = ("RESIP-A", "RESIP-B")
    # mihomo API 返回的节点 type（Vless / Trojan / Socks5 / Shadowsocks ...）；为空则不检查类型
    homebb_member_type: str = "Vless"
    # 悬浮窗：连接链路里出现这些名字就归为「家宽」
    homebb_markers: tuple[str, ...] = ("Claude-Only", "RESIP-Claude")


@dataclass(frozen=True)
class ProbeCfg:
    ip_url: str = "https://api.ipify.org"
    ai_probe_url: str = "https://api.openai.com/v1/models"
    # 连接表里用来找「AI 连接」的域名关键字（判断它走的链路含不含 ai_group）
    ai_hosts: tuple[str, ...] = ("openai.com", "chatgpt.com")
    # 物理上行网卡；direct_ips 必须经它直连（TUN route-exclude），为空则跳过该项检查
    uplink_interface: str = "en0"
    direct_ips: tuple[str, ...] = ()
    curl_timeout: int = 12


@dataclass(frozen=True)
class AlertCfg:
    title: str = "Clash 家宽监控"
    realert_secs: int = 1800
    # 这些状态常是几分钟内自愈的抖动（订阅节点抖、Verge 切换配置档时内核重载）：首次出现只观察一轮
    debounce_codes: tuple[str, ...] = ("daily_down", "core_reloading", "clash_dead", "deadchain_broken", "verge_service_failed")
    modal: bool = True
    sound: str = "Basso"
    # 可选 Telegram：两个环境变量都有值才发；不想要就留空
    telegram_token_env: str = "TG_TOKEN"
    telegram_chat_env: str = "TG_CHAT"


@dataclass(frozen=True)
class LockCfg:
    # 要上锁（uchg + sha256）的文件；支持 ~ 和 glob。为空表示不启用锁校验
    files: tuple[str, ...] = ()


@dataclass(frozen=True)
class RoutesCfg:
    # 出口覆盖规则：记忆文件（相对本项目目录）与规则集目录（mihomo 要求在内核数据目录之下）
    file: str = "routes.toml"
    dir: str = f"{VERGE_DIR_DEFAULT}/ai-homebb-rules"
    # 改出口时临时在 127.0.0.1 上提供规则集给内核拉取的端口（Merge 里 rule-providers 的 url 用它）
    serve_port: int = 7919


@dataclass(frozen=True)
class HudCfg:
    tick: float = 1.5
    # state.json 超过这么久没更新，悬浮窗显示「监控失联」
    stale_secs: int = 900
    ai_suffixes: tuple[str, ...] = (
        "anthropic.com", "claude.ai", "claude.com", "claudeusercontent.com",
        "openai.com", "chatgpt.com", "oaistatic.com", "oaiusercontent.com",
        "arkoselabs.com", "hcaptcha.com", "x.ai", "grok.com", "perplexity.ai",
        "ai.google.dev", "generativelanguage.googleapis.com",
    )
    noise_suffixes: tuple[str, ...] = (
        "googleapis.com", "google.com", "gstatic.com", "gvt1.com", "gvt2.com",
        "doubleclick.net", "googlesyndication.com", "googleadservices.com",
        "apple.com", "icloud.com", "mzstatic.com", "crashlytics.com",
        "app-measurement.com", "auth0.com", "googleusercontent.com",
    )
    terminal_names: tuple[str, ...] = (
        "Terminal", "iTerm2", "iTerm", "Ghostty", "kitty", "Alacritty", "Warp",
        "Tabby", "cmux", "Cmux", "Termius", "终端",
    )
    terminal_path_hints: tuple[str, ...] = (
        "terminal.app", "iterm", "ghostty", "kitty.app", "alacritty", "warp.app", "cmux.app",
    )


@dataclass(frozen=True)
class Settings:
    clash: ClashCfg = field(default_factory=ClashCfg)
    deadchain: DeadchainCfg = field(default_factory=DeadchainCfg)
    probe: ProbeCfg = field(default_factory=ProbeCfg)
    alert: AlertCfg = field(default_factory=AlertCfg)
    lock: LockCfg = field(default_factory=LockCfg)
    routes: RoutesCfg = field(default_factory=RoutesCfg)
    hud: HudCfg = field(default_factory=HudCfg)
    config_path: str = ""


def _build(cls, data: Any):
    """只取 dataclass 认识的键；list → tuple；未知键忽略（不因多写一项而崩）。"""
    if not isinstance(data, dict):
        return cls()
    kwargs = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        v = data[f.name]
        if isinstance(v, list):
            v = tuple(v)
        kwargs[f.name] = v
    return cls(**kwargs)


def expand_path(p: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(p)))


def expand_files(patterns: tuple[str, ...]) -> tuple[Path, ...]:
    """把 lock.files 展开成具体文件列表（~、环境变量、glob）。"""
    out: list[Path] = []
    for pat in patterns:
        s = str(expand_path(pat))
        if any(ch in s for ch in "*?["):
            out.extend(Path(x) for x in sorted(glob.glob(s)))
        else:
            out.append(Path(s))
    return tuple(out)


def find_config_path(explicit: str | Path | None = None) -> Path | None:
    if explicit:
        return Path(explicit)
    env = os.environ.get(ENV_CONFIG)
    if env:
        return Path(env)
    p = HERE / "config.toml"
    return p if p.exists() else None


def load_settings(path: str | Path | None = None) -> Settings:
    p = find_config_path(path)
    data: dict[str, Any] = {}
    if p is not None and p.exists():
        with p.open("rb") as f:
            data = tomllib.load(f)
    return Settings(
        clash=_build(ClashCfg, data.get("clash")),
        deadchain=_build(DeadchainCfg, data.get("deadchain")),
        probe=_build(ProbeCfg, data.get("probe")),
        alert=_build(AlertCfg, data.get("alert")),
        lock=_build(LockCfg, data.get("lock")),
        routes=_build(RoutesCfg, data.get("routes")),
        hud=_build(HudCfg, data.get("hud")),
        config_path=str(p) if p else "",
    )


SETTINGS = load_settings()
