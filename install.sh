#!/usr/bin/env bash
# 安装/更新 launchd 任务：监控（每 3 分钟）+ 悬浮窗（常驻）。只在本用户的 LaunchAgents 里操作，不碰 Clash 配置。
#   ./install.sh            安装两个任务
#   ./install.sh --no-hud   只装监控，不装悬浮窗
set -euo pipefail
PROJECT="$(cd "$(dirname "$0")" && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
WITH_HUD=1
[[ "${1:-}" == "--no-hud" ]] && WITH_HUD=0

pick_python() {  # 需要 3.11+（tomllib）
  for p in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /Library/Frameworks/Python.framework/Versions/Current/bin/python3; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      command -v "$p"; return
    fi
  done
  echo "需要 Python 3.11+（brew install python）" >&2; exit 1
}
pick_python_hud() {  # 悬浮窗还需要 PyObjC
  for p in "$PY" /Library/Frameworks/Python.framework/Versions/Current/bin/python3 /opt/homebrew/bin/python3; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import AppKit' 2>/dev/null; then command -v "$p"; return; fi
  done
  echo ""  # 没有
}

PY="$(pick_python)"
echo "监控用 Python: $PY"
[[ -f "$PROJECT/config.toml" ]] || { cp "$PROJECT/config.example.toml" "$PROJECT/config.toml"; echo "已生成 config.toml（请按你的 Clash 配置修改）"; }
"$PY" "$PROJECT/watch.py" --print-config >/dev/null || { echo "config.toml 解析失败" >&2; exit 1; }

render() {  # $1 模板 $2 目标
  sed -e "s#__PROJECT__#$PROJECT#g" -e "s#__PYTHON__#$PY#g" -e "s#__PYTHON_HUD__#$PY_HUD#g" "$1" > "$2"
}
load() {  # $1 label $2 plist
  launchctl bootout "gui/$(id -u)/$1" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$2"
  echo "已加载 $1"
}

mkdir -p "$AGENTS"
PY_HUD=""
render "$PROJECT/launchd/io.github.clash-ai-homebb.watch.plist.in" "$AGENTS/io.github.clash-ai-homebb.watch.plist"
load io.github.clash-ai-homebb.watch "$AGENTS/io.github.clash-ai-homebb.watch.plist"

if [[ $WITH_HUD -eq 1 ]]; then
  PY_HUD="$(pick_python_hud)"
  if [[ -z "$PY_HUD" ]]; then
    echo "没找到带 PyObjC 的 Python，跳过悬浮窗。要装：$PY -m pip install pyobjc-framework-Cocoa，然后重跑 ./install.sh"
  else
    echo "悬浮窗用 Python: $PY_HUD"
    render "$PROJECT/launchd/io.github.clash-ai-homebb.float.plist.in" "$AGENTS/io.github.clash-ai-homebb.float.plist"
    load io.github.clash-ai-homebb.float "$AGENTS/io.github.clash-ai-homebb.float.plist"
  fi
fi
echo "完成。先跑一轮看结果： $PY $PROJECT/watch.py"
