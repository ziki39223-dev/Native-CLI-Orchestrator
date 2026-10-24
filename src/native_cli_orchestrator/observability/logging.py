"""结构化观测：日志、计时。

三本日志分离（需求 §2「日志区分」）：
- ``scheduler.log``：调度决策（选账号、切换、队列变化）
- ``cli.log``：每个 CLI 进程的启停与输出摘要（**已脱敏**）
- ``upstream.log``：上游更新
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Final

from .timing import Stopwatch

#: 日志文件定义：logger 名 → 文件名。
LOG_FILES: Final[dict[str, str]] = {
    "dao.scheduler": "scheduler.log",
    "dao.cli": "cli.log",
    "dao.upstream": "upstream.log",
}
_DEFAULT_FILE: Final[str] = "orchestrator.log"

_FORMAT: Final[str] = "%(asctime)s %(levelname)-5s [%(name)s] %(message)s"
_configured = False


def configure_logging(
    *,
    level: str = "INFO",
    log_dir: str | Path | None = None,
    to_stderr: bool = True,
    force: bool = False,
) -> None:
    """初始化日志。

    Args:
        level: 日志级别。
        log_dir: 非空时按 logger 前缀分文件落盘（按天滚动）。
        to_stderr: 是否输出到 stderr；stdio 传输下 **必须** 只走 stderr。
    """
    global _configured
    if _configured and not force:
        return

    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(_FORMAT)

    if to_stderr:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        root.addHandler(stream)

    if log_dir is not None:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)

        default_handler = logging.handlers.TimedRotatingFileHandler(
            directory / _DEFAULT_FILE, when="midnight", backupCount=14, encoding="utf-8"
        )
        default_handler.setFormatter(formatter)
        root.addHandler(default_handler)

        # 专用日志：只挂到各自 logger，且阻止向 root 冒泡重复写默认文件。
        for logger_name, filename in LOG_FILES.items():
            child = logging.getLogger(logger_name)
            child.setLevel(level.upper())
            child.propagate = True  # 仍进 root（stderr + 默认文件）
            handler = logging.handlers.TimedRotatingFileHandler(
                directory / filename, when="midnight", backupCount=14, encoding="utf-8"
            )
            handler.setFormatter(formatter)
            child.addHandler(handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("mcp").setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str = "dao") -> logging.Logger:
    return logging.getLogger(name)


__all__ = ["LOG_FILES", "Stopwatch", "configure_logging", "get_logger"]
