"""Real 执行器：真实拉起 CLI 子进程（MCP over stdio 或纯 CLI 调用）。

设计要点：
1. **无 GUI、无前台依赖**：``CREATE_NO_WINDOW`` + 独立进程组，锁屏可运行；
2. 每个账号一个独立子进程，任务结束即终止（不常驻），保证"同组只有一个在线"；
3. 输出流式脱敏落盘，实时做故障识别；
4. 以 ``.DONE`` / ``.FAILED`` 为完成信号，而不是依赖进程退出码（CLI 可能常驻）。

CLI 差异用 ``CliProfile`` 描述，避免在代码里写三份 if：
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from ..common.text import redact
from ..contracts.constants import FaultKind
from ..contracts.errors import ErrorCode, ToolError
from ..observability.faults import detect_fault, is_env_error
from ..observability.logging import get_logger
from ..tasks.workspace import TaskWorkspace
from .base import RunContext, Runner, RunResult, resolve_cli_path

_log = get_logger("dao.cli")

#: 轮询完成标记的间隔（秒），比配置项更细，避免漏掉瞬时标记。
_POLL_S = 0.2
#: 进程优雅终止的等待秒数。
_TERMINATE_GRACE_S = 3.0


@dataclass(frozen=True, slots=True)
class CliProfile:
    """单个 CLII 的调用约定。

    需求里三家的命令不同（``traecli mcp-server`` / ``qoder mcp serve`` / ``workbuddy-mcp``），
    但"把 prompt 送进去、等产物、拿 .DONE"是一致的，差异收敛到这里。
    """

    group: str
    command: tuple[str, ...]
    #: 把 prompt 作为位置参数传入（False 表示只通过 inbox 文件传递）。
    prompt_as_arg: bool = False
    #: 额外固定参数。
    extra_args: tuple[str, ...] = ()
    #: 是否要求进程存活直到 .DONE（True）；False 表示 CLI 一次性执行完即退出。
    long_running: bool = True
    #: 工作目录相对任务根（None 表示任务根）。
    cwd_subdir: str | None = None


#: 三家 CLI 的默认画像，工作机可在 accounts.yml 里通过 cli 字段覆盖命令。
PROFILES: dict[str, CliProfile] = {
    "trae_group": CliProfile("trae_group", ("traecli", "mcp-server")),
    "qoder_group": CliProfile("qoder_group", ("qoder", "mcp", "serve")),
    "workbuddy_group": CliProfile("workbuddy_group", ("workbuddy-mcp",)),
}


@dataclass
class CliRunner(Runner):
    """真实子进程执行器。"""

    profiles: dict[str, CliProfile] = field(default_factory=lambda: dict(PROFILES))
    #: 继承给子进程的基线环境（凭据由 RunContext.env 覆盖）。
    base_env: dict[str, str] = field(default_factory=lambda: dict(os.environ))
    #: 子进程输出保留行数。
    output_tail_lines: int = 500
    #: 是否在结束时杀掉整个进程组（Windows 用 taskkill /T）。
    kill_tree: bool = True

    _active: dict[str, subprocess.Popen[str]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ------------------------------------------------------------------ 主流程

    def run(self, context: RunContext) -> RunResult:
        if sys.platform != "win32":
            # 三家 CLI 都是 Windows 客户端；开发机跑 mock 即可。
            raise ToolError(
                ErrorCode.DEPENDENCY_MISSING,
                "real 模式仅支持 Windows",
                detail={"platform": sys.platform},
            )

        profile = self.profiles.get(context.group)
        if profile is None:
            raise ToolError(
                ErrorCode.CONFIG_INVALID,
                f"分组 {context.group} 缺少 CLI 画像",
                detail={"group": context.group},
            )

        started = time.monotonic()
        workspace = context.workspace
        workspace.write_request(
            context.manifest,
            account_label=context.account_label,
            attempt=context.attempt,
        )

        process = self._spawn(profile, context, workspace)
        tail: deque[str] = deque(maxlen=max(50, self.output_tail_lines))
        readers = self._attach_readers(process, workspace, tail)

        try:
            return self._supervise(process, profile, context, workspace, tail, started)
        finally:
            self._terminate(process, profile, context)
            for thread in readers:
                thread.join(timeout=1.0)
            with self._lock:
                self._active.pop(context.task_id, None)

    # ------------------------------------------------------------------ 启动

    def _spawn(
        self, profile: CliProfile, context: RunContext, workspace: TaskWorkspace
    ) -> subprocess.Popen[str]:
        executable = resolve_cli_path(list(profile.command))
        if executable is None:
            # 环境问题，不是账号问题：不切账号，直接报错给上层。
            raise ToolError(
                ErrorCode.CLI_NOT_FOUND,
                f"找不到 CLI 可执行文件：{profile.command[0]}",
                detail={"group": context.group, "command": list(profile.command)},
            )

        args: list[str] = [str(executable), *profile.command[1:], *profile.extra_args]
        if profile.prompt_as_arg:
            args.append(workspace.prompt_path.read_text(encoding="utf-8"))

        env = dict(self.base_env)
        env.update(context.env)
        # 把任务上下文通过环境变量传给 CLI，避免依赖命令行长度。
        env.update(
            {
                "NCO_TASK_ID": context.task_id,
                "NCO_GROUP": context.group,
                "NCO_WORKSPACE": str(workspace.root),
                "NCO_ARTIFACT_DIR": str(workspace.artifact_dir),
                "NCO_DONE_MARKER": str(workspace.done_path),
                "NCO_ACCOUNT_LABEL": context.account_label,
            }
        )

        cwd = workspace.root
        if profile.cwd_subdir:
            cwd = workspace.root / profile.cwd_subdir
            cwd.mkdir(parents=True, exist_ok=True)

        creationflags = 0
        if sys.platform == "win32":  # pragma: no branch - 平台常量
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )

        try:
            process = subprocess.Popen(
                args,
                cwd=str(cwd),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise ToolError(
                ErrorCode.PROCESS_START_FAILED,
                f"拉起 CLI 失败：{exc}",
                detail={"group": context.group, "command": profile.command[0]},
            ) from exc

        with self._lock:
            self._active[context.task_id] = process

        _log.info(
            "拉起 task=%s group=%s account=%s pid=%d cmd=%s",
            context.task_id,
            context.group,
            context.account_label,
            process.pid,
            " ".join(profile.command),
        )
        return process

    def _attach_readers(
        self,
        process: subprocess.Popen[str],
        workspace: TaskWorkspace,
        tail: deque[str],
    ) -> list[threading.Thread]:
        """把 stdout/stderr 接到脱敏落盘 + 环形缓冲。"""

        def _pump(stream: IO[str] | None, *, is_err: bool) -> None:
            if stream is None:
                return
            for raw_line in iter(stream.readline, ""):
                line = redact(raw_line)
                tail.append(line)
                workspace.append_output(stderr=line if is_err else "", stdout="" if is_err else line)

        threads = [
            threading.Thread(target=_pump, args=(process.stdout,), kwargs={"is_err": False}, daemon=True),
            threading.Thread(target=_pump, args=(process.stderr,), kwargs={"is_err": True}, daemon=True),
        ]
        for thread in threads:
            thread.start()
        return threads

    # ------------------------------------------------------------------ 监督

    def _supervise(
        self,
        process: subprocess.Popen[str],
        profile: CliProfile,
        context: RunContext,
        workspace: TaskWorkspace,
        tail: deque[str],
        started: float,
    ) -> RunResult:
        deadline = started + context.timeout_s

        while True:
            if workspace.is_done():
                return self._finish_success(context, workspace, started)

            if workspace.is_failed():
                reason = workspace.failure_reason()
                fault = detect_fault(reason, exit_code=process.returncode) or FaultKind.CRASHED
                return self._finish_failure(
                    context, started, fault, process.returncode, reason, workspace
                )

            exit_code = process.poll()
            if exit_code is not None:
                # 进程退出但没写标记：按 profile 语义与输出判定。
                joined = "".join(tail)
                if is_env_error(joined):
                    raise ToolError(
                        ErrorCode.CLI_NOT_FOUND,
                        "CLI 报告命令/参数错误，请检查 accounts.yml 的 cli 配置",
                        detail={"group": context.group, "exit_code": exit_code},
                    )
                if exit_code == 0 and not profile.long_running:
                    # 一次性 CLI：跑完即退出，退出码 0 就是成功（它不写 .DONE 属正常）。
                    _log.info(
                        "一次性 CLI 正常退出 task=%s（long_running=false，以退出码判定）",
                        context.task_id,
                    )
                    return self._finish_success(context, workspace, started)
                detected = detect_fault(joined, exit_code=exit_code)
                return self._finish_failure(
                    context,
                    started,
                    detected or FaultKind.CRASHED,
                    exit_code,
                    joined[-800:],
                    workspace,
                )

            if time.monotonic() >= deadline:
                joined = "".join(tail)
                fault = detect_fault(joined) or FaultKind.TIMEOUT
                return self._finish_failure(
                    context,
                    started,
                    fault,
                    None,
                    f"任务超时（{context.timeout_s:.0f}s 内未出现 .DONE）",
                    workspace,
                )

            time.sleep(_POLL_S)

    def _finish_success(
        self, context: RunContext, workspace: TaskWorkspace, started: float
    ) -> RunResult:
        elapsed = time.monotonic() - started
        result_payload = workspace.read_result()
        summary = str(result_payload.get("summary") or "")
        if not summary:
            summary = _summary_from_done(workspace)
        workspace.write_result(
            {
                "task_id": context.task_id,
                "group": context.group,
                "account_label": context.account_label,
                "succeeded": True,
                "summary": summary,
                "finished_at": _iso_now(),
            }
        )
        _log.info(
            "任务完成 task=%s group=%s account=%s 用时=%.1fs",
            context.task_id,
            context.group,
            context.account_label,
            elapsed,
        )
        return RunResult(succeeded=True, exit_code=0, summary=summary, elapsed_s=elapsed)

    def _finish_failure(
        self,
        context: RunContext,
        started: float,
        fault: FaultKind,
        exit_code: int | None,
        reason: str,
        workspace: TaskWorkspace,
    ) -> RunResult:
        elapsed = time.monotonic() - started
        clean_reason = redact(reason).strip()[-500:]
        workspace.write_result(
            {
                "task_id": context.task_id,
                "group": context.group,
                "account_label": context.account_label,
                "succeeded": False,
                "fault": fault.value,
                "reason": clean_reason,
                "finished_at": _iso_now(),
            }
        )
        _log.warning(
            "任务失败 task=%s group=%s account=%s fault=%s exit=%s 用时=%.1fs",
            context.task_id,
            context.group,
            context.account_label,
            fault.value,
            exit_code,
            elapsed,
        )
        return RunResult(
            succeeded=False,
            fault=fault,
            exit_code=exit_code,
            reason=clean_reason,
            elapsed_s=elapsed,
            retry_with_other_account=fault
            in {
                FaultKind.QUOTA_EXHAUSTED,
                FaultKind.BANNED,
                FaultKind.AUTH_INVALID,
                FaultKind.RATE_LIMITED,
                FaultKind.TIMEOUT,
                FaultKind.CRASHED,
            },
        )

    # ------------------------------------------------------------------ 清理

    def _terminate(
        self, process: subprocess.Popen[str], profile: CliProfile, context: RunContext
    ) -> None:
        """终止子进程（含进程树），保证"任务结束即释放唯一激活名额"。"""
        if process.poll() is not None:
            return

        try:
            if sys.platform == "win32" and self.kill_tree:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                process.terminate()
                try:
                    process.wait(timeout=_TERMINATE_GRACE_S)
                except subprocess.TimeoutExpired:
                    process.kill()
        except OSError as exc:  # pragma: no cover - 进程可能已退出
            _log.debug("终止进程失败 pid=%d: %s", process.pid, exc)
        finally:
            _log.info(
                "释放 CLI 进程 task=%s group=%s pid=%d",
                context.task_id,
                profile.group,
                process.pid,
            )

    def shutdown(self) -> None:
        """服务退出时清理所有仍在运行的子进程。"""
        with self._lock:
            handles = list(self._active.values())
        for process in handles:
            try:
                if sys.platform == "win32" and self.kill_tree:
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        capture_output=True,
                        check=False,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                else:
                    process.send_signal(signal.SIGTERM)
            except OSError:  # pragma: no cover
                continue
        with self._lock:
            self._active.clear()


def _summary_from_done(workspace: TaskWorkspace) -> str:
    try:
        text = workspace.done_path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return text[:500]


def _iso_now() -> str:
    from ..common.text import now_local

    return now_local().isoformat()


__all__ = ["PROFILES", "CliProfile", "CliRunner", "Path"]
