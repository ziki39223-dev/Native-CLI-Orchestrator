"""stdio 子进程入口（DSH / Cherry Studio 原生本地 MCP 方式）。

    python -m native_cli_orchestrator.entry.stdio
"""

from __future__ import annotations

import sys

from .cli import run_stdio

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run_stdio(sys.argv[1:]))
