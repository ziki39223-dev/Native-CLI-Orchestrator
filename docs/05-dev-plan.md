# 开发计划

> 状态：阶段 1 已完成，等指令进入阶段 2（真机子进程调度）。

## 0. 阶段总览

| 阶段 | 名称 | 环境 | 产出 | 状态 |
| --- | --- | --- | --- | --- |
| P1 | 脚手架 & 契约定义 | 开发机 | 可跑 Mock 服务（SSE+stdio）+ 冻结契约 + 92 项测试 | ✅ 完成 |
| P2 | 真机子进程调度 | 开发机编码 / 工作机验证 | 子进程管理、故障解析、探针、三本日志 | 🟡 代码+桩验证完成，待真机确认 CLI 参数 |
| P2b | API 分组（任意 OpenAI 兼容） | 开发机 | `ApiRunner`、错误翻译、预算护栏、分组放开 | 🟡 代码+MockTransport 验证完成，待真实 key 联网确认 |
| P3 | 打包迁移、工作机适配 | 开发机 → 工作机 | 可迁移包 + 账号录入 + real 联通 | ⬜ |
| P4 | 对接 DSH 全链路 | 工作机 | 接入指引 + 三角色分工 + 闭环测试 | ⬜ |
| P5 | 稳定性、风控优化 | 双机 | 限流、告警、文档终稿 | ⬜ |

## P1 · 脚手架 & 契约定义（已完成）

- [x] 目录分区（src-layout + config/scripts/tests/devtools/docs）
- [x] `pyproject.toml` + `requirements*.txt`（运行/开发分离）
- [x] `contracts/`：六工具入参出参、15 个错误码、分组与状态枚举
- [x] `pools/`：账号池状态机 + 调度器（组锁、重试、持久化）
- [x] `tasks/`：manifest / inbox / .DONE / 产物 / 脱敏落盘
- [x] `runners/`：Runner 接口 + MockRunner（场景化故障）+ CliRunner（子进程）
- [x] `upstream/`：备份 → 同步 → 校验 → 回滚
- [x] 双传输入口（SSE / stdio）
- [x] `config.yaml` + `accounts.yml` 模板
- [x] 93 项测试（unit 69 / contract 20 / e2e 4）全绿
- [x] `ruff` + `mypy --strict` 全绿
- [x] SSE 实测闭环（六工具全通）+ stdio 实测闭环

DoD 达成情况：开发机零 CLI 依赖即可完成全部验证 ✅

## P2 · 真机子进程调度

代码已落地并用**真实子进程**验证（`tests/stubs/stub_cli.py`，16 项 e2e）：

- [x] 子进程拉起/监督/终止（`CREATE_NO_WINDOW` + 独立进程组 + `taskkill /T /F`）
- [x] 流式读取 stdout/stderr，脱敏落盘 + 环形缓冲
- [x] 故障解析（额度/封禁/鉴权/限流/超时/崩溃 + 环境类错误不换号）
- [x] `.DONE` / `.FAILED` / 进程退出三方判定
- [x] 一次性 CLI 语义（`long_running=False` 时以退出码判定）
- [x] 超时后进程树清理（不留僵尸，`_active` 表清空）
- [x] `devtools/probe_cli.py` 探针（自动摸命令/参数/是否说 JSON-RPC）
- [ ] 工作机：跑探针，把三家真实 `cli` 回填 `accounts.yml`
- [ ] 工作机：真实额度耗尽 → 自动切换实测
- [ ] 工作机：锁屏/黑屏下进程稳定性验证

DoD：三家各跑通一次真实任务；额度耗尽能自动切号；日志三本分离可读。

**开发机能验证到哪一步**（重要，避免误判"已全部完成"）：
真实子进程链路（拉起/输出/标记/超时/清理/脱敏）已全部验证；
**未验证**的是三家 CLI 自身的参数与登录态 —— 这必须在工作机用探针 + 冒烟确认。

## P2b · API 分组（已完成编码与本地验证）

- [x] `GroupName` 从三选一枚举放开为任意分组名 + 正则校验（契约 v1.1.0）
- [x] `kind: cli | api` 与 `api` 配置节（base_url/model/预算/价格/超时/额外头）
- [x] `ApiRunner`：OpenAI 兼容 `chat/completions`，只发最通用字段
- [x] 错误翻译：10 种 HTTP 场景 → FaultKind + 是否换 key
- [x] 预算护栏：按天、落盘、发请求前检查、超限 `POOL_EXHAUSTED`
- [x] `CompositeRunner` 按 kind 分派，CLI 与 API 组可共存
- [x] 契约新增 `kind` / `api_budget` / token 用量字段（向后兼容）
- [ ] 真实供应商联网验证（DeepSeek / 某中转 / 本地 Ollama 各一次）
- [ ] 按各家真实错误措辞补关键词表

DoD：任一 OpenAI 兼容供应商能跑通任务；配额耗尽自动换 key；日预算用尽后拒单且不影响次日。

## P3 · 打包迁移、工作机适配

- [ ] 打包脚本（文件夹 + 可选 PyInstaller 单文件）
- [ ] `09-deploy-and-packaging.md` 回填实测
- [ ] 工作机：账号录入流程、`accounts.yml` 填写、`mode: real` 启动
- [ ] 清空环境复验（只拿压缩包从零跑通）

DoD：工作机仅按文档操作即可跑通，无需开发机介入。

## P4 · 对接 DSH 全链路

- [ ] `11-integration-dsh.md` 定稿（SSE 地址、工具可见性、三角色分工）
- [ ] DSH 总指挥指向 FreeLLMAPI，只调本 MCP
- [ ] 三角色分工：Trae（生成）/ Qoder（评审）/ WorkBuddy（文献）
- [ ] 测试：正常跑完 / 额度耗尽切号 / 上游更新后兼容性
- [ ] 人工审批节点 + 产物归档到 md（配置在编排侧）

DoD：闭环在 DSH 内跑通一次，编排侧不感知账号切换。

## P5 · 稳定性、风控优化

- [ ] 请求间隔限流（`group_cooldown_s` / `switch_cooldown_s` 已实现，待实测调参）
- [ ] 告警：上游更新失败、账号连续故障、全组无可用账号
- [ ] `12-troubleshooting.md` + `13-changelog.md` + README 终稿

DoD：连续 3 次全链路无人工干预成功；失败场景均有结构化错误码。

## 风险与阻塞

| 风险 | 阶段 | 缓解 |
| --- | --- | --- |
| 三家 CLI 实际参数与默认画像不符 | P2 | `accounts.yml` 的 `cli` 字段可覆盖，改配置不改代码 |
| CLI 无 `.DONE` 约定（不知道何时算完成） | P2 | 已在 prompt 与 request.json 里明确要求写标记；必要时改用"进程退出 + 输出判定" |
| 多账号额度不互通 | P2 | 工作机实测记录；不互通则该分组只保留 1 个账号 |
| 免费额度限流阈值不清 | P5 | 先保守设冷却，观察日志后调参 |
| PyInstaller 打包体积/启动慢 | P3 | 默认交付文件夹，exe 为可选 |

## 里程碑

| Tag | 含义 |
| --- | --- |
| `phase1-contract-frozen` | 契约冻结，编排侧可并行开发 |
| `phase2-real-backend` | 三家真机跑通 |
| `phase3-packaged` | 可迁移交付包 |
| `v1.0.0` | 闭环验收通过 |

## 待确认项

1. P2 是否需要我补一份"三家 CLI 探针脚本"（工作机上跑一遍，输出实际命令/参数/输出样例）？
2. 是否需要 `git init` + 首次提交？
3. PyInstaller 单文件 exe 是必须项还是可选？
4. 产物归档到 md 由编排侧做，还是本项目提供一个可选写文件工具（会影响契约 §10）？
