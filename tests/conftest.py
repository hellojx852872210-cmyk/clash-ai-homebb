"""所有测试默认与本机隔离：不读项目目录的 config.toml、不自动找本机真实的 Clash 控制器、
覆盖规则的记忆和推送记录写在临时目录。这台机器上可能正跑着真实的 Clash，测试绝不能碰它。"""
import os
import tempfile
from pathlib import Path

if "CLASH_AI_HOMEBB_CONFIG" not in os.environ:
    _d = Path(tempfile.mkdtemp(prefix="clash-ai-homebb-test."))
    (_d / "isolated.toml").write_text(
        f'[clash]\nauto_discover = false\n[routes]\nfile = "{_d}/routes.toml"\ndir = "{_d}/rules"\n', encoding="utf-8")
    os.environ["CLASH_AI_HOMEBB_CONFIG"] = str(_d / "isolated.toml")
