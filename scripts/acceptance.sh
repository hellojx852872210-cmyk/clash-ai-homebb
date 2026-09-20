#!/usr/bin/env bash
# 本地验收：全程在隔离目录里跑，不碰你的 deadchain.toml、generated/、Clash 配置、launchd。
#   ./scripts/acceptance.sh          # 自动部分 + 交互部分
#   ./scripts/acceptance.sh --auto   # 只跑自动部分（CI 也能用）
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/clash-ai-homebb-accept.XXXXXX")"
export CLASH_AI_HOMEBB_SPEC="$WORK/deadchain.toml"
OUT="$WORK/generated"
echo "隔离目录：$WORK"
echo

echo "== 1/4 单元测试 =="
if "$PY" -c "import pytest" 2>/dev/null; then "$PY" -m pytest -q; else echo "（没有 pytest，用标准库 unittest）"; "$PY" -m unittest discover -s tests; fi
echo

echo "== 2/4 命令行增删 + 生成 =="
printf 'proxies:\n  - {name: "HK-1", type: ss, server: 198.51.100.10, port: 8388, cipher: aes-128-gcm, password: x}\n' > "$WORK/sub-b.yaml"
"$PY" wizard.py add-home '203.0.113.5:1080:user:pass' --name 家宽A >/dev/null
"$PY" wizard.py add-home 'vless://11111111-2222-3333-4444-555555555555@entry.example.com:443?security=reality&sni=swdist.apple.com&fp=chrome&pbk=PUBKEY&sid=0123abcd&flow=xtls-rprx-vision#家宽B' >/dev/null
"$PY" wizard.py add-daily 机场A 'https://a.example/sub?target=clash' >/dev/null
"$PY" wizard.py add-daily 机场B "$WORK/sub-b.yaml" >/dev/null
"$PY" wizard.py remove-home 家宽A >/dev/null
"$PY" wizard.py list
"$PY" wizard.py generate --out "$OUT" >/dev/null
test -s "$OUT/Merge.yaml" && test -s "$OUT/Script.js" && test -s "$OUT/config.toml" && test -s "$OUT/ai-homebb-providers/机场B.yaml"
"$PY" - "$OUT/config.toml" "$CLASH_AI_HOMEBB_SPEC" <<'PYEOF'
import sys, tomllib
c = tomllib.load(open(sys.argv[1], "rb")); s = tomllib.load(open(sys.argv[2], "rb"))
assert [n["name"] for n in s["homebb"]["nodes"]] == ["家宽B"], s["homebb"]["nodes"]
assert [x["name"] for x in s["daily"]["subscriptions"]] == ["机场A", "机场B"]
assert c["deadchain"]["homebb_members"] == ["家宽B"]
print("产物校验通过")
PYEOF
echo "— 出口覆盖 route.py（只写隔离目录，不碰 Clash）—"
printf '[routes]\nfile = "%s/routes.toml"\ndir = "%s/rules"\n' "$WORK" "$WORK" > "$WORK/route.toml"
export CLASH_AI_HOMEBB_CONFIG="$WORK/route.toml"
"$PY" route.py set Telegram 家宽 >/dev/null
"$PY" route.py set github.com 直连 >/dev/null
if "$PY" route.py set claude.ai 直连 >/dev/null 2>&1; then echo "AI 域名竟然被接受，应当拒绝"; exit 1; fi
echo "  AI 域名改出口被拒绝（正确）"
grep -q "PROCESS-NAME,Telegram" "$WORK/rules/user-homebb.yaml" || { echo "家宽规则集没写对"; exit 1; }
grep -q "github.com" "$WORK/rules/user-direct.yaml" || { echo "直连规则集没写对"; exit 1; }
echo "  规则集渲染正确"
"$PY" route.py list | sed 's/^/  /'
unset CLASH_AI_HOMEBB_CONFIG
export CLASH_AI_HOMEBB_SPEC="$WORK/deadchain.toml"
echo

MIHOMO="/Applications/Clash Verge.app/Contents/MacOS/verge-mihomo"
if [[ -x "$MIHOMO" ]]; then
  echo "（有 mihomo 内核，做一次真实 -t 校验）"
  "$PY" - "$OUT" "$WORK" <<'PYEOF'
import sys, json, os, shutil
from pathlib import Path
out, work = Path(sys.argv[1]), Path(sys.argv[2])
home = work / "home"; home.mkdir(exist_ok=True)
shutil.copytree(out / "ai-homebb-providers", home / "ai-homebb-providers", dirs_exist_ok=True)
# 生成的 Merge 全是 JSON 流式值，不需要 PyYAML：直接拼一个最小运行时
lines = (out / "Merge.yaml").read_text(encoding="utf-8").splitlines()
def block(key):
    items, on = [], False
    for l in lines:
        if l.startswith(key + ":"): on = True; continue
        if on and l.startswith("- "): items.append(json.loads(l[2:]))
        elif on and not l.startswith(" "): on = False
    return items
prov = {}
for l in lines:
    if l.startswith('  "sub-') or l.startswith('  "homebb-sub"'):
        k, v = l.strip().split(": ", 1); prov[json.loads(k)] = json.loads(v)
cfg = {"mixed-port": 17899, "mode": "rule", "log-level": "silent", "proxy-providers": prov,
       "proxies": block("prepend-proxies"), "proxy-groups": block("prepend-proxy-groups"),
       "rules": block("prepend-rules") + ["MATCH,DIRECT"]}
(home / "config.yaml").write_text(json.dumps(cfg, ensure_ascii=False))  # JSON 即 YAML
PYEOF
  if "$MIHOMO" -t -d "$WORK/home" -f "$WORK/home/config.yaml" 2>&1 | grep -q "test is successful"; then echo "mihomo -t 通过"; else echo "mihomo -t 失败（示例含占位公钥时属预期，真实节点应通过）"; fi
fi
echo

if [[ "${1:-}" == "--auto" ]]; then echo "自动部分完成。隔离目录：$WORK"; exit 0; fi

echo "== 3/4 交互验收（在隔离配置上操作，随便试）=="
cat <<'TXT'
请在菜单里依次验证，每步的预期已写在后面：
  a. 选 1 添加节点，粘贴 203.0.113.9:1080:u:p → 名字回车用默认 → 确认 y      预期：列表多一条
  b. 选 1，在「粘贴节点」处输入 b                                            预期：提示已返回，列表不变
  c. 选 4 删除，输入编号，确认处输入 n                                       预期：没删
  d. 选 4 删除，输入编号，确认 y                                            预期：删掉
  e. 选 3 添加日常订阅，名字 机场C，URL 随便一个 https://                    预期：多一条，菜单出现「有未保存的改动」
  f. 输入 q，确认处 n                                                       预期：留在菜单
  g. 输入 s 保存，再输入 0 退出                                              预期：退出后下面第 4 步能看到机场C
TXT
"$PY" wizard.py --out "$OUT" || true
echo

echo "== 4/4 退出后的最终状态 =="
"$PY" wizard.py list
echo
echo "验收完成。隔离目录：$WORK（不需要可删）。你的真实 deadchain.toml / generated/ / Clash 配置未被触碰。"
