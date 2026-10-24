"""ApiRunner 测试：OpenAI 兼容调用、错误翻译、预算护栏、流式无关的成功路径。

不联网：用 `httpx2.MockTransport` 拦截请求，断言请求体与响应解析。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx2
import pytest

from native_cli_orchestrator.config.accounts import parse_accounts
from native_cli_orchestrator.contracts.constants import FaultKind
from native_cli_orchestrator.contracts.errors import ErrorCode, ToolError
from native_cli_orchestrator.contracts.tools import TaskManifest
from native_cli_orchestrator.runners.api import ApiRunner
from native_cli_orchestrator.runners.api_client import (
    ApiUsage,
    classify_http_error,
    estimate_cost_yuan,
    parse_chat_response,
)
from native_cli_orchestrator.runners.base import RunContext
from native_cli_orchestrator.tasks.workspace import TaskWorkspace

# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #

ACCOUNTS = {
    "groups": {
        "trae_group": {"accounts": [{"label": "trae-a"}]},
        "qoder_group": {"accounts": [{"label": "qoder-a"}]},
        "workbuddy_group": {"accounts": [{"label": "wb-a"}]},
        "ds_group": {
            "kind": "api",
            "api": {
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-chat",
                "max_output_tokens": 512,
                "temperature": 0.2,
                "daily_budget": 1.0,
                "input_price_per_mtok": 1.0,
                "output_price_per_mtok": 2.0,
            },
            "accounts": [{"label": "ds-a", "env": {"OPENAI_API_KEY": "env:DS_KEY"}}],
        },
        "free_group": {
            "kind": "api",
            "api": {"base_url": "https://free.example.com/v1", "model": "free-model"},
            "accounts": [{"label": "free-a", "env": {"API_KEY": "env:FREE_KEY"}}],
        },
    }
}


@pytest.fixture
def accounts_cfg():
    return parse_accounts(json.loads(json.dumps(ACCOUNTS)))


def _context(
    tmp_path: Path,
    *,
    group: str = "ds_group",
    api_key: str = "sk-test",
    model: str = "",
    prompt: str = "你好",
) -> RunContext:
    workspace = TaskWorkspace.create(tmp_path / "ws", "t-api-001", group)
    manifest = TaskManifest(goal="API 测试", prompt=prompt, model=model)
    workspace.write_manifest(manifest)
    workspace.write_prompt(manifest)
    spec = parse_accounts(json.loads(json.dumps(ACCOUNTS))).api_spec(group)
    return RunContext(
        task_id=workspace.task_id,
        group=group,
        manifest=manifest,
        workspace=workspace,
        account_index=0,
        account_label="ds-a",
        api=spec,
        api_key=api_key,
        timeout_s=30.0,
    )


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx2.Request]:
    """把 httpx2.AsyncClient 的传输替换成 MockTransport，返回捕获的请求列表。"""
    captured: list[httpx2.Request] = []

    def _wrap(request: httpx2.Request) -> httpx2.Response:
        captured.append(request)
        return handler(request)

    real_client = httpx2.AsyncClient

    def _factory(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = httpx2.MockTransport(_wrap)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx2, "AsyncClient", _factory)
    return captured


def _ok_response(payload: dict) -> httpx2.Response:
    return httpx2.Response(200, json=payload)


# --------------------------------------------------------------------------- #
# 1. 纯函数：错误分类
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("status", "body", "expected_code", "expected_fault", "switchable"),
    [
        (429, "insufficient quota", ErrorCode.POOL_EXHAUSTED, FaultKind.QUOTA_EXHAUSTED, True),
        (429, "rate limit exceeded", ErrorCode.TASK_FAILED, FaultKind.RATE_LIMITED, True),
        (401, "invalid api key", ErrorCode.ACCOUNT_NOT_READY, FaultKind.AUTH_INVALID, True),
        (401, "account suspended", ErrorCode.TASK_FAILED, FaultKind.BANNED, True),
        (403, "policy violation", ErrorCode.TASK_FAILED, FaultKind.BANNED, True),
        (402, "insufficient balance", ErrorCode.POOL_EXHAUSTED, FaultKind.QUOTA_EXHAUSTED, True),
        (400, "maximum context length exceeded", ErrorCode.TASK_FAILED, FaultKind.CONTEXT_TOO_LONG, False),
        (404, "model_not_found", ErrorCode.TASK_FAILED, FaultKind.MODEL_UNAVAILABLE, False),
        (408, "request timeout", ErrorCode.TASK_TIMEOUT, FaultKind.TIMEOUT, True),
        (503, "overloaded", ErrorCode.TASK_FAILED, FaultKind.CRASHED, True),
    ],
)
def test_classify_http_error(
    status: int, body: str, expected_code: ErrorCode, expected_fault: FaultKind, switchable: bool
) -> None:
    code, fault, can_switch = classify_http_error(status, body)
    assert code is expected_code
    assert fault is expected_fault
    assert can_switch is switchable


def test_context_too_long_does_not_switch_account() -> None:
    """上下文超长换 key 没用 —— 换 key 只会把好 key 一个个标废。"""
    _code, fault, switchable = classify_http_error(400, "context_length_exceeded")
    assert fault is FaultKind.CONTEXT_TOO_LONG
    assert switchable is False


def test_estimate_cost() -> None:
    usage = ApiUsage(prompt_tokens=1_000_000, completion_tokens=1_000_000)
    assert estimate_cost_yuan(usage, input_price_per_mtok=1.0, output_price_per_mtok=2.0) == 3.0
    assert estimate_cost_yuan(ApiUsage(), input_price_per_mtok=1.0, output_price_per_mtok=2.0) == 0.0


# --------------------------------------------------------------------------- #
# 2. 响应解析
# --------------------------------------------------------------------------- #


def test_parse_standard_response() -> None:
    reply = parse_chat_response(
        {
            "id": "chatcmpl-x",
            "model": "deepseek-chat",
            "choices": [{"message": {"role": "assistant", "content": "答案"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    )
    assert reply.text == "答案"
    assert reply.usage.total_tokens == 15
    assert reply.truncated is False


def test_parse_content_blocks() -> None:
    """少数实现把 content 做成块数组。"""
    reply = parse_chat_response(
        {
            "choices": [
                {
                    "message": {
                        "content": [
                            {"type": "text", "text": "第一段"},
                            {"type": "text", "text": "第二段"},
                        ]
                    },
                    "finish_reason": "stop",
                }
            ]
        }
    )
    assert reply.text == "第一段第二段"


def test_parse_truncated_flag() -> None:
    reply = parse_chat_response(
        {"choices": [{"message": {"content": "半截"}, "finish_reason": "length"}]}
    )
    assert reply.truncated is True


def test_parse_missing_choices_raises() -> None:
    from native_cli_orchestrator.runners.api_client import ApiError

    with pytest.raises(ApiError):
        parse_chat_response({"error": "oops"})


# --------------------------------------------------------------------------- #
# 3. ApiRunner 端到端（用 MockTransport）
# --------------------------------------------------------------------------- #


def test_success_writes_artifact_and_usage(
    accounts_cfg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _install_transport(
        monkeypatch,
        lambda req: _ok_response(
            {
                "model": "deepseek-chat",
                "choices": [{"message": {"content": "开题报告的要点如下…"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500},
            }
        ),
    )
    runner = ApiRunner(budget_path=tmp_path / "budget.json")
    context = _context(tmp_path)

    result = runner.run(context)

    assert result.succeeded is True
    assert result.prompt_tokens == 1000
    assert result.completion_tokens == 500
    # 1000/1e6*1.0 + 500/1e6*2.0 = 0.002
    assert result.cost_yuan == pytest.approx(0.002)
    assert "开题报告" in result.summary

    # 请求体校验
    body = json.loads(captured[0].content)
    assert body["model"] == "deepseek-chat"
    assert body["max_tokens"] == 512
    assert body["temperature"] == 0.2
    assert body["messages"][-1]["content"] == "你好"
    assert captured[0].headers["authorization"] == "Bearer sk-test"

    # 产物与结果
    assert (context.workspace.artifact_dir / "response.md").is_file()
    payload = context.workspace.read_result()
    assert payload["usage"]["total_tokens"] == 1500
    assert payload["cost_yuan"] == pytest.approx(0.002)

    # inbox 里有请求记录（便于对账），且不含 key
    request_text = (context.workspace.inbox_dir / "request.json").read_text(encoding="utf-8")
    assert "sk-test" not in request_text


def test_manifest_model_overrides_group_default(
    accounts_cfg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _install_transport(
        monkeypatch,
        lambda req: _ok_response(
            {"choices": [{"message": {"content": "x"}, "finish_reason": "stop"}]}
        ),
    )
    runner = ApiRunner(budget_path=tmp_path / "budget.json")
    runner.run(_context(tmp_path, model="deepseek-reasoner"))

    body = json.loads(captured[0].content)
    assert body["model"] == "deepseek-reasoner"


def test_quota_error_switches_account(
    accounts_cfg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_transport(
        monkeypatch, lambda req: httpx2.Response(429, json={"error": {"message": "quota exceeded"}})
    )
    runner = ApiRunner(budget_path=tmp_path / "budget.json")

    result = runner.run(_context(tmp_path))

    assert result.succeeded is False
    assert result.fault is FaultKind.QUOTA_EXHAUSTED
    assert result.retry_with_other_account is True
    assert result.reason


def test_auth_error_switches_account(
    accounts_cfg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_transport(
        monkeypatch, lambda req: httpx2.Response(401, json={"error": {"message": "invalid api key"}})
    )
    runner = ApiRunner(budget_path=tmp_path / "budget.json")

    result = runner.run(_context(tmp_path))

    assert result.succeeded is False
    assert result.fault is FaultKind.AUTH_INVALID
    assert result.retry_with_other_account is True


def test_context_too_long_does_not_retry(
    accounts_cfg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_transport(
        monkeypatch,
        lambda req: httpx2.Response(
            400, json={"error": {"message": "maximum context length is 8192 tokens"}}
        ),
    )
    runner = ApiRunner(budget_path=tmp_path / "budget.json")

    result = runner.run(_context(tmp_path, prompt="很长的内容" * 10))

    assert result.succeeded is False
    assert result.fault is FaultKind.CONTEXT_TOO_LONG
    assert result.retry_with_other_account is False, "换 key 不能解决上下文超长"


def test_model_unavailable_does_not_retry(
    accounts_cfg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_transport(
        monkeypatch, lambda req: httpx2.Response(404, json={"error": {"code": "model_not_found"}})
    )
    runner = ApiRunner(budget_path=tmp_path / "budget.json")

    result = runner.run(_context(tmp_path, model="no-such-model"))

    assert result.succeeded is False
    assert result.fault is FaultKind.MODEL_UNAVAILABLE
    assert result.retry_with_other_account is False


def test_server_error_is_retryable(
    accounts_cfg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_transport(monkeypatch, lambda req: httpx2.Response(503, text="overloaded"))
    runner = ApiRunner(budget_path=tmp_path / "budget.json")

    result = runner.run(_context(tmp_path))

    assert result.fault is FaultKind.CRASHED
    assert result.retry_with_other_account is True


def test_timeout_maps_to_timeout_fault(
    accounts_cfg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow", request=request)

    _install_transport(monkeypatch, _raise)
    runner = ApiRunner(budget_path=tmp_path / "budget.json")

    result = runner.run(_context(tmp_path))

    assert result.succeeded is False
    assert result.fault is FaultKind.TIMEOUT
    assert result.retry_with_other_account is True


def test_missing_api_key_raises_account_not_ready(
    accounts_cfg, tmp_path: Path
) -> None:
    runner = ApiRunner(budget_path=tmp_path / "budget.json")
    context = _context(tmp_path, api_key="")

    with pytest.raises(ToolError) as excinfo:
        runner.run(context)
    assert excinfo.value.code is ErrorCode.ACCOUNT_NOT_READY


# --------------------------------------------------------------------------- #
# 4. 预算护栏
# --------------------------------------------------------------------------- #


def test_daily_budget_blocks_after_exhaustion(
    accounts_cfg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """预算 1 元；一次调用花 0.6 元，第二次应被拒（POOL_EXHAUSTED）。"""
    _install_transport(
        monkeypatch,
        lambda req: _ok_response(
            {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 300_000, "completion_tokens": 150_000},
            }
        ),
    )
    budget_file = tmp_path / "budget.json"
    runner = ApiRunner(budget_path=budget_file)

    first = runner.run(_context(tmp_path))
    assert first.succeeded is True
    assert first.cost_yuan == pytest.approx(0.6)  # 300k/1e6*1 + 150k/1e6*2 = 0.3+0.3

    second = runner.run(_context(tmp_path))
    assert second.succeeded is True  # 累计 1.2 > 1.0，但这一次开始时还有 0.4 余额

    third_ctx = _context(tmp_path)
    with pytest.raises(ToolError) as excinfo:
        runner.run(third_ctx)
    assert excinfo.value.code is ErrorCode.POOL_EXHAUSTED
    assert "预算" in excinfo.value.message
    assert excinfo.value.detail["budget"] == 1.0


def test_budget_persists_across_instances(
    accounts_cfg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重启后预算不能清零，否则护栏形同虚设。"""
    _install_transport(
        monkeypatch,
        lambda req: _ok_response(
            {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 600_000, "completion_tokens": 300_000},
            }
        ),
    )
    budget_file = tmp_path / "budget.json"
    ApiRunner(budget_path=budget_file).run(_context(tmp_path))

    fresh = ApiRunner(budget_path=budget_file)
    with pytest.raises(ToolError) as excinfo:
        fresh.run(_context(tmp_path))
    assert excinfo.value.code is ErrorCode.POOL_EXHAUSTED


def test_no_budget_means_unlimited(
    accounts_cfg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """daily_budget=0 → 不限额度（free_group 未设预算）。"""
    _install_transport(
        monkeypatch,
        lambda req: _ok_response(
            {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10_000_000, "completion_tokens": 10_000_000},
            }
        ),
    )
    runner = ApiRunner(budget_path=tmp_path / "budget.json")
    for _ in range(3):
        assert runner.run(_context(tmp_path, group="free_group", api_key="k")).succeeded is True


def test_budget_summary_shape(
    accounts_cfg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_transport(
        monkeypatch,
        lambda req: _ok_response(
            {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
            }
        ),
    )
    runner = ApiRunner(budget_path=tmp_path / "budget.json")
    runner.run(_context(tmp_path))

    summary = runner.budget_summary("ds_group", 1.0)
    assert summary["calls_today"] == 1
    assert summary["cost_today"] == pytest.approx(0.002)
    assert summary["daily_budget"] == 1.0
    assert 0 < summary["budget_remaining"] < 1.0
