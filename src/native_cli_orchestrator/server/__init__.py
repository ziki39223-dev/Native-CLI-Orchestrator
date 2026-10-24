"""服务装配层：MCP Server 实例、工具注册、生命周期。"""

from __future__ import annotations

from .app import SERVER_INSTRUCTIONS, SERVER_NAME, build_server, register_tools

__all__ = ["SERVER_INSTRUCTIONS", "SERVER_NAME", "build_server", "register_tools"]
