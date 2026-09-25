"""Clash Verge Rev 的版本差异收在这里：控制器在哪、服务模式为什么没起来、哪些文件不能上 uchg。

Verge 2.5.5（2026-09）起，服务模式先把配置引用的 file 型 provider / 规则集复制进 root 的 runtime 目录，
再用那份副本启动内核。由此带来三处和旧版不同：
  * 控制器 socket 从 /tmp/verge/verge-mihomo.sock 挪到
    /var/run/clash-verge-service/users/<uid>/verge-mihomo.sock（服务模式）或 $TMPDIR/verge-mihomo.sock（sidecar）；
  * 源文件带 uchg 时复制失败（EPERM），服务起不来内核，Verge 退回 sidecar 并自动关掉 TUN；
  * 内核读的是副本，改源文件再 PUT /providers/rules/<name> 不会生效（出口覆盖因此改用 http 规则集，见 routes.py）。
这里只做探测，不改 Verge 的任何文件；全部是纯函数或只读，方便测试。
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

VERGE_DIR_DEFAULT = "~/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev"
LEGACY_SOCKET = "/tmp/verge/verge-mihomo.sock"
SERVICE_RUN_DIR = "/var/run/clash-verge-service/"
SERVICE_SOCKET = SERVICE_RUN_DIR + "users/{uid}/verge-mihomo.sock"
SOCKET_NAME = "verge-mihomo.sock"
LOG_TAIL_BYTES = 256 * 1024


@dataclass(frozen=True)
class Controller:
    kind: str       # "unix" | "tcp"
    address: str    # socket 路径，或 host:port
    secret: str = ""
    source: str = ""  # 从哪来的，给报告看

    def label(self) -> str:
        return f"{self.address}（{self.source}）" if self.source else self.address

    @property
    def is_service(self) -> bool:
        """内核由 Verge 服务托管（file 型 provider 用的是复制品）。"""
        return self.kind == "unix" and "/clash-verge-service/" in self.address


def user_temp_dir() -> str:
    """当前用户的 $TMPDIR。launchd 起的进程不一定带这个环境变量，所以再问一次系统。"""
    t = os.environ.get("TMPDIR", "")
    if t:
        return t
    try:
        return subprocess.run(["getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def top_level_scalars(text: str) -> dict[str, str]:
    """只取 YAML 顶层的 `key: value` 标量行（Verge 的 config.yaml 就是这种扁平写法）。不做完整 YAML 解析。"""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z0-9_-]+):[ \t]+(.+?)[ \t]*$", line)
        if not m:
            continue
        v = m.group(2)
        if v[:1] in "\"'" and v[-1:] == v[:1] and len(v) >= 2:
            v = v[1:-1]
        out[m.group(1)] = v
    return out


def read_verge_config(verge_dir: Path) -> dict[str, str]:
    try:
        return top_level_scalars((verge_dir / "config.yaml").read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return {}


def candidates(
    socket: str = "",
    controller: str = "",
    secret: str = "",
    *,
    auto: bool = True,
    verge_conf: dict[str, str] | None = None,
    uid: int | None = None,
    tmpdir: str = "",
) -> list[Controller]:
    """按优先级列出可能的控制器；调用方逐个试 /version，第一个有响应的就是它。

    config.toml 里写了的最先试；auto=True 时再按 Verge 各版本的位置补上。
    """
    out: list[Controller] = []

    def add(c: Controller) -> None:
        if c.address and all((c.kind, c.address) != (x.kind, x.address) for x in out):
            out.append(c)

    if socket:
        add(Controller("unix", socket, source="config.toml"))
    if controller:
        add(Controller("tcp", controller, secret, source="config.toml"))
    if not auto:
        return out
    conf = verge_conf or {}
    add(Controller("unix", SERVICE_SOCKET.format(uid=os.getuid() if uid is None else uid), source="Verge 服务模式"))
    unix = conf.get("external-controller-unix", "")
    if unix:
        add(Controller("unix", unix, source="Verge config.yaml"))
    if tmpdir:
        add(Controller("unix", str(Path(tmpdir) / SOCKET_NAME), source="Verge sidecar"))
    add(Controller("unix", LEGACY_SOCKET, source="Verge 2.5.2 及更早"))
    tcp = conf.get("external-controller", "")
    if tcp:
        add(Controller("tcp", tcp, conf.get("secret", ""), source="Verge config.yaml"))
    return out


# ---------------- 服务模式为什么没起来 ----------------
_MODE = re.compile(r"Core running mode changed: *\S+ *-> *(\S+)")
# Verge 只在「服务连得上、但拒绝启动内核」时打这一行（服务没装走的是另一条「无法连接到Clash Verge Service」）
_FAIL = re.compile(r"\[Service\].*?(?:启动核心失败|[Ff]ailed to start (?:the )?core)[^:：]*[:：] *(.+)$")
_SERVICE_OK = ("服务成功启动核心",)
_ASSET = re.compile(r"failed to write the runtime asset (.+?): ")


def service_failure(log_text: str) -> str:
    """从 Verge 日志判断：最近一次让服务启动内核失败了、且内核现在不在服务模式。返回失败原因，没有返回空串。

    调用方拿到控制器后还应再核对一次：内核实际就在服务模式时忽略它（日志措辞可能随版本变）。
    """
    mode = ""
    fail = ""
    for line in log_text.splitlines():
        m = _MODE.search(line)
        if m:
            mode = m.group(1)
            if mode == "Service":
                fail = ""
            continue
        if any(k in line for k in _SERVICE_OK):
            fail = ""
            continue
        m = _FAIL.search(line)
        if m:
            fail = m.group(1).strip()
    return fail if fail and mode != "Service" else ""


def failed_asset(reason: str) -> str:
    """失败原因里点名的那个文件（相对 Verge 数据目录），没有返回空串。"""
    m = _ASSET.search(reason)
    return m.group(1).strip() if m else ""


def read_log_tail(verge_dir: Path, limit: int = LOG_TAIL_BYTES) -> str:
    p = verge_dir / "logs" / "latest.log"
    try:
        with p.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - limit))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def failure_hint(reason: str) -> str:
    if "Operation not permitted" in reason or "os error 1" in reason:
        f = failed_asset(reason)
        return (f"{f or 'Verge 数据目录里有文件'} 带 uchg 锁，服务复制不了。"
                "跑 python3 watch.py --migrate-lock 解开（只解 file 型订阅副本，保留 sha256 校验），再重开虚拟网卡模式")
    return "打开 Verge → 设置 → 服务模式，看报错；修好后重开虚拟网卡模式"


# ---------------- 哪些文件能上 uchg ----------------
def provider_entries(text: str) -> list[dict[str, str]]:
    """Verge 生成的 clash-verge.yaml 里 proxy-providers / rule-providers 的每一项：{section, name, type, path}。

    只认 Verge 写出的形态：顶层 section、两格缩进的名字、四格缩进的标量，或名字后面跟一行 {..} 流式映射。
    不做完整 YAML 解析，认不出的项 type/path 为空。
    """
    out: list[dict[str, str]] = []
    section = ""
    cur: dict[str, str] | None = None
    for line in text.splitlines():
        body = line.strip()
        if not body or body.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            key = body.split(":", 1)[0]
            section = key if key in ("proxy-providers", "rule-providers") and body.endswith(":") else ""
            cur = None
            continue
        if not section:
            continue
        if indent == 2:
            name, _, rest = body.partition(":")
            cur = {"section": section, "name": name.strip().strip("\"'"), "type": "", "path": ""}
            out.append(cur)
            rest = rest.strip()
            if rest.startswith("{"):
                for k in ("type", "path"):
                    m = re.search(rf"[\"']?{k}[\"']?\s*:\s*[\"']?([^,\"'}}]+)", rest)
                    if m:
                        cur[k] = m.group(1).strip()
            continue
        if indent == 4 and cur is not None:
            k, _, v = body.partition(":")
            k = k.strip()
            if k in ("type", "path") and not cur[k]:
                cur[k] = v.strip().strip("\"'")
    return out


def file_asset_paths(verge_dir: Path) -> set[Path] | None:
    """Verge 服务会复制的文件：当前运行配置里 file 型 provider 的 path。读不到运行配置返回 None。

    url 型订阅的缓存不在其中：服务不复制它们（内核自己下载到 runtime 目录），上 uchg 对服务无影响。
    """
    try:
        text = (verge_dir / "clash-verge.yaml").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    out: set[Path] = set()
    for e in provider_entries(text):
        if e["type"] != "file" or not e["path"]:
            continue
        p = Path(os.path.expanduser(e["path"]))
        out.add((p if p.is_absolute() else verge_dir / p).resolve())
    return out


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def is_runtime_asset(path: Path, verge_dir: Path, assets: set[Path] | None = None) -> bool:
    """这个文件会不会被 Verge 服务复制进 runtime 目录（会的就不能上 uchg：复制把标记一起带过去，随后替换失败）。

    assets 是 file_asset_paths() 的结果，以它为准；拿不到时退回按目录判断：Verge 数据目录下、profiles/ 以外。
    profiles/ 里的 Merge.yaml / Script.js 由 Verge 前台进程读、合成后再交给服务，上锁没问题。
    """
    if assets is not None:
        return path.resolve() in assets
    return _inside(path, verge_dir) and not _inside(path, verge_dir / "profiles")
