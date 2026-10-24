"""账号池层：分组状态机 + 单实例激活不变量。"""

from __future__ import annotations

from .manager import AccountSlot, GroupPool, PoolManager

__all__ = ["AccountSlot", "GroupPool", "PoolManager"]
