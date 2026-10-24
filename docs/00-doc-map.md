# 文档规划（Doc Map）

每份文档只承担一件事。需求变更只改 `01`，再同步 `04`/`06`/`05`。

## 1. 文档清单

| 编号 | 文档 | 唯一职责 | 状态 |
| --- | --- | --- | --- |
| 00 | `00-doc-map.md` | 文档体系：清单、职责、顺序 | ✅ |
| 01 | `01-requirements-baseline.md` | 需求基线（定稿版）：目标、边界、约束、验收 | ✅ |
| 02 | `02-architecture.md` | 分层、依赖方向、运行模式、三条不变量 | ✅ |
| 03 | `03-directory-layout.md` | 目录分区与文件归属 | ✅ |
| 04 | `04-dev-rules.md` | 契约纪律、状态机纪律、安全口径、测试门禁 | ✅ |
| 05 | `05-dev-plan.md` | 阶段任务、DoD、风险、里程碑 | ✅ |
| 06 | `06-tool-contract.md` | 六个工具入参/出参与错误码（**已冻结 v1.0.0**） | ✅ |
| 07 | `07-decision-log.md` | 决策记录（ADR）与待定项 | ✅ |
| 09 | `09-deploy-and-packaging.md` | 打包、迁移、工作机适配、真机验证清单 | ✅ 骨架（P3 回填实测） |
| 14 | `14-cli-probe.md` | 探针使用说明：怎么摸清三家 CLI 真实命令/参数 | ✅ |
| 15 | `15-api-providers.md` | 接任意家 API：配置模板、供应商清单、预算与排错 | ✅ |
| 10 | `10-mcp-inspector-testing.md` | 联调手册：Inspector / demo 客户端 / 异常用例 | ✅ |
| 11 | `11-integration-dsh.md` | DSH 接入指引 + FreeLLMAPI 说明（只写方法，不含配置内容） | ✅ |
| 12 | `12-troubleshooting.md` | 常见故障排查 | ⬜ P5 |
| 13 | `13-changelog.md` | 版本变更 | ⬜ P5 |

## 2. 图文档（分层产出，一轮只落一层）

| 文件 | 层级 | 内容 | 状态 |
| --- | --- | --- | --- |
| `diagrams/01-layers.md` | L0 | 编排层 ↔ 调度 MCP ↔ 三组 CLI 的分层与依赖方向 | ✅ |
| `diagrams/02-account-rotation.md` | L1 | 一次 submit 的账号选择与故障切换时序 | ✅ |
| `diagrams/03-process-lifecycle.md` | L2 | 子进程拉起/监督/终止/脱敏 | ✅ 已完成 |
| `diagrams/04-task-file-protocol.md` | L3 | 文件写入时序、原子性、产物收集、CLI 侧协议 | ✅ 已完成 |
| `diagrams/05-upstream-rollback.md` | L4 | 备份→同步→校验→回滚的目录级操作 | ✅ 已完成 |
| `diagrams/06-api-runners.md` | L5 | API 组接入：分派、错误翻译、预算护栏 | ✅ 已完成 |

## 3. 编写顺序

1. P1（已完成）：00 → 01 → 02(L0) → 03 → 04 → 05 → 06 冻结 → 07 → 10 → L1 图。
2. P2（已完成）：L2/L3/L4 图 → 14 探针说明。
3. P3：09 回填实测。
4. P4：11。
5. P5：13 + 12 补全 + README 终稿。

## 4. 纪律

- 契约以 `06` + `contracts/` 为唯一真源，冲突时以冻结契约为准并回改文档。
- 每份文档头部标 `状态` 与阶段，避免"像终稿其实是草稿"。
- 不得在多份文档重复写需求，只写引用。
