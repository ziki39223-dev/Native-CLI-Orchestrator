"""ID 生成：任务 ID 与调用关联 ID。"""

from __future__ import annotations

import itertools
import secrets
import time

_counter = itertools.count(1)


def new_correlation_id(prefix: str = "c") -> str:
    """单次工具调用的关联 ID。"""
    epoch = int(time.time()) & 0xFFFFFFFF
    return f"{prefix}-{epoch:08x}-{secrets.token_hex(2)}-{next(_counter):06d}"


def new_task_id(group: str, prefix: str = "t") -> str:
    """任务 ID：``t-<group简写>-<时间戳>-<随机>``，可直接当目录名。

    只用小写字母、数字和连字符，保证跨平台文件名安全。
    """
    short = group.split("_", 1)[0].lower()
    stamp = time.strftime("%Y%m%d%H%M%S")
    return f"{prefix}-{short}-{stamp}-{secrets.token_hex(3)}"
