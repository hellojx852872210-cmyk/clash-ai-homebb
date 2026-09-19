# Changelog

## Unreleased

- 新增 `wizard.py` 交互式引导与命令行增删：粘贴 socks5 / http / vless / trojan / ss / vmess / hysteria2 分享链接、`host:port:user:pass` 简写或 JSON 即可添加家宽节点；家宽订阅、日常订阅随时增删；生成/安装一键完成。
- `genconfig.py --install` 遇到被 uchg 锁住的目标文件时给出明确提示。
- 向导菜单：任意提示处输入 b 返回、按编号/名字删除并二次确认、未保存改动提示、s 保存 / 0 保存退出 / q 不保存退出、Ctrl-C 不写文件、`--out` 指定生成目录。
- 新增 `scripts/acceptance.sh` 本地验收脚本（隔离目录，不碰真实配置；没有 pytest 自动用 unittest）。
- 新增 `start.py`：检测客户环境（系统/Python/curl/Clash Verge/内核/TUN/系统代理/端口/是否已装/锁/launchd）并按缺什么补什么一步步引导，`--check` 只出报告。

## 0.1.0 (2026-09-19)

首个可供他人使用的版本。

- 新增 `genconfig.py`：从 `deadchain.toml` 生成 Merge.yaml / Script.js / providers / config.toml / INSTALL.md，
  家宽支持直接写节点或订阅，日常订阅支持 URL 与本地文件。
- 所有机器/策略相关值移到 `config.toml`（`config.py`），代码不再写死 IP、组名、路径。
- 状态码改为通用名：`resip_down→homebb_down`、`flower_down→daily_down`、`leak_7901_flower→leak_homebb_is_daily`、
  `leak_ai_flower→leak_ai_via_daily`、`group_not_deadchain→deadchain_broken`、`ssh_exclude_missing→direct_route_missing`。
  state.json 字段 `flower_ip/resip_ip/openai_via_7897/openai_chain` 改为 `daily_ip/homebb_ip/ai_via_daily/ai_chain`。
- 悬浮窗混合状态逐条标注 host 走家宽还是日常。
- 配置锁（uchg + sha256）、本地告警去抖与重复提醒。
- launchd 模板 + `install.sh` / `uninstall.sh`。
