# devtools

真机诊断辅助脚本放这里（开发机不需要）。

当前状态：预留目录。

已有能力可直接复用：
- 账号池状态：`workspace/.pools.state.json`
- 三本日志：`logs/scheduler.log`、`logs/cli.log`、`logs/upstream.log`
- 任务现场：`workspace/<task_id>/`（manifest / request / stdout / .DONE / artifacts）
- 上游备份：`backups/upstream-<时间戳>/`

P2 计划补充：
- `probe_cli.py`：在工作机探测三家 CLI 的实际命令、版本参数、输出样例（用于核对 `CliProfile`）
- `probe_done.py`：验证某 CLI 是否按约定写 `.DONE`，不写则给出适配建议
