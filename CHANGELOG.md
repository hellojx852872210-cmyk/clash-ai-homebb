# Changelog

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
