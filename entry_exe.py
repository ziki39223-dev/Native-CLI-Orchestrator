"""PyInstaller 入口（打包成单文件 exe 时使用）。

不参与正常运行：正常运行时用 ``python -m native_cli_orchestrator.entry``。
"""

from __future__ import annotations

import sys

from native_cli_orchestrator.entry.cli import main

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
