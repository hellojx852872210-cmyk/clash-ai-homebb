#!/usr/bin/env bash
# 把 panel.py 包成桌面 App「家宽选择器.app」：双击看状态，一键启用 / 停用监控和悬浮窗。
#   ./scripts/build-app.sh               生成到 dist/ 并装进 /Applications（不可写就装 ~/Applications）
#   ./scripts/build-app.sh --no-install  只生成到 dist/
# App 里只有启动脚本和图标，代码还是从本项目目录跑：改代码不用重装；挪了项目目录要重跑本脚本。
set -euo pipefail
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
NAME="家宽选择器"
INSTALL=1
[[ "${1:-}" == "--no-install" ]] && INSTALL=0

pick_python_hud() {  # 3.11+ 且带 PyObjC，和 install.sh 同一串候选
  for p in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /Library/Frameworks/Python.framework/Versions/Current/bin/python3; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys, AppKit; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      command -v "$p"; return
    fi
  done
}
PY="$(pick_python_hud)"
[[ -n "$PY" ]] || { echo "需要带 PyObjC 的 Python 3.11+：python3 -m pip install pyobjc-framework-Cocoa" >&2; exit 1; }
VERSION="$("$PY" -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb"))["project"]["version"])' "$PROJECT/pyproject.toml")"
echo "面板用 Python: $PY"

APP="$PROJECT/dist/$NAME.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
"$PY" "$PROJECT/scripts/make_icon.py" "$APP/Contents/Resources/AppIcon.icns"

render() {  # 从 stdin 读模板，换掉占位符
  sed -e "s#__PROJECT__#$PROJECT#g" -e "s#__PYTHON__#$PY#g" -e "s#__VERSION__#$VERSION#g" -e "s#__NAME__#$NAME#g"
}

render > "$APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>
  <string>__NAME__</string>
  <key>CFBundleDisplayName</key>
  <string>__NAME__</string>
  <key>CFBundleIdentifier</key>
  <string>io.github.clash-ai-homebb.panel</string>
  <key>CFBundleExecutable</key>
  <string>launcher</string>
  <key>CFBundleIconFile</key>
  <string>AppIcon</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>__VERSION__</string>
  <key>CFBundleVersion</key>
  <string>__VERSION__</string>
  <key>LSMinimumSystemVersion</key>
  <string>11.0</string>
  <key>NSHighResolutionCapable</key>
  <true/>
  <key>LSApplicationCategoryType</key>
  <string>public.app-category.utilities</string>
</dict>
</plist>
EOF

render > "$APP/Contents/MacOS/launcher" <<'EOF'
#!/bin/bash
# 由 scripts/build-app.sh 生成：用带 PyObjC 的 Python 跑项目里的 panel.py。
PROJECT="__PROJECT__"
RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin:$PATH"
fail() {
  /usr/bin/osascript -e "display alert \"__NAME__打不开\" message \"$1\" as critical" >/dev/null 2>&1
  exit 1
}
[[ -f "$PROJECT/panel.py" ]] || fail "找不到 $PROJECT/panel.py。项目挪过位置的话，在新位置重跑 scripts/build-app.sh。"
# 打包时挑好的 Python 优先（不再试 import，启动快）；它没了再按 install.sh 的顺序找
[[ -x "__PYTHON__" ]] && exec "__PYTHON__" "$PROJECT/panel.py" --icon "$RES/AppIcon.icns"
for p in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /Library/Frameworks/Python.framework/Versions/Current/bin/python3; do
  if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys, AppKit; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    exec "$p" "$PROJECT/panel.py" --icon "$RES/AppIcon.icns"
  fi
done
fail "没找到带 PyObjC 的 Python 3.11+。在终端运行：python3 -m pip install pyobjc-framework-Cocoa"
EOF
chmod +x "$APP/Contents/MacOS/launcher"
plutil -lint "$APP/Contents/Info.plist" >/dev/null
echo "已生成 $APP"

if [[ $INSTALL -eq 1 ]]; then
  DEST=/Applications
  [[ -w "$DEST" ]] || { DEST="$HOME/Applications"; mkdir -p "$DEST"; }
  rm -rf "${DEST:?}/$NAME.app"
  ditto "$APP" "$DEST/$NAME.app"
  # 让 Finder / 启动台 / 聚焦立刻认出新图标
  /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$DEST/$NAME.app" >/dev/null 2>&1 || true
  echo "已装到 $DEST/$NAME.app（聚焦搜「${NAME}」或在启动台打开，拖到程序坞更顺手）"
fi
