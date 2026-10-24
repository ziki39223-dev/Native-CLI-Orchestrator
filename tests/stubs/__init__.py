"""桩 CLI 的启动包装：让 CliRunner 能像调真实 CLI 一样调用它。

难点：`CliRunner` 用 `shutil.which(command[0])` 解析可执行文件。桩需要一个
"看起来像可执行文件"的入口。做法是在临时目录里生成一个 `traecli.bat`（Windows）
或 sh 脚本，内容就是 `python stub_cli.py`，然后把该目录塞进 PATH。

这样走的是**真实解析路径**（which → Popen → 进程树 → .DONE），不是绕过。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

STUB_SCRIPT = Path(__file__).resolve().parent / "stub_cli.py"


def install_stub_cli(bin_dir: Path, *, name: str = "traecli", extra_args: str = "") -> Path:
    """在 ``bin_dir`` 里造一个名为 ``name`` 的可执行包装脚本，返回其路径。"""
    bin_dir.mkdir(parents=True, exist_ok=True)
    python = sys.executable

    if sys.platform == "win32":
        wrapper = bin_dir / f"{name}.bat"
        content = f'@echo off\r\n"{python}" "{STUB_SCRIPT}" {extra_args}\r\n'
        wrapper.write_bytes(content.encode("utf-8"))
    else:
        wrapper = bin_dir / name
        content = f'#!/bin/sh\nexec "{python}" "{STUB_SCRIPT}" {extra_args}\n'
        wrapper.write_text(content, encoding="utf-8")
        wrapper.chmod(0o755)
    return wrapper


def prepend_path(monkeypatch_getenv: object, bin_dir: Path) -> dict[str, str]:
    """返回一个把 ``bin_dir`` 放到 PATH 最前的环境字典。"""
    assert monkeypatch_getenv is not None
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    return env


__all__ = ["STUB_SCRIPT", "install_stub_cli", "prepend_path"]
