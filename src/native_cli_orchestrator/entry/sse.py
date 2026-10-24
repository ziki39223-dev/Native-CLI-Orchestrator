"""SSE 服务入口（主推）。

    python -m native_cli_orchestrator.entry.sse --port 8770
"""

from __future__ import annotations

import sys

from .cli import run_sse

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run_sse(sys.argv[1:]))
