#!/usr/bin/env python3
"""从 deadchain.toml 生成 Clash Verge 的 Merge.yaml / Script.js / providers / config.toml。

只写到 --out 目录；--install --yes 才复制进 Verge 数据目录（原文件先备份）。
不依赖第三方库：YAML 用 JSON 流式语法写（JSON 是 YAML 的子集）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
VERGE_DIR_DEFAULT = "~/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev"
PROVIDERS_SUBDIR = "ai-homebb-providers"
EXCLUDE_INFO_NODES = "到期|流量|重置|获取|剩余|套餐|官网|Traffic|Expire|Reset"
CHECK_URL = "http://www.gstatic.com/generate_204"

DEFAULT_AI_DOMAINS = [
    "anthropic.com", "claude.ai", "claude.com", "claudeusercontent.com",
    "statsigapi.net", "statsig.com", "featuregates.org",
    "openai.com", "chatgpt.com", "oaistatic.com", "oaiusercontent.com",
    "arkoselabs.com", "hcaptcha.com",
    "gemini.google.com", "aistudio.google.com", "notebooklm.google.com",
    "ai.google.dev", "generativelanguage.googleapis.com",
    "x.ai", "grok.com", "perplexity.ai",
]
DEFAULT_AI_IPCIDR = ["160.79.104.0/21"]  # Anthropic
STUN_BROWSERS = [
    "Google Chrome", "Google Chrome Canary", "Chromium", "Microsoft Edge", "Safari",
    "Cursor", "ChatGPT", "Brave Browser", "Firefox", "Arc", "Dia",
]
STUN_PORTS = (19302, 19305, 3478)
# mihomo 配置里的 type → 控制器 API 里的 type
API_TYPE = {
    "vless": "Vless", "vmess": "Vmess", "trojan": "Trojan", "ss": "Shadowsocks", "ssr": "ShadowsocksR",
    "socks5": "Socks5", "http": "Http", "hysteria": "Hysteria", "hysteria2": "Hysteria2",
    "tuic": "Tuic", "wireguard": "WireGuard", "anytls": "AnyTLS", "ssh": "Ssh", "mieru": "Mieru",
}


def j(x) -> str:
    """JSON 即 YAML 流式写法，省掉 PyYAML 依赖。"""
    return json.dumps(x, ensure_ascii=False)


def expand(p: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(p)))


class SpecError(ValueError):
    pass


def load_spec(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def build(spec: dict) -> tuple[dict[str, str], list[tuple[Path, str]]]:
    """返回 (要写的文本文件 {相对路径: 内容}, 要复制的文件 [(源, 相对目标)])。"""
    names = spec.get("names") or {}
    AI = names.get("ai_group", "Claude-Only")
    HB = names.get("homebb_group", "RESIP-Claude")
    DAILY = names.get("daily_group", "日常出口")
    DAILY_AUTO = names.get("daily_auto_group", "日常-自动")

    hb = spec.get("homebb") or {}
    iface = hb.get("interface", "")
    port = int(hb.get("listener_port", 7901))
    nodes = list(hb.get("nodes") or [])
    hb_sub = hb.get("subscription") or {}
    if not nodes and not hb_sub:
        raise SpecError("[homebb] 至少要有 nodes 或 subscription")
    for n in nodes:
        if not n.get("name") or not n.get("type") or not n.get("server"):
            raise SpecError(f"家宽节点缺 name/type/server: {n}")
        if iface and "interface-name" not in n:
            n["interface-name"] = iface
    hb_types = {API_TYPE.get(str(n.get("type")).lower(), "") for n in nodes}
    hb_member_type = hb_types.pop() if (nodes and not hb_sub and len(hb_types) == 1) else ""

    daily = spec.get("daily") or {}
    subs = list(daily.get("subscriptions") or [])
    if not subs:
        raise SpecError("[daily] 至少要有一条 subscriptions")
    copies: list[tuple[Path, str]] = []
    providers: dict[str, dict] = {}

    def provider(name: str, src: dict, prefix: str) -> dict:
        p: dict = {
            "path": f"./{PROVIDERS_SUBDIR}/{name}.yaml",
            "exclude-filter": EXCLUDE_INFO_NODES,
            "override": {"additional-prefix": prefix},
            "health-check": {"enable": True, "url": CHECK_URL, "interval": 300},
        }
        if src.get("url"):
            p.update({"type": "http", "url": src["url"], "interval": 86400})
        elif src.get("file"):
            f = expand(src["file"])
            if not f.exists():
                raise SpecError(f"订阅文件不存在: {f}")
            copies.append((f, f"{PROVIDERS_SUBDIR}/{name}.yaml"))
            p["type"] = "file"
        else:
            raise SpecError(f"订阅 {name} 要有 url 或 file")
        return p

    sub_names: list[str] = []
    for s in subs:
        n = str(s.get("name") or "").strip()
        if not n:
            raise SpecError("每条日常订阅都要有 name")
        providers[f"sub-{n}"] = provider(n, s, f"[{n}] ")
        sub_names.append(n)
    if hb_sub:
        providers["homebb-sub"] = provider("homebb", hb_sub, "[home] ")

    ai = spec.get("ai") or {}
    domains = list(ai.get("domains") or DEFAULT_AI_DOMAINS)
    procs = list(ai.get("processes") or [])
    proc_re = list(ai.get("process_path_regex") or [])
    direct_ips = [str(x) for x in ((spec.get("direct") or {}).get("ips") or [])]
    dns = spec.get("dns") or {}
    bootstrap = list(dns.get("bootstrap") or ["223.5.5.5", "8.8.8.8"])

    # ---------- groups ----------
    groups: list[dict] = []
    for n in sub_names:
        groups.append({"name": f"{n}-自动", "type": "url-test", "use": [f"sub-{n}"], "url": CHECK_URL,
                       "interval": 300, "tolerance": 100, "lazy": False})
    groups.append({"name": DAILY_AUTO, "type": "fallback", "proxies": [f"{n}-自动" for n in sub_names],
                   "url": CHECK_URL, "interval": 120, "lazy": False})
    groups.append({"name": DAILY, "type": "select", "proxies": [DAILY_AUTO] + [f"{n}-自动" for n in sub_names]})
    hb_group: dict = {"name": HB, "type": "fallback", "url": CHECK_URL, "interval": 120, "lazy": False}
    if nodes:
        hb_group["proxies"] = [n["name"] for n in nodes]
    if hb_sub:
        hb_group["use"] = ["homebb-sub"]
    groups.append(hb_group)
    groups.append({"name": AI, "type": "select", "proxies": [HB]})

    # ---------- rules ----------
    rules: list[str] = [f"IN-NAME,homebb-in,{AI}", f"IN-PORT,{port},{AI}"]
    if ai.get("reject_quic", True):
        rules += [f"AND,((DOMAIN-SUFFIX,{d}),(NETWORK,udp),(DST-PORT,443)),REJECT" for d in domains]
    if ai.get("reject_stun_browsers", True):
        rx = "|".join(STUN_BROWSERS)
        for sp in STUN_PORTS:
            rules.append(f"AND,((PROCESS-PATH-REGEX,.*/({rx}).app/),(NETWORK,udp),(DST-PORT,{sp})),REJECT")
            rules.append(f"AND,((PROCESS-NAME,com.apple.WebKit.Networking),(NETWORK,udp),(DST-PORT,{sp})),REJECT")
    rules += [f"PROCESS-NAME,{p},{AI}" for p in procs]
    rules += [f"PROCESS-PATH-REGEX,{r},{AI}" for r in proc_re]
    rules += [f"DOMAIN-SUFFIX,{d},{AI}" for d in domains]
    rules += [f"IP-CIDR,{c},{AI},no-resolve" for c in DEFAULT_AI_IPCIDR]
    # 家宽入口域名/IP 直连，避免被 TUN 塞回代理
    for n in nodes:
        srv = str(n["server"])
        rules.append(f"IP-CIDR,{srv}/32,DIRECT,no-resolve" if srv.replace(".", "").isdigit() else f"DOMAIN,{srv},DIRECT")
    for ip in direct_ips:
        rules.append(f"IP-CIDR,{ip}/32,DIRECT,no-resolve")
    if direct_ips:
        rules += ["PROCESS-NAME,ssh,DIRECT", "PROCESS-NAME,scp,DIRECT", "PROCESS-NAME,rsync,DIRECT"]

    # ---------- Merge.yaml ----------
    L: list[str] = []
    L.append(f"# 由 clash-ai-homebb genconfig.py 生成于 {time.strftime('%Y-%m-%d %H:%M')}。改 deadchain.toml 重新生成，不要手改。")
    L.append("# 语义：AI 域名/进程/家宽入站口 → AI 组 → 家宽组（只有家宽节点，没有 DIRECT，挂了就断线）；")
    L.append("#       其它流量 → 日常出口（各订阅 url-test + fallback，能通哪条走哪条）。")
    L.append("find-process-mode: always")
    L.append("ipv6: false")
    if direct_ips:
        L.append("tun:")
        L.append("  strict-route: false")
        L.append("  route-exclude-address: " + j([f"{ip}/32" for ip in direct_ips]))
    L.append("proxy-providers:")
    for k, v in providers.items():
        L.append(f"  {j(k)}: {j(v)}")
    L.append("prepend-proxies:")
    for n in nodes:
        L.append("- " + j(n))
    L.append("prepend-proxy-groups:")
    for g in groups:
        L.append("- " + j(g))
    L.append("prepend-rules:")
    for r in rules:
        L.append("- " + j(r))
    if dns.get("via_homebb", True):
        doh = [f"https://1.1.1.1/dns-query#{AI}", f"https://dns.google/dns-query#{AI}"]
        L.append("dns:")
        L.append("  nameserver: " + j(doh))
        L.append("  default-nameserver: " + j(bootstrap))
        L.append("  proxy-server-nameserver: " + j(bootstrap))
        L.append("  fallback: " + j(doh))
        L.append("  nameserver-policy:")
        L.append("    " + j(",".join(f"+.{d}" for d in domains)) + ": " + j(doh))
    L.append("listeners:")
    L.append("- " + j({"name": "homebb-in", "type": "mixed", "port": port, "listen": "127.0.0.1", "proxy": AI}))
    merge = "\n".join(L) + "\n"

    # ---------- Script.js ----------
    tpl = (HERE / "templates" / "Script.js.tpl").read_text(encoding="utf-8")
    script = (
        tpl.replace("__NAMES__", j({"ai": AI, "homebb": HB, "daily": DAILY}))
        .replace("__PROVIDERS__", j(providers))
        .replace("__PROXIES__", j(nodes))
        .replace("__GROUPS__", j(groups))
    )

    # ---------- watch 的 config.toml ----------
    verge = VERGE_DIR_DEFAULT
    cfg = "\n".join([
        "# 由 genconfig.py 生成，给 watch.py / float.py 用",
        "[clash]",
        f'homebb_proxy = "http://127.0.0.1:{port}"',
        "",
        "[deadchain]",
        f"ai_group = {j(AI)}",
        f"homebb_group = {j(HB)}",
        'homebb_group_type = "Fallback"',
        f"homebb_members = {j([n['name'] for n in nodes] if (nodes and not hb_sub) else [])}",
        f"homebb_member_type = {j(hb_member_type)}",
        f"homebb_markers = {j([AI, HB])}",
        "",
        "[probe]",
        f"direct_ips = {j(direct_ips)}",
        f"uplink_interface = {j(iface or 'en0')}",
        "",
        "[lock]",
        "files = " + j([f"{verge}/profiles/Merge.yaml", f"{verge}/profiles/Script.js", f"{verge}/{PROVIDERS_SUBDIR}/*.yaml"]),
        "",
    ])

    install_md = f"""# 安装步骤

1. 把 `Merge.yaml` 内容粘到 Clash Verge Rev「配置 → 全局扩展配置 → Merge」，`Script.js` 粘到「Script」；
   或直接 `python3 genconfig.py deadchain.toml --install --yes` 让脚本复制（会备份原文件）。
2. 把 `{PROVIDERS_SUBDIR}/` 整个目录放到 Verge 数据目录：`{verge}/{PROVIDERS_SUBDIR}/`
   （mihomo 只允许 provider 文件在内核目录之下）。url 型订阅首次由 mihomo 自行下载到这里。
3. 在 Verge 里重新激活当前配置档（切换一下即可），Proxies 组选「{DAILY}」。
4. 把 `config.toml` 放到本项目目录，然后 `python3 watch.py --pin` 上锁，`./install.sh` 装监控和悬浮窗。
5. 验证：`curl -x http://127.0.0.1:{port} https://api.ipify.org` 应返回家宽 IP；`python3 watch.py` 应打印「正常」。
"""
    return (
        {"Merge.yaml": merge, "Script.js": script, "config.toml": cfg, "INSTALL.md": install_md},
        copies,
    )


def write_out(out: Path, files: dict[str, str], copies: list[tuple[Path, str]]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / PROVIDERS_SUBDIR).mkdir(exist_ok=True)
    for rel, text in files.items():
        (out / rel).write_text(text, encoding="utf-8")
    for src, rel in copies:
        shutil.copy2(src, out / rel)


def install(out: Path, verge: Path, project: Path) -> None:
    ts = time.strftime("%Y%m%d%H%M%S")
    prof = verge / "profiles"
    if not prof.is_dir():
        raise SpecError(f"Verge 目录不对：{prof} 不存在")
    import stat as _stat
    for name in ("Merge.yaml", "Script.js"):
        dst = prof / name
        if dst.exists() and getattr(dst.stat(), "st_flags", 0) & getattr(_stat, "UF_IMMUTABLE", 0):
            raise SpecError(f"{dst} 已被 uchg 锁住，先 python3 watch.py --unpin 再安装，装完 --pin")
    for name in ("Merge.yaml", "Script.js"):
        dst = prof / name
        if dst.exists():
            shutil.copy2(dst, dst.with_name(f"{name}.bak-{ts}"))
        shutil.copy2(out / name, dst)
        print(f"已写 {dst}")
    pdir = verge / PROVIDERS_SUBDIR
    pdir.mkdir(exist_ok=True)
    for f in (out / PROVIDERS_SUBDIR).glob("*.yaml"):
        shutil.copy2(f, pdir / f.name)
        print(f"已写 {pdir / f.name}")
    cfg = project / "config.toml"
    if cfg.exists():
        shutil.copy2(cfg, cfg.with_name(f"config.toml.bak-{ts}"))
    shutil.copy2(out / "config.toml", cfg)
    print(f"已写 {cfg}")
    print("下一步：在 Verge 里重新激活配置档，Proxies 选日常出口，然后 python3 watch.py --pin")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成 clash-ai-homebb 的 Merge/Script/providers/config")
    ap.add_argument("spec", help="deadchain.toml")
    ap.add_argument("--out", default="generated", help="输出目录（默认 ./generated）")
    ap.add_argument("--install", action="store_true", help="生成后复制进 Verge 数据目录与本项目（需 --yes）")
    ap.add_argument("--yes", action="store_true", help="确认 --install 会覆盖 Verge 里的 Merge/Script（有备份）")
    ap.add_argument("--verge-dir", default=VERGE_DIR_DEFAULT, help="Clash Verge Rev 数据目录")
    a = ap.parse_args(argv)
    try:
        spec = load_spec(Path(a.spec))
        files, copies = build(spec)
    except (SpecError, tomllib.TOMLDecodeError, OSError) as e:
        print(f"生成失败：{e}", file=sys.stderr)
        return 2
    out = Path(a.out)
    write_out(out, files, copies)
    for rel in list(files) + [c[1] for c in copies]:
        print(f"生成 {out / rel}")
    if a.install:
        if not a.yes:
            print("--install 需要加 --yes 确认（会覆盖 Verge 的 Merge.yaml / Script.js，原文件备份）", file=sys.stderr)
            return 3
        try:
            install(out, expand(a.verge_dir), HERE)
        except SpecError as e:
            print(f"安装失败：{e}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
