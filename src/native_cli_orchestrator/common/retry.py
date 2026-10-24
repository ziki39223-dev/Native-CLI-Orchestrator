"""重试策略：有限次重试 + 退避。

只用于"幂等操作"（如轮询文件、探测进程），不用于提交任务——重试提交会产生重复任务。
"""

from __future__ import annotations

import time
from collections.abc import Callable


def retry_call[T](
    func: Callable[[], T],
    *,
    attempts: int = 3,
    delay_s: float = 0.2,
    backoff: float = 2.0,
    on_error: tuple[type[BaseException], ...] = (Exception,),
) -> T:
    """执行 ``func``，失败按退避重试，最终仍失败则抛最后一次异常。"""
    if attempts < 1:
        raise ValueError("attempts 必须 >= 1")

    wait = delay_s
    last_error: BaseException | None = None
    for index in range(attempts):
        try:
            return func()
        except on_error as exc:
            last_error = exc
            if index == attempts - 1:
                raise
            time.sleep(wait)
            wait *= backoff

    assert last_error is not None  # 理论不可达
    raise last_error
