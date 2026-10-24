# 分层图 L2：子进程生命周期

> 状态：P2 · 本轮只落 L2（子进程从拉起、监督到终止的完整路径）。
> 上一层 L1 见 `02-account-rotation.md`；下一层 L3（任务文件协议）在下一轮。

## L2-1 总览：一次尝试的进程生命

```
Scheduler._execute
  └─ runner.run(context)                       ← CliRunner
       ├─ workspace.write_request(...)         ← 协议：先写 inbox/request.json
       ├─ _spawn(profile, context, ws)         ← 起进程
       │    ├─ resolve_cli_path()  → 找不到 ⇒ CLI_NOT_FOUND（不换号）
       │    ├─ 组装 args: [exe, *profile.command[1:], *extra_args]
       │    ├─ 组装 env: base_env + 账号凭据 + NCO_* 任务上下文
       │    ├─ cwd = workspace（或 cwd_subdir）
       │    └─ Popen(..., CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP)
       ├─ _attach_readers(...)                 ← 2 个 daemon 线程泵 stdout/stderr
       ├─ _supervise(process, profile, ...)    ← 轮询判定
       └─ finally:
            ├─ _terminate(process)             ← taskkill /T /F（含进程树）
            ├─ join(reader threads, 1.0s)
            └─ _active.pop(task_id)
```

## L2-2 监督循环的判定优先级

```
每 0.2s 一轮，按下列顺序判定（先命中先返回）：

1. workspace.is_done()        → 成功（写 result.json）
2. workspace.is_failed()      → 失败（故障识别 → 可换号？）
3. process.poll() != None     → 进程已退出
     ├─ is_env_error(输出)?   → 抛 CLI_NOT_FOUND（环境问题，不标账号）
     ├─ exit=0 且 long_running=False → 成功（一次性 CLI 语义）
     ├─ 故障识别(输出, exit)   → 失败（quota/banned/.../crashed）
     └─ 无识别 + 非 0          → CRASHED（可换号）
4. now >= deadline            → 失败（TIMEOUT，可换号）
5. 否则 sleep 0.2 继续
```

优先级顺序的理由：
- **`.DONE` 优先于退出码**：常驻 CLI 可能刚写完标记就退出，此时退出码无意义。
- **`.FAILED` 优先于进程退出**：CLI 主动报失败比"进程挂了"信息量大。
- **环境错误优先于故障识别**：命令写错时输出里也可能出现 timeout 字样，不能错标账号。

## L2-3 进程树终止（Windows 语义）

```
python (后端服务)
  └─ traecli.bat ─ cmd.exe
       └─ python stub_cli.py / 真实 CLI 进程
            └─ （可能还有它自己拉起的子进程）

taskkill /PID <pid> /T /F
  ↑ /T = 连子进程一起杀
  ↑ /F = 强制
```

为什么必须 `/T`：`.bat` 包装会多一层 `cmd.exe`，只杀直接子进程会留下孤儿进程，
导致"任务结束了但账号还占着在线名额"，破坏单实例不变量。

## L2-4 输出脱敏路径

```
子进程 stdout/stderr
   │  readline（逐行，bufsize=1）
   ├─► tail（deque，环形缓冲，用于故障识别与失败摘要）
   └─► redact()  ──► stdout.log / stderr.log（落盘）
```

- 落盘前必过 `redact()`：Bearer token、`api_key=`、JWT、`sk-` 前缀全部替换为 `***`。
- `tail` 里保留原文（内存态），因为故障识别需要匹配原始关键词；但**不落盘**。
- 失败摘要 `fail_reason` 也过一次 `redact()`（`_finish_failure` 里）。

## L2-5 已被测试覆盖的行为

| 行为 | 测试 |
| --- | --- |
| 完整成功链路（请求→进程→产物→.DONE） | `test_stub_success_full_chain` |
| 凭据真的注入子进程 | `test_env_injected_into_child` |
| 输出脱敏后落盘 | `test_output_is_redacted_on_disk` |
| 配额/封禁识别 | `test_stub_fault_detected[quota/banned]` |
| 崩溃（无标记 + 非零退出） | `test_stub_crash_without_marker` |
| 退出 0 但无标记 ⇒ 失败 | `test_stub_exit_zero_without_marker_fails` |
| 超时 + 进程被清理 | `test_stub_timeout_kills_process` |
| 一次性 CLI 语义 | `test_one_shot_cli_succeeds_on_exit_zero` |
| 命令/参数错误 ⇒ CLI_NOT_FOUND | `test_bad_args_reports_cli_not_found` |
| shutdown 清理进程树 | `test_shutdown_clears_running_processes` |
| 成功后不留进程 | `test_no_leftover_process_after_success` |

这些用的是 `tests/stubs/stub_cli.py`：一个**真的被 Popen 拉起的独立进程**，
不是 Mock 对象，所以验证的是真实链路。

## 下一层

- L3 任务文件协议（manifest/inbox/.DONE/产物/状态文件的读写顺序与原子性）：下一轮写 `04-task-file-protocol.md`
