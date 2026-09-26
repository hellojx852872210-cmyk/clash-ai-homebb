#!/usr/bin/env bash
# 卸载 launchd 任务（不删项目、不碰 Clash 配置、不解锁文件；解锁请 python3 watch.py --unpin）
set -uo pipefail
for label in io.github.clash-ai-homebb.watch io.github.clash-ai-homebb.float io.github.clash-ai-homebb.guard; do
  launchctl bootout "gui/$(id -u)/$label" >/dev/null 2>&1 && echo "已卸载 $label"
  rm -f "$HOME/Library/LaunchAgents/$label.plist"
done
echo "完成"
