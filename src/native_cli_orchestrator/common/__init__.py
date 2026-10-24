"""通用工具：无业务的纯函数与小工具。"""

from __future__ import annotations

from .ids import new_correlation_id, new_task_id
from .retry import retry_call
from .text import now_local, preview, redact, truncate

__all__ = [
    "new_correlation_id",
    "new_task_id",
    "now_local",
    "preview",
    "redact",
    "retry_call",
    "truncate",
]
