# Changelog

## Unreleased

- 新增出口守护 `guard.py`（常驻，`install.sh` 一起装，桌面 App 里可开关）：每 5 秒测没设覆盖的流量、每 30 秒测 AI 链，两次都明确不通才动手（测速接口出错不算不通）；分析是节点挂了、自动组被钉住、手动组选了死节点、整份订阅挂了还是本机上行断了，再从最靠近节点的一层往上修（解开钉住的自动组并立刻重测 → 手动组先组内换、再往上换到日常出口），修一层测一次，通了就停。AI 只在家宽组内换节点，家宽全挂时保持断开；日常流量不会被切到家宽、直连或拒绝，也不碰 AI 能走到的组。每次动手前现测目标、按最新状态复核，被人改过就停手；同一个故障只通知一次。`--explain` / `--dry-run` 不改任何选择。
- `api_json` 支持带 JSON 请求体（守护切换选择要用）。
- 监控：日常探测不通时，提示写出没设覆盖的流量实际走的整条链路；默认出口没经过日常出口（例如 Proxies 选了某个固定节点）时明说「挂了不会自动切换」，不再笼统说「所有订阅都不通」。日常出口组名可在 config.toml 的 `[deadchain] daily_group` 里改。
- 兼容 Clash Verge 2.5.5（服务模式先把 provider 复制进 root 目录再启动内核）：
  - 控制器自动发现：依次试 config.toml 里写的、2.5.5 服务模式 `/var/run/clash-verge-service/users/<uid>/verge-mihomo.sock`、
    Verge config.yaml 的 unix socket、`$TMPDIR/verge-mihomo.sock`、旧版 `/tmp/verge/verge-mihomo.sock`、config.yaml 的 TCP 控制器，
    以 `/version` 有响应为准（sidecar 退出后留下的死 socket 不再被当成「内核在跑」）；常驻的悬浮窗在 Verge 重启或切换模式后自己重新找，
    不再误报「Clash 未开」。`[clash] socket` 默认改为空。
  - 配置锁：Verge 运行配置（clash-verge.yaml）里 file 型 provider 引用的文件（订阅副本）只记 sha256、不再打 uchg——
    带 uchg 会让服务复制失败、内核退回 sidecar、TUN 开不了。url 订阅的缓存服务不复制，保持原来的锁。
    新增 `watch.py --migrate-lock`：只解开这些文件的 uchg，不重算已记的 sha256（内容对不上的不动、留给人核对）；
    `--pin` 也会按新规则上锁。监控发现订阅副本带 uchg 会点名文件并给出修法；`genconfig.py --install` 装订阅副本时也会解开旧锁。
  - 新状态码 `verge_service_failed`：读 Verge 日志，服务启动内核失败且内核不在服务模式时报出失败原文和修法；`start.py --check` 同步检出。
  - 出口覆盖规则集改为 `http` 型：改动时在 `127.0.0.1:7919`（`[routes] serve_port`）临时提供文件、PUT 让内核现拉；
    缓存放 `ai-homebb-rules-cache/`，和源文件分开。file 型在 2.5.5 服务模式下改了不生效，遇到时明确提示重新生成。
    监控每轮核对：记忆在上次推送后改过、或内核条数和上次推完时不同（例如缓存丢了）就自动重推；没有 routes.toml 时不推，
    内核认不了的规则不会导致每轮空推。端口被别的程序占着、给的内容不对时中止推送。
  - `direct_route_missing` 说明 2.5.5 以 Verge 自己的 TUN「排除自定义网段」为准，生成的 INSTALL.md 也加了这一步。
- 新增桌面 App「家宽选择器」：`scripts/build-app.sh` 把 `panel.py` 包成 .app 装进 /Applications。
  窗口里看判级、家宽 / 日常出口、AI 链路、配置锁和上次检查时间；两个开关分别启停监控和悬浮窗，
  「一键启用」全部打开，「立即检查」马上跑一轮；监控停着时顶部标明结果不再更新。`--snapshot` 不弹窗直接出截图。
- 新增 `agents.py`：按 plist 里的脚本路径认 launchd 任务（label 不同的旧任务也认，不会重复安装），
  停用 = bootout + disable（重新登录也不自启），启用 = enable + bootstrap，没有 plist 时按模板现装。
- `install.sh` 加载任务前先 `launchctl enable`，在面板里停用过的任务重新安装不会失败。
- 日常出口改为粘住优先：订阅组从「每 5 分钟重测、快 100ms 就换、闲时也测」改成「每 10 分钟、快 800ms 才换、闲时不测」，
  `日常-自动` 健康检查从 120 秒放宽到 300 秒。原参数会让出口 IP 每几分钟跳一次，日常上网被反复要求重新登录。
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
