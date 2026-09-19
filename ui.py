"""终端交互小工具：颜色（只在终端里）、标题、状态行、提问。wizard.py / start.py 共用，风格统一。"""
from __future__ import annotations

import os
import sys

USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if USE_COLOR else s


def bold(s: str) -> str:
    return _c("1", s)


def dim(s: str) -> str:
    return _c("2", s)


def green(s: str) -> str:
    return _c("32", s)


def red(s: str) -> str:
    return _c("31", s)


def yellow(s: str) -> str:
    return _c("33", s)


def cyan(s: str) -> str:
    return _c("36", s)


OK = green("✓")
BAD = red("✗")
NA = dim("–")


def mark(v) -> str:
    """True → ✓，False → ✗，None → –（不适用 / 未知）"""
    return OK if v is True else (BAD if v is False else NA)


def title(text: str) -> None:
    print()
    print(bold(text))
    print(dim("─" * 44))


def section(text: str) -> None:
    print()
    print(bold(f"  {text}"))


def line(m: str, label: str, detail: str = "") -> None:
    print(f"  {m} {label}" + (dim(f"  {detail}") if detail else ""))


def info(text: str) -> None:
    print(f"  {text}")


def note(text: str) -> None:
    print(dim(f"  {text}"))


def success(text: str) -> None:
    print(f"  {OK} {text}")


def fail(text: str) -> None:
    print(f"  {BAD} {text}")


class Back(Exception):
    """用户在提问处输入 b：放弃当前操作，回上一级。"""


BACK_WORDS = ("b", "back", "返回")


def ask(prompt: str, default: str = "", required: bool = False, allow_back: bool = True) -> str:
    """一行提问。空输入取默认；required 且无默认时不允许空；输入 b 抛 Back。"""
    hint = dim(f" [{default}]") if default else ""
    while True:
        s = input(f"  {prompt}{hint}{cyan(' › ')}").strip()
        if allow_back and s.lower() in BACK_WORDS:
            raise Back
        if not s:
            s = default
        if s or not required:
            return s
        print(dim("    这里不能留空" + ("，输入 b 返回" if allow_back else "")))


def confirm(prompt: str, default_yes: bool = True) -> bool:
    s = ask(f"{prompt} {dim('(y/n)')}", "y" if default_yes else "n")
    return s.lower().startswith("y")


def choose(items: list[str], what: str) -> int:
    """按编号或名字选一项，返回下标；列表为空直接 Back。"""
    if not items:
        note(f"没有可选的{what}")
        raise Back
    for i, name in enumerate(items, 1):
        print(f"    {i}. {name}")
    while True:
        s = ask(f"选哪个{what}？输入编号或名字", required=True)
        if s.isdigit() and 1 <= int(s) <= len(items):
            return int(s) - 1
        if s in items:
            return items.index(s)
        note("没有这一项，再试一次")


def pause(prompt: str = "按回车继续") -> None:
    input(f"  {prompt}{cyan(' › ')}")
