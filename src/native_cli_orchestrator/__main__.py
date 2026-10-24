"""``python -m native_cli_orchestrator``：等同 ``entry.cli main``。"""

from __future__ import annotations

import sys

from .entry.cli import main

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
