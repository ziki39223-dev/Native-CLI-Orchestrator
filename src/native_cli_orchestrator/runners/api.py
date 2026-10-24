"""API 执行器：走 OpenAI 兼容 HTTP，不拉进程。

与 ``CliRunner`` 的分工：

| | CliRunner | ApiRunner |
| --- | --- | --- |
| 传输 | 子进程 + stdio | HTTPS（chat/completions） |
| 完成信号 | `.DONE` 文件 | HTTP 响应返回即完成 |
| "账号" | 环境变量组合 | 一个 API key |
| 失败判定 | 输出关键词 | HTTP 状态码 + 错误体 |

对调度器而言两者一样：都实现 ``Runner.run()``，返回 ``RunResult``。
因此**账号轮换、冷却、预算、单实例**这些逻辑完全复用，不需要为 API 再写一套。

同时把「预算护栏」做在这里（而不是调度器里），因为只有执行器知道真实 token 用量。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from ..common.text import now_local, redact
from ..contracts.errors import ErrorCode, ToolError
from ..observability.logging import get_logger
from ..tasks.workspace import TaskWorkspace
from .api_client import (
    ApiError,
    ApiUsage,
    build_chat_body,
    call_chat,
    estimate_cost_yuan,
)
from .base import RunContext, Runner, RunResult

_log = get_logger("dao.cli")

#: 预算账本文件名（相对 workspace 根）。
BUDGET_FILE = ".api_budget.json"


@dataclass
class BudgetLedger:
    """按 (分组, 日期) 记录 API 花费。

    为什么按天：日预算是人能理解的粒度，也避免"跑了三天才发现超支"。
    为什么落盘：服务重启后预算不能清零，否则护栏形同虚设。
    """

    path: Path
    #: {group: {"date": "2026-01-01", "cost": 1.23, "calls": 5, "prompt_tokens": 0, "completion_tokens": 0}}
    _data: dict[str, dict[str, Any]] = field(default_factory=dict)

    def load(self) -> BudgetLedger:
        if self.path.is_file():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                _log.warning("预算账本损坏，重置：%s", self.path)
                raw = {}
            if isinstance(raw, dict):
                self._data = raw
        return self

    def _bucket(self, group: str) -> dict[str, Any]:
        today = date.today().isoformat()
        bucket = self._data.get(group)
        if not isinstance(bucket, dict) or bucket.get("date") != today:
            bucket = {
                "date": today,
                "cost": 0.0,
                "calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
            }
            self._data[group] = bucket
        return bucket

    def today(self, group: str) -> dict[str, Any]:
        return dict(self._bucket(group))

    def remaining(self, group: str, daily_budget: float) -> float | None:
        """剩余预算；``daily_budget<=0`` 表示不限额，返回 None。"""
        if daily_budget <= 0:
            return None
        return max(0.0, daily_budget - float(self._bucket(group)["cost"]))

    def record(
        self, group: str, *, usage: ApiUsage, cost_yuan: float
    ) -> dict[str, Any]:
        bucket = self._bucket(group)
        bucket["cost"] = round(float(bucket["cost"]) + cost_yuan, 6)
        bucket["calls"] = int(bucket["calls"]) + 1
        bucket["prompt_tokens"] = int(bucket["prompt_tokens"]) + usage.prompt_tokens
        bucket["completion_tokens"] = int(bucket["completion_tokens"]) + usage.completion_tokens
        self.save()
        return dict(bucket)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{id(self)}.tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)


class ApiRunner(Runner):
    """OpenAI 兼容 API 执行器。"""

    #: API 分组需要 key，因此必须校验凭据。
    requires_credentials = True

    def __init__(
        self,
        *,
        budget_path: Path | None = None,
        system_prompt: str = "",
    ) -> None:
        self._ledger = (
            BudgetLedger(path=budget_path).load() if budget_path is not None else None
        )
        self._system_prompt = system_prompt

    # ------------------------------------------------------------------ 入口

    def run(self, context: RunContext) -> RunResult:
        """同步外壳：调度器是同步的，这里用 asyncio.run 桥接。

        每个任务独立事件循环：任务之间没有共享的 async 资源，
        这样避免"跨线程复用 loop"这一类隐蔽问题。
        """
        return asyncio.run(self._run_async(context))

    async def _run_async(self, context: RunContext) -> RunResult:
        spec = context.api
        if spec is None:
            raise ToolError(
                ErrorCode.CONFIG_INVALID,
                f"分组 {context.group} 是 api 类型但缺少 api 配置",
                detail={"group": context.group},
            )
        if not context.api_key:
            raise ToolError(
                ErrorCode.ACCOUNT_NOT_READY,
                f"账号 {context.account_label} 没有可用的 API key",
                detail={"group": context.group, "label": context.account_label},
            )

        # 预算护栏：预算耗尽时直接拒单（POOL_EXHAUSTED，可被调度器当作"该组不可用"）。
        self._check_budget(context, spec.daily_budget)

        model = context.manifest.model or _account_model(context) or spec.model
        body = build_chat_body(
            model=model,
            prompt=context.manifest.prompt,
            system=self._system_prompt,
            max_output_tokens=spec.max_output_tokens,
            temperature=spec.temperature,
        )

        # 协议对齐：把请求体也落盘，便于上游/对账排查（**不含 key**）。
        workspace = context.workspace
        _write_request(workspace, group=context.group, label=context.account_label, body=body)

        try:
            reply = await call_chat(
                base_url=spec.base_url,
                api_key=context.api_key,
                body=body,
                timeout_s=min(spec.request_timeout_s, context.timeout_s),
                extra_headers=spec.extra_headers,
            )
        except ApiError as exc:
            return self._handle_api_error(context, spec, exc)

        cost = estimate_cost_yuan(
            reply.usage,
            input_price_per_mtok=spec.input_price_per_mtok,
            output_price_per_mtok=spec.output_price_per_mtok,
        )
        self._record_usage(context, reply.usage, cost)

        return self._finish_success(context, reply, cost)

    # ------------------------------------------------------------------ 预算

    def _check_budget(self, context: RunContext, daily_budget: float) -> None:
        if self._ledger is None or daily_budget <= 0:
            return
        remaining = self._ledger.remaining(context.group, daily_budget)
        if remaining is None or remaining <= 0:
            today = self._ledger.today(context.group)
            _log.error(
                "分组 %s 日预算已用尽：已花 %.4f 元 / 上限 %.4f 元（%d 次调用）",
                context.group,
                today["cost"],
                daily_budget,
                today["calls"],
            )
            raise ToolError(
                ErrorCode.POOL_EXHAUSTED,
                f"分组 {context.group} 今日 API 预算已用尽"
                f"（已花 {today['cost']:.4f} 元 / 上限 {daily_budget:.4f} 元），"
                f"如需继续请提高 daily_budget 或次日再试",
                detail={
                    "group": context.group,
                    "spent": today["cost"],
                    "budget": daily_budget,
                    "date": today["date"],
                },
            )

    def _record_usage(self, context: RunContext, usage: ApiUsage, cost: float) -> None:
        if self._ledger is None:
            return
        bucket = self._ledger.record(context.group, usage=usage, cost_yuan=cost)
        _log.info(
            "API 用量 group=%s account=%s prompt=%d completion=%d cost=%.6f 今日累计=%.4f（%d 次）",
            context.group,
            context.account_label,
            usage.prompt_tokens,
            usage.completion_tokens,
            cost,
            bucket["cost"],
            bucket["calls"],
        )

    def budget_summary(self, group: str, daily_budget: float) -> dict[str, Any]:
        """供 get_pool_status 展示当日花费。"""
        if self._ledger is None:
            return {}
        today = self._ledger.today(group)
        return {
            "date": today["date"],
            "cost_today": today["cost"],
            "calls_today": today["calls"],
            "prompt_tokens_today": today["prompt_tokens"],
            "completion_tokens_today": today["completion_tokens"],
            "daily_budget": daily_budget,
            "budget_remaining": self._ledger.remaining(group, daily_budget),
        }

    # ------------------------------------------------------------------ 结果

    def _handle_api_error(
        self, context: RunContext, spec: Any, exc: ApiError
    ) -> RunResult:
        """把 ApiError 翻译成 RunResult（不抛，让调度器按统一逻辑决定换不换号）。

        例外：环境级错误（配置问题）直接抛，避免被当成"账号故障"把好 key 标记掉。
        """
        switchable = bool((exc.detail or {}).get("switchable"))
        clean_reason = redact(exc.message)[:500]

        if exc.code is ErrorCode.CONFIG_INVALID:
            raise exc

        _log.warning(
            "API 任务失败 task=%s group=%s account=%s fault=%s switchable=%s",
            context.task_id,
            context.group,
            context.account_label,
            exc.fault.value if exc.fault else "-",
            switchable,
        )

        # 失败也写结果文件，便于 DSH 与人工排查。
        context.workspace.write_result(
            {
                "task_id": context.task_id,
                "group": context.group,
                "account_label": context.account_label,
                "succeeded": False,
                "fault": exc.fault.value if exc.fault else "",
                "reason": clean_reason,
                "error_code": exc.code.value,
                "finished_at": now_local().isoformat(),
                "base_url": spec.base_url,
            }
        )

        return RunResult(
            succeeded=False,
            fault=exc.fault,
            reason=clean_reason,
            retry_with_other_account=switchable,
            elapsed_s=0.0,
        )

    def _finish_success(self, context: RunContext, reply: Any, cost: float) -> RunResult:
        started_note = f"{reply.latency_s:.2f}s"
        summary = reply.text.strip()

        # 产物落盘：正文 + 原始元信息，DSH 可直接读文件。
        artifact_dir = context.workspace.artifact_dir
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "response.md").write_text(summary + "\n", encoding="utf-8")
        context.workspace.write_result(
            {
                "task_id": context.task_id,
                "group": context.group,
                "account_label": context.account_label,
                "succeeded": True,
                "summary": summary[:1000],
                "model": reply.model,
                "finish_reason": reply.finish_reason,
                "truncated": reply.truncated,
                "usage": {
                    "prompt_tokens": reply.usage.prompt_tokens,
                    "completion_tokens": reply.usage.completion_tokens,
                    "total_tokens": reply.usage.total_tokens,
                },
                "cost_yuan": round(cost, 6),
                "latency_s": round(reply.latency_s, 3),
                "finished_at": now_local().isoformat(),
            }
        )

        _log.info(
            "API 任务完成 task=%s group=%s account=%s model=%s tokens=%d/%d 耗时=%s",
            context.task_id,
            context.group,
            context.account_label,
            reply.model,
            reply.usage.prompt_tokens,
            reply.usage.completion_tokens,
            started_note,
        )

        return RunResult(
            succeeded=True,
            exit_code=0,
            summary=summary[:2000],
            elapsed_s=reply.latency_s,
            prompt_tokens=reply.usage.prompt_tokens,
            completion_tokens=reply.usage.completion_tokens,
            cost_yuan=cost,
            truncated=reply.truncated,
        )

    def shutdown(self) -> None:
        """无长连接需要清理。"""
        return


def _account_model(context: RunContext) -> str:
    """账号级模型覆盖（同 key 下可以拆多个模型）。"""
    if context.api_key and context.manifest.metadata.get("_account_model"):
        return str(context.manifest.metadata["_account_model"])
    return ""


def _write_request(
    workspace: TaskWorkspace, *, group: str, label: str, body: dict[str, Any]
) -> None:
    """把请求体写进 inbox（与 CLI 协议保持一致的目录结构，便于统一排查）。"""
    path = workspace.inbox_dir / "request.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "protocol": "openai-compatible",
        "group": group,
        "account_label": label,
        "model": body.get("model"),
        "max_tokens": body.get("max_tokens"),
        "temperature": body.get("temperature"),
        "created_at": now_local().isoformat(),
        # 正文单独存，避免与其它元信息混在一起
        "messages_path": "inbox/messages.json",
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (workspace.inbox_dir / "messages.json").write_text(
        json.dumps(body.get("messages", []), ensure_ascii=False, indent=2), encoding="utf-8"
    )


__all__ = ["BUDGET_FILE", "ApiRunner", "BudgetLedger"]
