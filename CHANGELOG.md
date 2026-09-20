# Changelog

## Unreleased

- 悬浮窗可以直接改出口：点一下展开「家宽 / 直连 / 代理 / 清除覆盖 / 取消」，选完即时生效；拖动照旧，12 秒不操作自动收起。
  点击不再把窗口带偏（位移小于阈值当手抖，窗口不动），点下即展开不等定时器，规则集状态由后台缓存、点击零等待。
- 新增 `route.py`：看当前哪个 App 走家宽/直连/代理，把它改到别的出口并记住。改动写进 `routes.toml`，
  渲染成三个 classical 文件型规则集由 mihomo 直接重读，不用解锁 Merge.yaml、不重载整份配置、不断开已有连接。
- 三条 `RULE-SET` 规则由生成器排在 AI 死链规则之后，覆盖规则改不动 AI 出口；把 AI 域名设成直连/代理会被拒绝。
- `genconfig.py` 输出 `rule-providers` 与规则集文件，并在重新生成时保留已有覆盖；`Script.js` 放行 `RULE-SET,user-*` 并补齐 `rule-providers`。

## 0.2.0 (2026-09-19)

- 向导：菜单上直接给出「下一步选什么」及理由，回车即按建议执行；每个输入处附示例；引导里的确认项标明推荐答案。
- 新增 `wizard.py` 交互式引导与命令行增删：粘贴 socks5 / http / vless / trojan / ss / vmess / hysteria2 分享链接、`host:port:user:pass` 简写或 JSON 即可添加家宽节点；家宽订阅、日常订阅随时增删；生成/安装一键完成。
- `genconfig.py --install` 遇到被 uchg 锁住的目标文件时给出明确提示。
- 向导菜单：任意提示处输入 b 返回、按编号/名字删除并二次确认、未保存改动提示、s 保存 / 0 保存退出 / q 不保存退出、Ctrl-C 不写文件、`--out` 指定生成目录。
- 新增 `scripts/acceptance.sh` 本地验收脚本（隔离目录，不碰真实配置；没有 pytest 自动用 unittest）。
- 新增 `start.py`：检测客户环境（系统/Python/curl/Clash Verge/内核/TUN/系统代理/端口/是否已装/锁/launchd）并按缺什么补什么一步步引导，`--check` 只出报告。
- 监控：控制器暂时没返回 AI 组时判为 `core_reloading`（警告）而不是 `deadchain_broken`；`core_reloading` / `clash_dead` / `deadchain_broken` 默认加入去抖，切换配置档引起的内核重载不再弹窗。


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
