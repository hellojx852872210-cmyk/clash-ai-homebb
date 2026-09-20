# clash-ai-homebb

让 **AI 流量只走家宽（住宅 IP）**，其它流量走 **任何能打通的订阅**，家宽挂了 AI 就断线、绝不回落到机场或真实 IP。
配套一个 macOS 监控（本地告警）、一个前台 App 出口悬浮窗，以及一把防误改的配置锁。

适用：macOS + [Clash Verge Rev](https://github.com/clash-verge-rev/clash-verge-rev)（mihomo 内核）。

## 它解决什么

Claude / ChatGPT / Grok 这类服务对出口 IP 很敏感，机房 IP 容易被风控，住宅家宽 IP 稳。常见做法是在 Clash 里把 AI 域名指到家宽节点，但：

- 家宽节点一挂，Clash 的 fallback 会把 AI 流量甩到机场节点，账号就在两个 IP 之间跳；
- 手滑改了分组、订阅更新覆盖了规则、脚本没生效，都没人告诉你；
- 你以为走的是家宽，其实浏览器走了 QUIC / WebRTC / 自带 DoH 绕过了规则。

本项目的做法：

| 层 | 做法 |
|---|---|
| 路由 | AI 域名 / 进程 / 专用入站口 → `AI 组`(select) → `家宽组`(fallback，只有家宽节点)。没有 DIRECT，没有机场，挂了就是断 |
| 日常 | 每份订阅一个 url-test，再按你给的顺序 fallback，包成「日常出口」插到 Proxies 第一位 |
| 防漏 | AI 域名 UDP/443 REJECT（逼降 TCP）、浏览器 STUN REJECT、DNS 走 DoH#AI 组（家宽挂了 DNS 一起 fail-closed） |
| 监控 | 每 3 分钟：家宽口出口 IP ≠ 日常出口 IP、AI 探测不能经日常口走通、组结构未被改、锁未被动、直连路由在 |
| 告警 | 只走本机：状态切换弹通知 + 模态框，持续故障每 30 分钟再提醒，抖动先观察一轮 |
| 悬浮窗 | 前台 App 的连接走了哪里：家宽 / 日常 / 直连 / 混合（混合时逐条标注） |
| 锁 | `--pin` 给 Merge / Script / 订阅副本打 `uchg` 不可变标记并记 sha256；改了或标记丢了立刻告警 |
| 改出口 | 点一下悬浮窗，或用 `route.py`，把某个 App / 域名改成家宽、直连或代理，记住并立刻生效 |

## 快速开始

```bash
git clone https://github.com/hellojx852872210-cmyk/clash-ai-homebb && cd clash-ai-homebb
python3 start.py                            # 检测环境 → 引导添加家宽/订阅 → 生成 → 安装 → 验证 → 上锁 → 装 launchd
```

`start.py` 先检测系统、Python、curl、Clash Verge 是否安装、内核是否在跑、TUN/系统代理、端口、是否已装过、是否上锁、launchd 等，打印一份报告，
再按缺什么补什么一步步带你做，每步可跳过；`python3 start.py --check` 只检测不改动，提问题时把报告贴出来。手动等价步骤：

```bash
python3 wizard.py                            # 交互式引导：添加家宽节点/订阅、日常订阅，生成配置
cat generated/INSTALL.md                    # 按步骤把 Merge/Script/providers 放进 Clash Verge
cp generated/config.toml .                  # 监控用的配置
python3 watch.py                            # 看一轮结果，应为「正常」
python3 watch.py --pin                      # 上锁
./install.sh                                # 装 launchd：监控 + 悬浮窗
```

引导里粘贴节点就行，支持 `socks5://`、`http://`、`vless://`、`trojan://`、`ss://`、`vmess://`、`hysteria2://` 分享链接，
住宅代理常见的 `host:port:user:pass` 简写，以及 JSON 节点。之后随时增删，不用进菜单：

```bash
python3 wizard.py add-home  'socks5://user:pass@203.0.113.5:1080#家宽A'
python3 wizard.py add-home  '203.0.113.5:1080:user:pass'
python3 wizard.py add-home-sub  https://home.example/sub?type=clash
python3 wizard.py add-daily 机场A https://a.example/sub?target=clash
python3 wizard.py add-daily 机场B ~/Downloads/b.yaml
python3 wizard.py remove-daily 机场A
python3 wizard.py list
python3 wizard.py generate            # 只写到 generated/；加 --install --yes 才装进 Clash Verge
```

不想用引导也可以直接改 `deadchain.toml`（示例见 `deadchain.example.toml`）再 `python3 genconfig.py deadchain.toml --out generated/`。里面可以写：

- **家宽入口**：直接写 mihomo 节点（socks5 / http / vless / trojan / ss / hysteria2 … 任意类型，多个按顺序 fallback），或给一份家宽订阅（URL / 本地文件），两者可并存；
- **日常订阅**：多份，URL 型由 mihomo 自动更新，文件型复制进去；
- **AI 域名 / 进程**、**必须直连的自家机 IP**、**DNS 是否随家宽 fail-closed**。

改了 `deadchain.toml` 就重新生成；生成器默认只写到 `generated/`，加 `--install --yes` 才会覆盖 Verge 里的 Merge / Script（原文件备份）。

## 改某个 App 的出口

默认分流之外，想把某个 App 或域名单独摆到别的出口：

**最省事的办法：点悬浮窗。** 悬浮窗显示的就是当前前台 App 的出口，点它一下会展开三个按钮：

```
Google Chrome
现在 日常 · 选新出口
[家宽] [直连] [代理]
[清除覆盖] [取消]
```

选一个即可，新连接立刻按新出口走；12 秒不操作自动收起。拖动悬浮窗仍然照旧（按住移动即可，只有「点一下不移动」才会展开）。
GUI App 按它的程序包路径匹配，所以浏览器的各种 Helper 进程会一起跟着改。

也可以用命令：

```bash
python3 route.py                      # 列出正在联网的 App 和它当前走的出口，选一个改
python3 route.py set Telegram 家宽     # 也可以直接指定
python3 route.py set github.com 直连
python3 route.py set 203.0.113.9 代理
python3 route.py list                 # 已记住的覆盖
python3 route.py remove Telegram      # 取消，恢复默认分流
python3 route.py status               # 现在谁走哪
```

改动记在 `routes.toml`，同时渲染成三个 mihomo 规则集文件（`ai-homebb-rules/user-{homebb,direct,daily}.yaml`）。
mihomo 直接重读这三个文件，所以**不用解锁 Merge.yaml、不用重载整份配置、不会断开已有连接**，命令返回后新连接就按新出口走。
重新生成配置时覆盖规则不会丢（`genconfig.py` 从 `routes.toml` 重新渲染）。

三条 `RULE-SET` 规则排在 AI 死链规则之后，所以**覆盖规则改不动 AI 的出口**：把 `claude.ai` 设成直连会被直接拒绝，
就算手改规则集文件塞进去也不会生效（实测如此）。这是有意的，死链优先。

手工写 Merge.yaml 的人第一次要装一下规则集钩子：`python3 route.py hook` 会打印要贴的片段；用 `genconfig.py` 生成配置的不用管，生成器已经带上。

## 本地验收

改了向导或生成器之后，不必碰真实配置就能完整过一遍：

```bash
./scripts/acceptance.sh          # 单元测试 → 命令行增删生成 → mihomo -t → 交互菜单逐项验证 → 最终状态
./scripts/acceptance.sh --auto   # 只跑自动部分
```

全程使用临时目录里的 `deadchain.toml` 和输出目录（通过环境变量 `CLASH_AI_HOMEBB_SPEC` 和 `--out` 隔离），
你的 `deadchain.toml`、`generated/`、Clash 配置、launchd 任务都不会被改。

## 日常操作

```bash
python3 watch.py                # 跑一轮，打印判级
python3 watch.py --json         # 完整快照
python3 watch.py --print-config # 生效配置
python3 watch.py --unpin        # 解锁 → 改配置 → 重新生成/粘贴 → python3 watch.py --pin
./uninstall.sh                  # 卸载 launchd 任务
```

状态码：`ok` / `homebb_down`（家宽断，AI 已断，未漏）/ `daily_down` / `leak_homebb_is_daily` / `leak_ai_via_daily` / `deadchain_broken` / `config_tampered` / `direct_route_missing` / `clash_dead`。

## 要求

- macOS（launchd、`chflags uchg`、`osascript`、AppKit）
- Python 3.11+（`tomllib`）；悬浮窗额外需要 `pip install pyobjc-framework-Cocoa`
- Clash Verge Rev（用它的 unix socket 控制器；其它 mihomo 客户端可在 `config.toml` 里改成 TCP controller + secret）
- `curl`

## 已知边界

- 锁是 **防误改和留证**，不是防对手：root 可以 `chflags nouchg`。监控会在 3 分钟内发现并告警。
- Clash Verge 切换配置档或重启时会重新套用它记住的 Proxies 选择，第一次要在 Verge 里手动点一次「日常出口」。
- 浏览器自带 DoH + ECH 会让 SNI 变成 `cloudflare-ech.com`，域名规则匹配不到；系统代理模式下浏览器把域名交给 Clash 所以没问题，TUN 模式下建议关掉浏览器的 Secure DNS。
- 悬浮窗按进程归属连接，终端类 App 会把子进程（如 CLI 工具）一并算进去。
- provider 文件必须放在 mihomo 的数据目录之下（安全路径限制），生成器和安装说明已按此处理。

## 目录

```
start.py                环境检测 + 全程引导（第一次运行这个）
wizard.py                增删家宽与订阅（菜单 / 命令行）
route.py                 看当前出口、把某个 App/域名改到家宽/直连/代理，并记住
routes.py                覆盖规则的数据模型与规则集渲染
genconfig.py            生成 Merge.yaml / Script.js / providers / config.toml
deadchain.example.toml  生成器输入示例
templates/Script.js.tpl Script 模板
watch.py                监控 + 锁（launchd 每 3 分钟）
float.py                悬浮窗
egress.py               连接表 → 出口摘要
config.py               config.toml 加载
config.example.toml     监控配置示例
launchd/ install.sh uninstall.sh
tests/                  pytest
```

## 参与开发

分支、PR、CI、发布与回滚流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## License

MIT
