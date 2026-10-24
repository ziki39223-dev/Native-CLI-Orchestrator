"""执行器层：Mock / Cli / Api 三个实现，用 CompositeRunner 按分组 kind 分派。"""

from __future__ import annotations

from .api import ApiRunner, BudgetLedger
from .base import RunContext, Runner, RunResult
from .cli import PROFILES, CliProfile, CliRunner
from .composite import CompositeRunner
from .mock import MockRunner

__all__ = [
    "PROFILES",
    "ApiRunner",
    "BudgetLedger",
    "CliProfile",
    "CliRunner",
    "CompositeRunner",
    "MockRunner",
    "RunContext",
    "RunResult",
    "Runner",
]
