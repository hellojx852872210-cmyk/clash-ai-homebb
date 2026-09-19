#!/usr/bin/env python3
"""交互式引导：不用手写 TOML，一问一答添加家宽节点 / 家宽订阅 / 日常订阅，随时增删，生成并（确认后）安装。

用法：
  python3 wizard.py                                   # 菜单模式
  python3 wizard.py add-home  'socks5://u:p@203.0.113.5:1080#家宽A'
  python3 wizard.py add-home  '203.0.113.5:1080:user:pass'      # host:port:user:pass 简写，默认 socks5（--http 则 http）
  python3 wizard.py add-home  'vless://uuid@host:443?security=reality&pbk=...&sid=...&sni=...&fp=chrome&flow=xtls-rprx-vision#家宽B'
  python3 wizard.py add-home-sub  https://home.example/sub?type=clash   # 或本地 .yaml 路径
  python3 wizard.py add-daily 机场A https://a.example/sub?target=clash
  python3 wizard.py add-daily 机场B ~/Downloads/b.yaml
  python3 wizard.py remove-home 家宽A   |   remove-daily 机场A   |   remove-home-sub
  python3 wizard.py list
  python3 wizard.py generate [--install --yes]

数据文件：deadchain.toml（可用环境变量 CLASH_AI_HOMEBB_SPEC 指到别处）。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

HERE = Path(__file__).resolve().parent
SPEC_PATH = Path(os.environ.get("CLASH_AI_HOMEBB_SPEC") or HERE / "deadchain.toml")

DEFAULT_SPEC: dict[str, Any] = {
    "names": {"ai_group": "Claude-Only", "homebb_group": "RESIP-Claude", "daily_group": "日常出口"},
    "homebb": {"interface": "en0", "listener_port": 7901, "nodes": []},
    "daily": {"subscriptions": []},
    "ai": {
        "processes": ["Claude", "Claude Helper", "Claude Helper (GPU)", "Claude Helper (Renderer)", "claude", "codex"],
        "process_path_regex": [".*/\\.local/share/claude/versions/", ".*/npm-global/.*(codex|@openai/codex)"],
        "reject_quic": True,
        "reject_stun_browsers": True,
    },
    "dns": {"via_homebb": True, "bootstrap": ["223.5.5.5", "8.8.8.8"]},
    "direct": {"ips": []},
}


# ---------------- TOML 写出（tomllib 只读，自己写一个够用的） ----------------
def _scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)  # JSON 字符串是合法的 TOML basic string
    if isinstance(v, list):
        return "[" + ", ".join(_scalar(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_scalar(x)}" for k, x in v.items()) + " }"
    raise TypeError(f"不支持的 TOML 值: {v!r}")


def _key(k: str) -> str:
    return k if all(c.isalnum() or c in "-_" for c in k) else json.dumps(k, ensure_ascii=False)


def _is_table_list(v: Any) -> bool:
    return isinstance(v, list) and bool(v) and all(isinstance(x, dict) for x in v)


def _emit_table(lines: list[str], path: str, table: dict, array_item: bool = False) -> None:
    lines.append(f"[[{path}]]" if array_item else f"[{path}]")
    subtables: list[tuple[str, Any]] = []
    for k, v in table.items():
        if isinstance(v, dict) and any(isinstance(x, (dict, list)) for x in v.values()) or _is_table_list(v):
            subtables.append((k, v))
        else:
            lines.append(f"{_key(k)} = {_scalar(v)}")
    lines.append("")
    for k, v in subtables:
        sub = f"{path}.{_key(k)}"
        if isinstance(v, dict):
            _emit_table(lines, sub, v)
        else:
            for item in v:
                _emit_table(lines, sub, item, array_item=True)


def dump_toml(spec: dict) -> str:
    lines: list[str] = ["# clash-ai-homebb 配置（由 wizard.py 维护；也可以手改后 python3 genconfig.py deadchain.toml）", ""]
    for k, v in spec.items():
        if isinstance(v, dict):
            _emit_table(lines, _key(k), v)
        else:
            lines.append(f"{_key(k)} = {_scalar(v)}")
    return "\n".join(lines).rstrip("\n") + "\n"


# ---------------- 节点解析：常见分享格式 → mihomo 节点 ----------------
def _b64(s: str) -> str:
    s = s.strip().replace("-", "+").replace("_", "/")
    return base64.b64decode(s + "=" * (-len(s) % 4)).decode("utf-8", "replace")


def _q(qs: dict[str, list[str]], *keys: str, default: str = "") -> str:
    for k in keys:
        if qs.get(k):
            return qs[k][0]
    return default


def _name_of(frag: str, fallback: str) -> str:
    return unquote(frag).strip() or fallback


def parse_node(text: str, *, default_name: str = "home", prefer_http: bool = False) -> dict:
    """把一行节点描述转成 mihomo proxy 字典。支持：
    JSON 对象 / socks5:// / http(s):// / ss:// / trojan:// / vless:// / vmess:// / hysteria2:// / host:port[:user:pass]
    """
    t = text.strip()
    if not t:
        raise ValueError("空输入")
    if t.startswith("{"):
        d = json.loads(t)
        if not d.get("type") or not d.get("server"):
            raise ValueError("JSON 节点至少要有 type 和 server")
        d.setdefault("name", default_name)
        return d
    if "://" not in t:
        parts = t.split(":")
        if len(parts) not in (2, 4):
            raise ValueError("简写格式应为 host:port 或 host:port:user:pass")
        d = {"name": default_name, "type": "http" if prefer_http else "socks5", "server": parts[0], "port": int(parts[1]), "udp": False}
        if len(parts) == 4:
            d["username"], d["password"] = parts[2], parts[3]
        return d
    u = urlsplit(t)
    scheme = u.scheme.lower()
    qs = parse_qs(u.query)
    name = _name_of(u.fragment, default_name)
    if scheme in ("socks5", "socks5h", "socks", "http", "https"):
        if not u.hostname or not u.port:
            raise ValueError("缺 host 或 port")
        d = {"name": name, "type": "http" if scheme.startswith("http") else "socks5", "server": u.hostname, "port": u.port, "udp": False}
        if u.username:
            d["username"] = unquote(u.username)
        if u.password:
            d["password"] = unquote(u.password)
        if scheme == "https":
            d["tls"] = True
        return d
    if scheme == "ss":
        body = t[len("ss://"):].split("#", 1)[0]
        if "@" not in body:  # 老式整体 base64
            body = _b64(body)
        userinfo, _, hostport = body.rpartition("@")
        hostport = hostport.split("/", 1)[0].split("?", 1)[0]
        if ":" not in userinfo:
            userinfo = _b64(userinfo)
        method, _, password = userinfo.partition(":")
        host, _, port = hostport.rpartition(":")
        return {"name": name, "type": "ss", "server": host.strip("[]"), "port": int(port), "cipher": method, "password": unquote(password), "udp": False}
    if scheme == "trojan":
        d = {"name": name, "type": "trojan", "server": u.hostname, "port": u.port, "password": unquote(u.username or ""), "udp": False}
        sni = _q(qs, "sni", "peer")
        if sni:
            d["sni"] = sni
        if _q(qs, "allowInsecure", "insecure") in ("1", "true"):
            d["skip-cert-verify"] = True
        net = _q(qs, "type", default="tcp")
        if net == "ws":
            d["network"] = "ws"
            d["ws-opts"] = {"path": _q(qs, "path", default="/"), "headers": {"Host": _q(qs, "host", default=sni or u.hostname)}}
        elif net == "grpc":
            d["network"] = "grpc"
            d["grpc-opts"] = {"grpc-service-name": _q(qs, "serviceName")}
        return d
    if scheme == "vless":
        d = {"name": name, "type": "vless", "server": u.hostname, "port": u.port, "uuid": unquote(u.username or ""), "udp": False}
        sec = _q(qs, "security")
        if sec in ("tls", "reality"):
            d["tls"] = True
            if _q(qs, "sni"):
                d["servername"] = _q(qs, "sni")
            if _q(qs, "fp"):
                d["client-fingerprint"] = _q(qs, "fp")
        if sec == "reality":
            d["reality-opts"] = {"public-key": _q(qs, "pbk"), "short-id": _q(qs, "sid")}
        if _q(qs, "flow"):
            d["flow"] = _q(qs, "flow")
        net = _q(qs, "type", default="tcp")
        d["network"] = net
        if net == "ws":
            d["ws-opts"] = {"path": _q(qs, "path", default="/"), "headers": {"Host": _q(qs, "host", default=u.hostname)}}
        elif net == "grpc":
            d["grpc-opts"] = {"grpc-service-name": _q(qs, "serviceName")}
        return d
    if scheme == "vmess":
        j = json.loads(_b64(t[len("vmess://"):]))
        d = {"name": _name_of(str(j.get("ps") or ""), name), "type": "vmess", "server": j.get("add"), "port": int(j.get("port")),
             "uuid": j.get("id"), "alterId": int(j.get("aid") or 0), "cipher": j.get("scy") or "auto", "udp": False}
        if str(j.get("tls") or "") == "tls":
            d["tls"] = True
            if j.get("sni"):
                d["servername"] = j["sni"]
        net = j.get("net") or "tcp"
        d["network"] = net
        if net == "ws":
            d["ws-opts"] = {"path": j.get("path") or "/", "headers": {"Host": j.get("host") or j.get("add")}}
        return d
    if scheme in ("hysteria2", "hy2"):
        d = {"name": name, "type": "hysteria2", "server": u.hostname, "port": u.port, "password": unquote(u.username or "")}
        if _q(qs, "sni"):
            d["sni"] = _q(qs, "sni")
        if _q(qs, "insecure") in ("1", "true"):
            d["skip-cert-verify"] = True
        return d
    raise ValueError(f"不认识的格式: {scheme}://")


# ---------------- spec 读写与增删 ----------------
def load_spec(path: Path = SPEC_PATH) -> dict:
    if not path.exists():
        return json.loads(json.dumps(DEFAULT_SPEC))
    with path.open("rb") as f:
        spec = tomllib.load(f)
    for k, v in DEFAULT_SPEC.items():  # 补齐缺的段，不覆盖已有
        spec.setdefault(k, json.loads(json.dumps(v)))
    spec["homebb"].setdefault("nodes", [])
    spec["daily"].setdefault("subscriptions", [])
    return spec


def save_spec(spec: dict, path: Path = SPEC_PATH) -> None:
    text = dump_toml(spec)
    tomllib.loads(text)  # 写出前自检
    path.write_text(text, encoding="utf-8")


def add_home_node(spec: dict, node: dict) -> None:
    nodes = spec["homebb"]["nodes"]
    names = {n["name"] for n in nodes}
    base = node["name"]
    i = 2
    while node["name"] in names:
        node["name"] = f"{base}-{i}"
        i += 1
    nodes.append(node)


def remove_home_node(spec: dict, name: str) -> bool:
    nodes = spec["homebb"]["nodes"]
    keep = [n for n in nodes if n["name"] != name]
    spec["homebb"]["nodes"] = keep
    return len(keep) != len(nodes)


def set_home_sub(spec: dict, src: str) -> None:
    spec["homebb"]["subscription"] = {"url": src} if "://" in src else {"file": src}


def clear_home_sub(spec: dict) -> bool:
    return spec["homebb"].pop("subscription", None) is not None


def add_daily(spec: dict, name: str, src: str) -> None:
    subs = spec["daily"]["subscriptions"]
    subs[:] = [s for s in subs if s.get("name") != name]
    subs.append({"name": name, "url": src} if "://" in src else {"name": name, "file": src})


def remove_daily(spec: dict, name: str) -> bool:
    subs = spec["daily"]["subscriptions"]
    keep = [s for s in subs if s.get("name") != name]
    spec["daily"]["subscriptions"] = keep
    return len(keep) != len(subs)


def describe(spec: dict) -> str:
    out: list[str] = []
    hb = spec["homebb"]
    out.append("家宽节点：" + (", ".join(f"{n['name']}({n['type']} {n['server']}:{n['port']})" for n in hb["nodes"]) or "（无）"))
    sub = hb.get("subscription")
    out.append("家宽订阅：" + (sub.get("url") or sub.get("file") if sub else "（无）"))
    subs = spec["daily"]["subscriptions"]
    out.append("日常订阅：" + (", ".join(f"{s['name']}({'url' if s.get('url') else 'file'})" for s in subs) or "（无）"))
    out.append(f"直连 IP：{spec['direct'].get('ips') or '（无）'}   DNS 随家宽：{spec['dns'].get('via_homebb', True)}   家宽口：127.0.0.1:{hb.get('listener_port', 7901)}")
    return "\n".join(out)


def generate(spec_path: Path, install: bool = False, yes: bool = False) -> int:
    sys.path.insert(0, str(HERE))
    import genconfig  # noqa: E402

    argv = [str(spec_path), "--out", str(HERE / "generated")]
    if install:
        argv += ["--install"] + (["--yes"] if yes else [])
    return genconfig.main(argv)


# ---------------- 菜单 ----------------
def _ask(prompt: str, default: str = "") -> str:
    s = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    return s or default


def menu() -> int:
    spec = load_spec()
    print(f"配置文件：{SPEC_PATH}\n")
    while True:
        print(describe(spec))
        print("\n 1) 添加家宽节点（粘贴 socks5:// vless:// trojan:// ss:// vmess:// hysteria2:// 链接，或 host:port:user:pass）")
        print(" 2) 设置家宽订阅（URL 或本地 yaml）      3) 添加日常订阅（名字 + URL/本地 yaml）")
        print(" 4) 删除家宽节点        5) 删除家宽订阅        6) 删除日常订阅")
        print(" 7) 设置直连 IP（自家 SSH 机器）          8) DNS 是否随家宽 fail-closed")
        print(" 9) 生成配置到 generated/                10) 生成并安装到 Clash Verge（会备份原文件）")
        print(" 0) 保存并退出")
        c = _ask("选择")
        try:
            if c == "1":
                raw = _ask("节点")
                if not raw:
                    continue
                http = raw.count(":") == 3 and _ask("简写格式：socks5 还是 http", "socks5") == "http"
                node = parse_node(raw, default_name=_ask("名字", f"home-{len(spec['homebb']['nodes']) + 1}"), prefer_http=http)
                add_home_node(spec, node)
                print(f"已添加 {node['name']}")
            elif c == "2":
                src = _ask("家宽订阅 URL 或文件路径")
                if src:
                    set_home_sub(spec, src)
            elif c == "3":
                name = _ask("订阅名字（英文/中文都行，不要空格）")
                src = _ask("URL 或本地 yaml 路径")
                if name and src:
                    add_daily(spec, name, src)
            elif c == "4":
                print("已删除" if remove_home_node(spec, _ask("节点名字")) else "没有这个节点")
            elif c == "5":
                print("已删除" if clear_home_sub(spec) else "本来就没有")
            elif c == "6":
                print("已删除" if remove_daily(spec, _ask("订阅名字")) else "没有这个订阅")
            elif c == "7":
                spec["direct"]["ips"] = [x.strip() for x in _ask("IP 列表，逗号分隔（留空清空）").split(",") if x.strip()]
            elif c == "8":
                spec["dns"]["via_homebb"] = _ask("DNS 走家宽（家宽挂了 DNS 一起断）y/n", "y").lower().startswith("y")
            elif c in ("9", "10"):
                save_spec(spec)
                if c == "10":
                    if _ask("会覆盖 Clash Verge 里的 Merge.yaml / Script.js（自动备份）。输入 yes 确认") != "yes":
                        continue
                    print("提示：如果之前 --pin 上过锁，先 python3 watch.py --unpin")
                rc = generate(SPEC_PATH, install=(c == "10"), yes=True)
                print("完成" if rc == 0 else f"失败（exit {rc}）")
            elif c == "0":
                save_spec(spec)
                print(f"已保存 {SPEC_PATH}")
                return 0
        except (ValueError, KeyError, json.JSONDecodeError) as e:
            print(f"出错：{e}")
        print()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="clash-ai-homebb 引导 / 增删家宽与订阅")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("add-home", help="添加家宽节点");
    p.add_argument("node"); p.add_argument("--name", default=""); p.add_argument("--http", action="store_true", help="host:port 简写按 http 而非 socks5")
    sub.add_parser("add-home-sub", help="设置家宽订阅").add_argument("src")
    p = sub.add_parser("add-daily", help="添加日常订阅"); p.add_argument("name"); p.add_argument("src")
    sub.add_parser("remove-home").add_argument("name")
    sub.add_parser("remove-home-sub")
    sub.add_parser("remove-daily").add_argument("name")
    sub.add_parser("list")
    p = sub.add_parser("generate"); p.add_argument("--install", action="store_true"); p.add_argument("--yes", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        return menu()
    spec = load_spec()
    try:
        if a.cmd == "add-home":
            node = parse_node(a.node, default_name=a.name or f"home-{len(spec['homebb']['nodes']) + 1}", prefer_http=a.http)
            if a.name:
                node["name"] = a.name
            add_home_node(spec, node)
            print(f"已添加 {node['name']} ({node['type']} {node['server']}:{node['port']})")
        elif a.cmd == "add-home-sub":
            set_home_sub(spec, a.src); print("已设置家宽订阅")
        elif a.cmd == "add-daily":
            add_daily(spec, a.name, a.src); print(f"已添加日常订阅 {a.name}")
        elif a.cmd == "remove-home":
            print("已删除" if remove_home_node(spec, a.name) else "没有这个节点")
        elif a.cmd == "remove-home-sub":
            print("已删除" if clear_home_sub(spec) else "本来就没有")
        elif a.cmd == "remove-daily":
            print("已删除" if remove_daily(spec, a.name) else "没有这个订阅")
        elif a.cmd == "list":
            print(describe(spec)); return 0
        elif a.cmd == "generate":
            save_spec(spec)
            return generate(SPEC_PATH, install=a.install, yes=a.yes)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"出错：{e}", file=sys.stderr); return 2
    save_spec(spec)
    print(describe(spec))
    return 0


if __name__ == "__main__":
    sys.exit(main())
