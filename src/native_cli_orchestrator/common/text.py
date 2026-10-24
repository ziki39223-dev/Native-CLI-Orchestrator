"""文本处理：脱敏、摘要、截断。"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Final

#: 摘要长度上限。
PREVIEW_CHARS: Final[int] = 60

#: 需要打码的模式：常见 token / 密钥 / cookie 形态。
_SENSITIVE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd|cookie)\s*[=:]\s*\S+"),
    re.compile(r"\bsk-[A-Za-z0-9]{8,}"),
    re.compile(r"\beyJ[A-Za-z0-9._\-]{10,}"),  # JWT
)


def preview(text: str, limit: int = PREVIEW_CHARS) -> str:
    """折叠空白 + 截断，用于日志与出参摘要。"""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit] + "…"


def truncate(text: str, max_chars: int) -> tuple[str, bool]:
    """按字符数截断，返回 ``(文本, 是否截断)``。"""
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def redact(text: str) -> str:
    """把疑似凭据替换成 ``***``。

    用于：CLI 标准输出落盘、错误信息回传、日志。宁可多打码。
    """
    result = text
    for pattern in _SENSITIVE_PATTERNS:
        if pattern.groups:
            result = pattern.sub(lambda m: f"{m.group(1)}=***", result)
        else:
            result = pattern.sub("***", result)
    return result


def now_local() -> datetime:
    """带本地时区的当前时间。"""
    return datetime.now().astimezone()
