"""故障识别：从 CLI 输出 / 退出码中判断故障类型。

识别策略：正则关键词 + 退出码兜底，**保留原始片段**（脱敏后）供人工核对。
宁可判成可切换（多换账号）也不要漏判成全组失效，但要避免把语法错误误判成配额耗尽。

维护方式：关键词表集中在 ``_PATTERNS``，随上游 CLI 文案更新而调整（不涉及契约）。
"""

from __future__ import annotations

import re
from typing import Final

from ..contracts.constants import FaultKind

#: 顺序有意义：先匹配到的优先（配额的词往往也含 rate limit）。
_PATTERNS: Final[tuple[tuple[FaultKind, tuple[str, ...]], ...]] = (
    (
        FaultKind.QUOTA_EXHAUSTED,
        (
            r"quota",
            r"insufficient[_ ]?(quota|credit|balance)",
            r"out of (credits?|quota)",
            r"额度",
            r"配额",
            r"余额不足",
            r"次数已用完",
            r"usage limit reached",
            r"exceeded your current quota",
        ),
    ),
    (
        FaultKind.BANNED,
        (
            r"\bbanned\b",
            r"account (is )?(suspended|disabled|blocked)",
            r"风控",
            r"封禁",
            r"账号异常",
            r"violat(ed|ion)",
        ),
    ),
    (
        FaultKind.AUTH_INVALID,
        (
            r"invalid[ _]?(api[ _]?key|token|credential)",
            r"\bunauthori[sz]ed\b",
            r"\b401\b",
            r"not logged in",
            r"login required",
            r"请先登录",
            r"认证失败",
            r"token (has )?expired",
        ),
    ),
    (
        FaultKind.RATE_LIMITED,
        (
            r"rate[ _]?limit",
            r"too many requests",
            r"\b429\b",
            r"请求过于频繁",
            r"稍后重试",
        ),
    ),
    (
        FaultKind.TIMEOUT,
        (
            r"timed? ?out",
            r"timeout",
            r"超时",
        ),
    ),
    (
        FaultKind.CRASHED,
        (
            r"traceback \(most recent call last\)",
            r"segmentation fault",
            r"panicked at",
        ),
    ),
)

_COMPILED: Final[tuple[tuple[FaultKind, tuple[re.Pattern[str], ...]], ...]] = tuple(
    (kind, tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns))
    for kind, patterns in _PATTERNS
)

#: 命令不存在 / 参数错误属于"环境问题"，不是账号问题，不切换账号。
ENV_ERROR_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"is not recognized as an internal or external command", re.IGNORECASE),
    re.compile(r"command not found", re.IGNORECASE),
    re.compile(r"no such file or directory", re.IGNORECASE),
    re.compile(r"unrecognized arguments?", re.IGNORECASE),
    re.compile(r"unknown (option|flag|command)", re.IGNORECASE),
)


def detect_fault(text: str, *, exit_code: int | None = None) -> FaultKind | None:
    """从输出文本与退出码判断故障类型。

    Returns:
        命中的 :class:`FaultKind`；输出为空且退出码为 0 时返回 ``None``。
    """
    haystack = text or ""
    for kind, patterns in _COMPILED:
        for pattern in patterns:
            if pattern.search(haystack):
                return kind

    if exit_code is not None and exit_code != 0:
        # 未识别到关键词但进程非零退出：归为崩溃，可换账号重试。
        return FaultKind.CRASHED
    return None


def is_env_error(text: str) -> bool:
    """判断是否是环境/命令问题（不该切账号，应报 CLI_NOT_FOUND 一类的错）。"""
    haystack = text or ""
    return any(pattern.search(haystack) for pattern in ENV_ERROR_PATTERNS)


__all__ = ["detect_fault", "is_env_error"]
