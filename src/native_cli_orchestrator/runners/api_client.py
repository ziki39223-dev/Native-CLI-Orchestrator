"""API 调用：OpenAI 兼容 Chat Completions 适配器。

一期只实现 OpenAI 兼容协议（`POST {base_url}/chat/completions`），因为它同时覆盖：

- 官方：OpenAI / DeepSeek / Moonshot(Kimi) / 智谱 GLM / 阿里 Qwen / MiniMax
- 聚合与中转：OpenRouter / one-api / new-api / FreeLLMAPI 之类的网关
- 本地：vLLM / SGLang / LM Studio / Ollama（`/v1` 兼容层）

按需求取舍：**不做 Anthropic 原生协议**（见 docs/07 ADR-016），需要时用中转站的
OpenAI 兼容端点即可。

错误映射是本文件的核心价值：把 HTTP 状态码 + 错误体翻译成项目的 ``FaultKind``，
让调度层知道"该换 key"还是"该改 prompt"（详见 docs/06 错误码表）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from ..common.text import redact
from ..contracts.constants import FaultKind
from ..contracts.errors import ErrorCode, ToolError
from ..observability.logging import get_logger

_log = get_logger("dao.cli")

#: 从 401/403 的响应体里识别"封号"的关键词。
_BANNED_HINTS = ("banned", "suspended", "disabled", "blocked", "violat", "风控", "封禁")
#: 从 400 的响应体里识别"上下文超长"的关键词。
_CONTEXT_HINTS = ("context length", "context_length_exceeded", "maximum context", "too many tokens")
#: 从 404 的响应体里识别"模型不存在"。
_MODEL_HINTS = ("model_not_found", "model not found", "does not exist", "no such model")


@dataclass(slots=True)
class ApiUsage:
    """一次 API 调用的 token 用量。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    @property
    def has_value(self) -> bool:
        return self.prompt_tokens > 0 or self.completion_tokens > 0


@dataclass(slots=True)
class ApiReply:
    """一次 API 调用的结果（成功路径）。"""

    text: str
    model: str
    usage: ApiUsage = field(default_factory=ApiUsage)
    finish_reason: str = ""
    latency_s: float = 0.0
    #: 原始响应里的 usage 之外的附加字段（如 id、created）。
    raw_meta: dict[str, Any] = field(default_factory=dict)

    @property
    def truncated(self) -> bool:
        """是否因为 max_tokens 被截断（编排侧应知道"结果不完整"）。"""
        return self.finish_reason in {"length", "max_tokens"}


class ApiError(ToolError):
    """API 调用失败，带 FaultKind 供调度层决定是否换 key。"""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        fault: FaultKind | None = None,
        detail: dict[str, Any] | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(code, message, detail=detail)
        self.fault = fault
        self.retry_after_s = retry_after_s


def _parse_retry_after(headers: dict[str, str]) -> float | None:
    """解析 Retry-After 头（秒），没有或非法返回 None。"""
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def classify_http_error(
    status: int, body: str, *, error_code_field: str = ""
) -> tuple[ErrorCode, FaultKind | None, bool]:
    """把 HTTP 错误翻译成（项目错误码, 故障类型, 是否值得换 key）。

    这是本模块最关键的逻辑：换 key 的判据不是"失败了"，而是"这个 key 不行了"。
    """
    lowered = (body or "").lower()
    code_field = (error_code_field or "").lower()

    if status == 429:
        # 429 既可能是"这个 key 的额度/频率到顶"，也可能是"全局限流"。
        if any(word in lowered for word in ("quota", "insufficient", "balance", "billing", "额度", "余额")):
            return ErrorCode.POOL_EXHAUSTED, FaultKind.QUOTA_EXHAUSTED, True
        return ErrorCode.TASK_FAILED, FaultKind.RATE_LIMITED, True

    if status == 401:
        if any(word in lowered for word in _BANNED_HINTS):
            return ErrorCode.TASK_FAILED, FaultKind.BANNED, True
        return ErrorCode.ACCOUNT_NOT_READY, FaultKind.AUTH_INVALID, True

    if status == 403:
        if any(word in lowered for word in _BANNED_HINTS):
            return ErrorCode.TASK_FAILED, FaultKind.BANNED, True
        return ErrorCode.ACCOUNT_NOT_READY, FaultKind.AUTH_INVALID, True

    if status == 402:
        # 部分中转站用 402 表示余额不足。
        return ErrorCode.POOL_EXHAUSTED, FaultKind.QUOTA_EXHAUSTED, True

    if status in {400, 422}:
        if any(word in lowered for word in _CONTEXT_HINTS) or "context" in code_field:
            # 上下文超长：换 key 无用，应缩短 prompt 或换大上下文模型。
            return ErrorCode.TASK_FAILED, FaultKind.CONTEXT_TOO_LONG, False
        return ErrorCode.TASK_FAILED, None, False

    if status == 404:
        if any(word in lowered for word in _MODEL_HINTS) or "model" in code_field:
            # 模型名不存在/无权限：改配置，不换 key。
            return ErrorCode.TASK_FAILED, FaultKind.MODEL_UNAVAILABLE, False
        return ErrorCode.TASK_FAILED, None, False

    if status in {408, 504}:
        return ErrorCode.TASK_TIMEOUT, FaultKind.TIMEOUT, True

    if status >= 500:
        # 上游服务端错误：换 key 通常也没用，但可以让调度层重试一次。
        return ErrorCode.TASK_FAILED, FaultKind.CRASHED, True

    return ErrorCode.TASK_FAILED, None, False


def _extract_error(body: str) -> tuple[str, str]:
    """从错误响应体里抽 (message, error_code 字段)。"""
    if not body:
        return "", ""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body.strip()[:500], ""
    if not isinstance(payload, dict):
        return body.strip()[:500], ""

    error = payload.get("error")
    if isinstance(error, dict):
        message = str(error.get("message") or error.get("detail") or "")
        code = str(error.get("code") or error.get("type") or "")
        return message[:800], code
    if isinstance(error, str):
        return error[:800], ""
    return json.dumps(payload, ensure_ascii=False)[:500], ""


def parse_chat_response(payload: dict[str, Any], *, model_hint: str = "") -> ApiReply:
    """解析 OpenAI 兼容的 chat/completions 响应。"""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ApiError(
            ErrorCode.TASK_FAILED,
            "API 响应里没有 choices",
            fault=None,
            detail={"keys": sorted(payload)[:10]},
        )

    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message")
    text = ""
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            # 少数实现把 content 做成 [{type: text, text: ...}]
            parts = [
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict) and part.get("type") in {"text", "output_text"}
            ]
            text = "".join(parts)
    if not text:
        # 兼容 completions 老式接口
        text = str(first.get("text") or "")

    usage_raw = payload.get("usage") or {}
    usage = ApiUsage(
        prompt_tokens=int(usage_raw.get("prompt_tokens") or 0),
        completion_tokens=int(usage_raw.get("completion_tokens") or 0),
        total_tokens=int(usage_raw.get("total_tokens") or 0),
    )
    if usage.total_tokens == 0:
        usage.total_tokens = usage.prompt_tokens + usage.completion_tokens

    meta: dict[str, Any] = {
        key: payload[key]
        for key in ("id", "created", "system_fingerprint")
        if key in payload
    }
    return ApiReply(
        text=text,
        model=str(payload.get("model") or model_hint),
        usage=usage,
        finish_reason=str(first.get("finish_reason") or ""),
        raw_meta=meta,
    )


def build_chat_body(
    *,
    model: str,
    prompt: str,
    system: str = "",
    max_output_tokens: int = 4096,
    temperature: float = 0.3,
) -> dict[str, Any]:
    """构造 chat/completions 请求体。

    只用最通用的字段（model / messages / max_tokens / temperature），
    因为不同家对 `max_completion_tokens`、`response_format` 等支持不一。
    """
    messages: list[dict[str, str]] = []
    if system.strip():
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return {
        "model": model,
        "messages": messages,
        "max_tokens": max_output_tokens,
        "temperature": temperature,
        "stream": False,
    }


async def call_chat(
    *,
    base_url: str,
    api_key: str,
    body: dict[str, Any],
    timeout_s: float,
    extra_headers: dict[str, str] | None = None,
) -> ApiReply:
    """发起一次 chat/completions 调用。

    用 ``httpx2``（mcp 的传递依赖）；它天然支持 async，避免为 HTTP 调用再开线程。
    """
    import httpx2

    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        **(extra_headers or {}),
    }

    started = time.monotonic()
    client = httpx2.AsyncClient(timeout=timeout_s, follow_redirects=True)
    try:
        response = await client.post(url, json=body, headers=headers)
    except httpx2.TimeoutException as exc:
        raise ApiError(
            ErrorCode.TASK_TIMEOUT,
            f"API 请求超时（{timeout_s:.0f}s）",
            fault=FaultKind.TIMEOUT,
            detail={"base_url": base_url, "reason": str(exc)[:200], "switchable": True},
        ) from exc
    except httpx2.HTTPError as exc:
        raise ApiError(
            ErrorCode.TASK_FAILED,
            f"API 请求失败：{type(exc).__name__}",
            fault=FaultKind.CRASHED,
            detail={"base_url": base_url, "reason": str(exc)[:200], "switchable": True},
        ) from exc
    finally:
        await client.aclose()

    latency = time.monotonic() - started
    text = response.text or ""

    if response.status_code >= 400:
        message, code_field = _extract_error(text)
        error_code, fault, switchable = classify_http_error(
            response.status_code, text, error_code_field=code_field
        )
        _log.warning(
            "API 失败 status=%d fault=%s switchable=%s msg=%s",
            response.status_code,
            fault.value if fault else "-",
            switchable,
            redact(message)[:200],
        )
        raise ApiError(
            error_code,
            f"HTTP {response.status_code}：{redact(message)[:300]}",
            fault=fault,
            detail={
                "status": response.status_code,
                "switchable": switchable,
                "error_code_field": code_field,
            },
            retry_after_s=_parse_retry_after(dict(response.headers)),
        )

    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise ApiError(
            ErrorCode.TASK_FAILED,
            "API 返回的不是合法 JSON",
            fault=FaultKind.CRASHED,
            detail={"body_preview": redact(text)[:300]},
        ) from exc

    if not isinstance(payload, dict):
        raise ApiError(
            ErrorCode.TASK_FAILED,
            "API 返回的 JSON 顶层不是对象",
            fault=FaultKind.CRASHED,
        )

    reply = parse_chat_response(payload, model_hint=str(body.get("model", "")))
    reply.latency_s = latency
    return reply


def estimate_cost_yuan(
    usage: ApiUsage,
    *,
    input_price_per_mtok: float,
    output_price_per_mtok: float,
) -> float:
    """按每百万 token 单价估算花费（元）。未配价格时返回 0。"""
    return (
        usage.prompt_tokens * input_price_per_mtok / 1_000_000
        + usage.completion_tokens * output_price_per_mtok / 1_000_000
    )


__all__ = [
    "ApiError",
    "ApiReply",
    "ApiUsage",
    "build_chat_body",
    "call_chat",
    "classify_http_error",
    "estimate_cost_yuan",
    "parse_chat_response",
]
