# 安装步骤

1. 把 `Merge.yaml` 内容粘到 Clash Verge Rev「配置 → 全局扩展配置 → Merge」，`Script.js` 粘到「Script」；
   或直接 `python3 genconfig.py deadchain.toml --install --yes` 让脚本复制（会备份原文件）。
2. 把 `ai-homebb-providers/` 整个目录放到 Verge 数据目录：`~/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev/ai-homebb-providers/`
   （mihomo 只允许 provider 文件在内核目录之下）。url 型订阅首次由 mihomo 自行下载到这里。
3. 在 Verge 里重新激活当前配置档（切换一下即可），Proxies 组选「日常出口」。
4. 把 `config.toml` 放到本项目目录，然后 `python3 watch.py --pin` 上锁，`./install.sh` 装监控和悬浮窗。
5. 验证：`curl -x http://127.0.0.1:7901 https://api.ipify.org` 应返回家宽 IP；`python3 watch.py` 应打印「正常」。
