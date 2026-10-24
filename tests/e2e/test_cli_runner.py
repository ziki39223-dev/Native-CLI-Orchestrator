"""CliRunner 真子进程集成测试（开发机可跑，无需三家真实 CLI）。

做法：用 `tests/stubs/stub_cli.py` 生成一个名为 ``traecli`` 的包装脚本塞进 PATH，
让 `CliRunner` 走**完全真实**的链路：which 解析 → Popen → CREATE_NO_WINDOW →
输出泵 → 轮询 `.DONE` → 进程树终止。

这样能验证 MockRunner 覆盖不到的部分：
- 环境变量是否真的注入到子进程
- stdout/stderr 是否真的脱敏后才落盘
- 超时后进程是否真的被杀掉（不留僵尸）
- 进程退出但没写标记时怎么判定
- 命令/参数错误是否被识别为环境类错误（不标账号故障）
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from native_cli_orchestrator.config.accounts import load_accounts
from native_cli_orchestrator.contracts.constants import FaultKind
from native_cli_orchestrator.contracts.errors import ErrorCode, ToolError
from native_cli_orchestrator.contracts.tools import TaskManifest
from native_cli_orchestrator.runners.base import RunContext
from native_cli_orchestrator.runners.cli import PROFILES, CliProfile, CliRunner
from native_cli_orchestrator.tasks.workspace import TaskWorkspace

from ..stubs import install_stub_cli

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Cl 进程树语义只在 Windows 上验证")


@pytest.fixture
def stub_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把桩 CLI 塞进 PATH，返回 bin 目录。"""
    bin_dir = tmp_path / "stubbin"
    install_stub_cli(bin_dir, name="traecli")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return bin_dir


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner(
        profiles={
            "trae_group": CliProfile("trae_group", ("traecli", "mcp-server")),
            **{k: v for k, v in PROFILES.items() if k != "trae_group"},
        },
        output_tail_lines=200,
    )


def _context(
    tmp_path: Path,
    *,
    timeout_s: float = 10.0,
    attempt: int = 1,
    env: dict[str, str] | None = None,
) -> RunContext:
    workspace = TaskWorkspace.create(tmp_path / "ws", "t-stub-001", "trae_group")
    manifest = TaskManifest(goal="桩集成测试", prompt="输出一段内容", acceptance=["有产物"])
    workspace.write_manifest(manifest)
    workspace.write_prompt(manifest)
    return RunContext(
        task_id=workspace.task_id,
        group="trae_group",
        manifest=manifest,
        workspace=workspace,
        account_index=0,
        account_label="trae-a",
        env=env or {},
        attempt=attempt,
        timeout_s=timeout_s,
    )


# --------------------------------------------------------------------------- #
# 正常路径
# --------------------------------------------------------------------------- #


def test_stub_success_full_chain(stub_env: Path, runner: CliRunner, tmp_path: Path) -> None:
    """完整成功链路：请求文件 → 子进程 → 产物 → .DONE。"""
    assert stub_env.is_dir()
    context = _context(tmp_path)

    result = runner.run(context)

    assert result.succeeded is True
    assert result.exit_code == 0
    assert result.summary
    assert context.workspace.is_done()

    # 产物落盘且可收集
    artifacts = context.workspace.collect_artifacts(expected=context.manifest.artifacts)
    paths = {item.path for item in artifacts}
    assert "artifacts/report.md" in paths
    assert "outline.md" in paths

    # 输出被真实捕获（说明输出泵工作）
    stdout = context.workspace.stdout_path.read_text(encoding="utf-8")
    assert "[stub] 启动" in stdout
    assert "[stub] 已写 .DONE" in stdout


def test_env_injected_into_child(stub_env: Path, runner: CliRunner, tmp_path: Path) -> None:
    """账号 env 必须真的传进子进程（凭据注入的关键路径）。"""
    assert stub_env.is_dir()
    context = _context(tmp_path, env={"STUB_MODE": "ok"})

    result = runner.run(context)

    assert result.succeeded is True
    stdout = context.workspace.stdout_path.read_text(encoding="utf-8")
    assert "account=trae-a" in stdout, "NCO_ACCOUNT_LABEL 应出现在子进程环境里"


def test_output_is_redacted_on_disk(stub_env: Path, runner: CliRunner, tmp_path: Path) -> None:
    """子进程打印的 token 必须脱敏后才进 stdout.log。"""
    assert stub_env.is_dir()
    context = _context(tmp_path, env={"STUB_MODE": "echo_token"})

    result = runner.run(context)

    assert result.succeeded is True
    combined = context.workspace.stdout_path.read_text(encoding="utf-8")
    assert "stub-token-abcdef1234567890" not in combined
    assert "sk-stubsecret9876543210" not in combined
    assert "***" in combined


# --------------------------------------------------------------------------- #
# 故障路径
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("mode", "expected"), [("quota", FaultKind.QUOTA_EXHAUSTED), ("banned", FaultKind.BANNED)])
def test_stub_fault_detected(
    stub_env: Path, runner: CliRunner, tmp_path: Path, mode: str, expected: FaultKind
) -> None:
    assert stub_env.is_dir()
    context = _context(tmp_path, env={"STUB_MODE": mode})

    result = runner.run(context)

    assert result.succeeded is False
    assert result.fault is expected
    assert result.retry_with_other_account is True
    assert context.workspace.is_failed()


def test_stub_crash_without_marker(stub_env: Path, runner: CliRunner, tmp_path: Path) -> None:
    """非零退出且没写标记：应识别为崩溃，允许换账号。"""
    assert stub_env.is_dir()
    context = _context(tmp_path, env={"STUB_MODE": "crash"})

    result = runner.run(context)

    assert result.succeeded is False
    assert result.fault is FaultKind.CRASHED
    assert result.exit_code == 1


def test_stub_exit_zero_without_marker_fails(stub_env: Path, runner: CliRunner, tmp_path: Path) -> None:
    """退出码 0 但没写 .DONE：不能当成功（ADR-004）。"""
    assert stub_env.is_dir()
    context = _context(tmp_path, env={"STUB_MODE": "exit_ok"})

    result = runner.run(context)

    assert result.succeeded is False, "进程退出 0 不等于任务完成"
    assert context.workspace.is_done() is False


def test_stub_timeout_kills_process(stub_env: Path, runner: CliRunner, tmp_path: Path) -> None:
    """超时：返回 TIMEOUT 故障，且进程必须已被杀掉（不留僵尸）。"""
    assert stub_env.is_dir()
    context = _context(tmp_path, timeout_s=1.0, env={"STUB_MODE": "hang"})

    started = time.monotonic()
    result = runner.run(context)
    elapsed = time.monotonic() - started

    assert result.succeeded is False
    assert result.fault is FaultKind.TIMEOUT
    assert elapsed < 12.0, f"超时后应立即返回，实际 {elapsed:.1f}s"

    # 进程表里不应再有该任务的活跃句柄
    assert not runner._active, "超时后必须清理 _active 表"


def test_stub_slow_mode_completes(stub_env: Path, runner: CliRunner, tmp_path: Path) -> None:
    """慢但未超时：应正常成功（验证轮询等待逻辑）。"""
    assert stub_env.is_dir()
    context = _context(tmp_path, timeout_s=10.0, env={"STUB_MODE": "slow", "STUB_DELAY": "0.6"})

    result = runner.run(context)

    assert result.succeeded is True


def test_bad_args_reports_cli_not_found(stub_env: Path, runner: CliRunner, tmp_path: Path) -> None:
    """命令/参数错误属环境问题：抛 CLI_NOT_FOUND，转成任务级 error_code。"""
    assert stub_env.is_dir()
    context = _context(tmp_path, env={"STUB_MODE": "bad_args"})

    with pytest.raises(ToolError) as excinfo:
        runner.run(context)

    assert excinfo.value.code is ErrorCode.CLI_NOT_FOUND


def test_missing_cli_reports_cli_not_found(runner: CliRunner, tmp_path: Path) -> None:
    """PATH 里没有该命令时立即报 CLI_NOT_FOUND（不重试、不标账号）。"""
    context = _context(tmp_path)
    with pytest.raises(ToolError) as excinfo:
        runner.run(context)
    assert excinfo.value.code is ErrorCode.CLI_NOT_FOUND
    assert "traecli" in excinfo.value.message


# --------------------------------------------------------------------------- #
# 进程清理
# --------------------------------------------------------------------------- #


def test_shutdown_clears_running_processes(stub_env: Path, runner: CliRunner, tmp_path: Path) -> None:
    """服务退出时 shutdown() 必须清掉仍在跑的进程。"""
    assert stub_env.is_dir()
    context = _context(tmp_path, timeout_s=30.0, env={"STUB_MODE": "hang"})

    import threading

    thread = threading.Thread(target=runner.run, args=(context,), daemon=True)
    thread.start()

    # 等进程起来
    for _ in range(100):
        if runner._active:
            break
        time.sleep(0.05)
    assert runner._active, "子进程应已拉起"

    runner.shutdown()
    assert not runner._active

    thread.join(timeout=15.0)
    assert not thread.is_alive(), "shutdown 后任务线程应能退出"


def test_no_leftover_process_after_success(stub_env: Path, runner: CliRunner, tmp_path: Path) -> None:
    """成功后必须释放 _active，避免"同组多个进程在线"。"""
    assert stub_env.is_dir()
    context = _context(tmp_path)

    runner.run(context)

    assert not runner._active


def test_accounts_config_command_override(tmp_path: Path, stub_env: Path) -> None:
    """accounts.yml 的 cli 字段能覆盖默认命令（三家命令差异由配置驱动）。"""
    assert stub_env.is_dir()
    path = tmp_path / "accounts.yml"
    path.write_text(
        """
groups:
  trae_group:
    cli: ["traecli", "mcp-server"]
    accounts:
      - label: trae-a
  qoder_group:
    accounts: [{label: qoder-a}]
  workbuddy_group:
    accounts: [{label: wb-a}]
""",
        encoding="utf-8",
    )
    accounts = load_accounts(path)
    assert accounts.command_for("trae_group") == ["traecli", "mcp-server"]
    # 未配置时用内置默认
    assert accounts.command_for("qoder_group") == ["qoder", "mcp", "serve"]


# --------------------------------------------------------------------------- #
# 一次性 CLI（long_running=False）
# --------------------------------------------------------------------------- #


def test_one_shot_cli_succeeds_on_exit_zero(stub_env: Path, tmp_path: Path) -> None:
    """一次型 CLI：退出码 0 即成功，不强制要求写 .DONE。

    这是"三家 CLI 未必都遵守 .DONE 约定"的兜底：把 profile.long_running 设 False 即可。
    """
    assert stub_env.is_dir()
    runner = CliRunner(
        profiles={"trae_group": CliProfile("trae_group", ("traecli",), long_running=False)}
    )
    context = _context(tmp_path, env={"STUB_MODE": "exit_ok"})

    result = runner.run(context)

    assert result.succeeded is True, "long_running=False 时应以退出码 0 判成功"
    assert result.exit_code == 0
    assert not context.workspace.is_done(), "该模式下不依赖 .DONE"


def test_long_running_cli_still_requires_marker(stub_env: Path, tmp_path: Path) -> None:
    """常驻 CLI（默认 long_running=True）退出 0 但无标记仍算失败。"""
    assert stub_env.is_dir()
    runner = CliRunner(profiles={"trae_group": CliProfile("trae_group", ("traecli",))})
    context = _context(tmp_path, env={"STUB_MODE": "exit_ok"})

    result = runner.run(context)

    assert result.succeeded is False
