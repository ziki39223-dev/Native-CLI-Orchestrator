"""Native-CLI-Orchestrator：三家 CLI 账号池的聚合调度 MCP 服务。

核心不变量：**同一分组同一时刻只有一个 CLI 进程在线**；跨分组可并行。
本包只包含调度器本身，不含 DSH / FreeLLMAPI 的配置内容。
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
