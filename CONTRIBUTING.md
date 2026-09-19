# 协作流程

本项目是一个跑在个人 Mac 上的工具，没有服务器，但流程照标准软件项目走。分工：**AI 写代码、跑检查、开分支、准备 PR；维护者确认效果、合并、正式发布。**

## 1. 代码与密钥

- 代码全部在仓库，`README.md` 写清怎么运行。
- 密钥、节点、订阅、本机路径不上传：`config.toml`、`deadchain.toml`、`lock.json`、`state.json`、`generated/`、`*.bak-*` 已在 `.gitignore`。
  示例文件只能放占位符（`REPLACE_WITH_...`、RFC 5737 的 `203.0.113.x` / `198.51.100.x` 地址）。
- 提交前自查：`git diff --cached | grep -nE "uuid|password|public-key|token"`，出现真实值就撤回。

## 2. 分支

- `main` 只接受 PR 合并，不直接推。
- 每个改动一个分支：`feat/<主题>`、`fix/<主题>`、`chore/<主题>`、`docs/<主题>`。
- 分支从最新 `main` 切出，改完就提 PR，不攒大改动。

## 3. Pull Request

- PR 说明三件事：改了什么、为什么、怎么验证的（贴命令和结果）。模板见 `.github/pull_request_template.md`。
- 涉及 Clash 配置生成（`genconfig.py`、`templates/`）的改动，必须附上 `mihomo -t` 通过的记录。
- 维护者在本机验证效果后合并（Squash merge，保持 `main` 历史一行一个改动）。

## 4. 自动检查（CI）

- `.github/workflows/test.yml`：每个 push / PR 在 macOS 上跑 `pytest` 和生成器冒烟（用 `examples/deadchain.sample.toml` 生成一遍）。
- `main` 开了分支保护：`pytest` 检查不通过不能合并；分支必须与 `main` 同步。
- 本地先跑：`python3 -m pytest -q`。

## 5. 发布（CD）

- 「测试环境」就是维护者自己的 Mac：合并后 `git pull`，`python3 watch.py` 跑一轮看到「正常」，悬浮窗显示正常，才算通过。
- 涉及配置生成的版本，用 `genconfig.py deadchain.toml --out generated/` 重新生成，检查 diff 后再 `--install --yes`（会自动备份原文件），然后 `watch.py --pin` 重新上锁。
- 正式发布：更新 `CHANGELOG.md`，`pyproject.toml` 里升版本号，打 tag：
  ```bash
  git tag -a v0.2.0 -m "v0.2.0" && git push origin v0.2.0
  ```
  `.github/workflows/release.yml` 会按 tag 自动建 GitHub Release 并生成说明。

## 6. 退路

- 代码回滚：`git checkout v0.1.0`（或 `git revert <merge commit>` 再走 PR）。
- 配置回滚：`genconfig.py --install` 每次都会把原 `Merge.yaml` / `Script.js` 备份成 `*.bak-<时间戳>`；改坏了 `--unpin` → 复制回去 → `--pin`。
- 状态类文件（`state.json`、`lock.json`、`watch.log`）是本机产物，不入库，不需要回滚。
- 没有数据库；订阅副本在 Clash Verge 数据目录的 `ai-homebb-providers/`，自己另存一份。

## 分工速查

| 步骤 | AI | 维护者 |
|---|---|---|
| 写代码、补测试、更新文档 | ✓ | |
| 本地跑 pytest / mihomo -t / 生成器冒烟 | ✓ | |
| 开分支、提交、开 PR、写验证记录 | ✓ | |
| 在本机确认效果 | | ✓ |
| 合并 PR | | ✓ |
| 更新 CHANGELOG、升版本、打 tag 发布 | 准备 | 执行 |
| 出问题回滚 | 给出命令 | 执行 |
