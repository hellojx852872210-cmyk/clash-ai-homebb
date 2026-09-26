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
| 锁 | `--pin` 给 Merge / Script 打 `uchg` 不可变标记并记 sha256，file 型订阅副本只记 sha256（Verge 服务模式要复制它）；改了或标记丢了立刻告警 |
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
规则集在 Clash 里是 `http` 型：改动的那一刻本项目在 `127.0.0.1:7919` 临时提供这三个文件，让内核现拉，拉完即关，平时不占端口。
所以**不用解锁 Merge.yaml、不用重载整份配置、不会断开已有连接**，命令返回后新连接就按新出口走。
内核重启后读自己的缓存；缓存丢了（例如 Verge 换了运行目录）监控下一轮发现条数不对会自动重推。
重新生成配置时覆盖规则不会丢（`genconfig.py` 从 `routes.toml` 重新渲染）。

三条 `RULE-SET` 规则排在 AI 死链规则之后，所以**覆盖规则改不动 AI 的出口**：把 `claude.ai` 设成直连会被直接拒绝，
就算手改规则集文件塞进去也不会生效（实测如此）。这是有意的，死链优先。

手工写 Merge.yaml 的人第一次要装一下规则集钩子：`python3 route.py hook` 会打印要贴的片段；用 `genconfig.py` 生成配置的不用管，生成器已经带上。
早期版本生成的是 `file` 型规则集，在 Verge 2.5.5 服务模式下改了不生效（内核读的是 Verge 复制过去的副本），
重新 `python3 genconfig.py deadchain.toml --install --yes` 或按 `route.py hook` 换成新片段即可。

## 桌面 App

不想记命令，就包成一个能双击的 App：

```bash
./scripts/build-app.sh          # 生成「家宽选择器.app」并装进 /Applications（--no-install 只生成到 dist/）
```

打开后一个窗口看全：当前判级、家宽 / 日常出口 IP、AI 链路、配置锁、上次检查时间。
下面两个开关分别管监控和悬浮窗；右下「一键启用」全部打开（都开着时变成「全部停用」），「立即检查」马上跑一轮。

- 开关直接操作 launchd：停用 = `bootout` + `disable`，下次登录也不会自己起来；启用 = `enable` + `bootstrap`，还没装过就按 `launchd/` 模板现装。
- **停用不改 Clash 配置，AI 仍然只走家宽**，只是没人盯、没悬浮窗了；监控停着时顶部会标明「这是停用前最后一次的结果」。
- 按 plist 里的脚本路径认任务，不按 label：早先手装、label 不同的任务也能管，不会再装出第二份（两份悬浮窗会互相杀）。
- App 里只有启动脚本和图标，代码从本项目目录跑：改代码不用重装，挪了项目目录重跑一次 `build-app.sh`。
- 命令行等价：`python3 agents.py [status|enable|disable] [watch|float]`，`python3 panel.py --summary` 打印面板内容。

## 出口守护（自动切换）

监控每 3 分钟看一次，发现问题只报警；出口守护是常驻的，盯两条链：没设覆盖的流量（最后那条 `MATCH` 规则）每 5 秒测一次，AI 组每 30 秒测一次（家宽常按流量计费，不测太勤；家宽组自己也会每 2 分钟测、A/B 自动换）。
不通先隔 1 秒复测，两次都明确不通才动手，网络抖一下不会误切；测速接口本身出错不算不通，只记日志、不动手。动手时先分析原因，再从最靠近节点的一层往上修，修一层测一次，通了就停：

| 链上的组 | 怎么修 |
|---|---|
| 自动组（url-test / fallback） | 被钉住就解开，让它立刻重测，mihomo 自己换到活的成员；整份订阅都挂了，外层 fallback 重测后换下一份 |
| 手动组 | 当前选的不通，就改选同组里能通的：先在组内换（台湾节点挂了先换另一个台湾节点），整组都不通再往上一层换，优先日常出口组 |

分析会写清楚是哪一环的问题，比如：

```
日常流量不通：当前节点 台湾节点 1 不通；TW 是手动组，固定选了 台湾节点 1，挂了不会自动切换。已处理：TW：台湾节点 1（不通）→ 台湾节点 2。已恢复
日常流量不通：当前节点 香港节点 4 不通；所在订阅 sub-a 的副本 6 天没更新。已处理：日常-自动：机场A-自动 → 机场B-自动（重测后自动换）。已恢复
家宽不通：当前节点 RESIP-A 不通。已处理：RESIP-Claude：RESIP-A → RESIP-B（重测后自动换）。已恢复
```

通知里的「原因」只写稳定的诊断（同一个故障只报一次靠它）；订阅还剩几个活节点这类随时在变的数字写进日志。

- **红线不变**：AI 组本身不动，家宽组只在 `[deadchain] homebb_members` 写明的节点之间换（组里混进别的东西、或者白名单里的名字其实是个套着别的出口的组，就不碰、报配置异常），家宽全挂时 AI 保持断开，不回落到机场；日常流量不会被切到家宽、直连或拒绝，日常修复也不碰 AI 组能走到的任何组（包括 fallback 的备用成员，不然 AI 一换就走过去了）。候选按「现在和以后可能走到哪」整条检查：手动组看当前选择，自动组看全部成员（不认识的组类型只要有成员也按组看），混了这些成员的自动组也不替它重测。
- 直连的几个检测地址和另一条链都不通时，多半是本机上行断了：照样试一轮备选，但不会切到不安全的出口；控制器连不上且 Clash Verge 没在运行时，会把 Verge 重新打开（至少隔 60 秒才再试）。
- 手动组的候选一批批并发测（每批 12 个，一轮大约 10 秒；软上限，开始了的那批会测完），没测完下一轮接着测（这次故障里测过的记着，链一通就作废），**没证明整组都不通不往上一层跳**；刚证明整组都不通，上一层同一轮至少测一批，大组不会让上一层永远轮不到；改选、重测时控制接口出错不算「这层修不好」，不往上一层跳。每次动手（改选、解固定、让自动组重测）前，先确认兜底规则没变、目标此刻还明确不通（后台测，结果只在测完 3 秒内算数），再按最新状态把要动的组和候选整个复核一遍，复核完立刻动手；被人手动改过就停手，动手后链路上游也变了（不是这次改动能解释的）也停手；常驻进程和手动 `--once` 撞上时只有一个动手（另一个如实报告「正在处理」）。
- 判断安全看 mihomo 报的实际类型、不看名字：直连 / 拒绝类节点不管叫什么都排除，类型查不到的节点也不当安全。`[deadchain] homebb_members` 没写的话，守护认不出哪些是家宽，家宽那条链只报告不动手；日常那条链照样把家宽组运行时能走到的节点都排除，但内核里连家宽组都找不到时也不自动切换。家宽节点如果不全在家宽组里，请都写进白名单。
- 同一个组动过后 60 秒内不再动它，防止来回切；但网不能断，冷却期内会往上一层换。
- mihomo 对 url-test / fallback 组重测时会顺带解开固定：`unfix_auto = false` 时，被钉住的自动组守护也不替它重测，直接往上一层找。
- 每次处理写进 `watch.log`（带「[守护]」前缀）并发系统通知；同一个原因持续不通只通知一次，恢复时再说一声；自己闪断又自己好了（守护没动手、之前也没报过不通）、这轮没下结论（另一个守护正在处理、测速接口出错、读控制接口失败）都只记日志，不弹通知；已经做了的动作照样报。一条链连续 2 分钟都没下结论，会提醒一次「守护可能没在正常工作」（比如锁文件被 root 占了）。
- `./install.sh` 会一起装上；桌面 App 里有它的开关；参数在 `config.toml` 的 `[guard]` 段。

```bash
python3 guard.py --explain   # 不改任何选择：两条链现在走哪、通不通、有没有「挂了也不会自动换」的隐患
python3 guard.py --dry-run   # 测一轮，不通就分析并打印第一步打算怎么修，不改任何选择
python3 guard.py --once      # 测一轮，不通就真的修
```

`--explain` / `--dry-run` 只测速、不改选择；测速本身会刷新 mihomo 记录的延迟，自动组可能据此自己换一次。

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
python3 agents.py               # 监控 / 悬浮窗 / 出口守护任务的状态；enable / disable 启停
python3 guard.py --explain      # 两条链现在走哪、通不通、有没有挂了也不会自动换的隐患
./uninstall.sh                  # 卸载 launchd 任务
```

状态码：`ok` / `homebb_down`（家宽断，AI 已断，未漏）/ `daily_down` / `leak_homebb_is_daily` / `leak_ai_via_daily` / `deadchain_broken` / `config_tampered` / `direct_route_missing` / `verge_service_failed` / `clash_dead`。

## 升级到 Clash Verge 2.5.5 之后

2.5.5 的服务模式先把配置里引用的 provider 文件复制进自己的 root 目录，再用副本启动内核。老用户可能碰到：

| 现象 | 原因 | 处理 |
|---|---|---|
| 开不了虚拟网卡（TUN），Verge 提示服务不可用后自动关掉 TUN；监控报 `verge_service_failed` | 旧版本给订阅副本打了 `uchg`，服务复制时 `Operation not permitted`，内核退回 sidecar | `python3 watch.py --migrate-lock`（只解开这些副本的 uchg，已记的 sha256 不变），再在 Verge 里打开虚拟网卡模式 |
| 改出口提示「覆盖规则集还是 file 型」 | 内核读的是副本，改源文件不生效 | 重新 `genconfig.py --install --yes`，或按 `route.py hook` 换成 http 型 |
| 监控报 `direct_route_missing`：自家机「不在 exclude」 | Merge 里的 `tun.route-exclude-address` 被 Verge 自己的 TUN 设置盖掉 | Verge「设置 → 虚拟网卡模式（齿轮）→ 排除自定义网段」加上这些 `IP/32` |
| 悬浮窗显示「Clash 未开」、监控报 `clash_dead` 但 Clash 明明在跑 | 控制器 socket 换了位置 | 已自动发现，不用在 `config.toml` 里写 socket；旧版本手写过的删掉即可 |

`python3 start.py --check` 会把以上几项一起检出来。

## 要求

- macOS（launchd、`chflags uchg`、`osascript`、AppKit）
- Python 3.11+（`tomllib`）；悬浮窗和桌面 App 额外需要 `pip install pyobjc-framework-Cocoa`
- Clash Verge Rev（控制器自动发现，2.5.2 及更早、2.5.5 服务模式 / sidecar 都支持；其它 mihomo 客户端可在 `config.toml` 里写 TCP controller + secret）
- `curl`

## 已知边界

- 锁是 **防误改和留证**，不是防对手：root 可以 `chflags nouchg`。监控会在 3 分钟内发现并告警。
- Clash Verge 切换配置档或重启时会重新套用它记住的 Proxies 选择，第一次要在 Verge 里手动点一次「日常出口」。
- 浏览器自带 DoH + ECH 会让 SNI 变成 `cloudflare-ech.com`，域名规则匹配不到；系统代理模式下浏览器把域名交给 Clash 所以没问题，TUN 模式下建议关掉浏览器的 Secure DNS。
- 悬浮窗按进程归属连接，终端类 App 会把子进程（如 CLI 工具）一并算进去。
- provider 文件必须放在 mihomo 的数据目录之下（安全路径限制），生成器和安装说明已按此处理。
- 用 python.org 安装包的 Python 跑的任务，归系统设置「登录项与扩展 → 允许在后台」里的「Python Software Foundation」开关管；
  它关着时开机不会自动启动（手动启用能跑，重启又没了），监控和悬浮窗的 plist 里最好用 Homebrew 的 Python。

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
guard.py                出口守护：出口不通时分析原因、自动切到能通的出口（常驻）
panel.py                桌面控制面板（scripts/build-app.sh 包成「家宽选择器.app」）
agents.py               监控 / 悬浮窗 / 出口守护 launchd 任务的查看、启用、停用
egress.py               连接表 → 出口摘要
config.py               config.toml 加载
verge.py                Clash Verge 各版本差异：控制器位置、服务模式失败日志、哪些文件不能上 uchg
config.example.toml     监控配置示例
launchd/ install.sh uninstall.sh
scripts/                build-app.sh 打包 App、make_icon.py 画图标、acceptance.sh 本地验收
tests/                  pytest
```

## 参与开发

分支、PR、CI、发布与回滚流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## License

MIT
