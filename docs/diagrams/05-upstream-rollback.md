# 分层图 L4：上游更新与回滚

> 状态：P2 · 本轮落 L4（最后一层）。上一层 L3 见 `04-task-file-protocol.md`。

## L4-1 目录视角：谁会动谁

```
project/
 ├─ vendor/                      ← 唯一被更新的目录（原子替换）
 │    ├─ schemas/tools.json      ← 兼容性校验的关键文件
 │    └─ cli/…                   ← CLI 配置/参数清单
 ├─ upstream.version.json        ← 版本记录（更新成功才写）
 └─ backups/
      └─ upstream-<时间戳>/
           ├─ vendor/            ← 更新前快照（回滚源）
           ├─ upstream.version.json
           └─ backup.meta.json   ← {old_version, created_at}
```

纪律：**除 `vendor/` 与 `upstream.version.json` 外，任何文件不被上游更新触碰**。
配置（`config/*.yml`）、账号池状态、任务目录都不在同步范围。

## L4-2 更新时序

```
update(check_only / force)
  │
  ├─ 读本地版本 (current_version)
  ├─ 读上游版本 (git ls-remote / 本地目录 mtime)
  │
  ├─ check_only ────────────────► 直接返回，无副作用
  ├─ 版本相同且非 force ─────────► 返回"已是最新"，无副作用
  │
  ├─ ① _backup()                ← 先备份，后动手（不可跳过）
  │     ├─ copytree(vendor → backups/<stamp>/vendor)
  │     └─ copy2(version.json)
  │
  ├─ ② _sync()
  │     ├─ 本地目录 → _copy_tree（逐文件比对大小+内容，只拷差异）
  │     └─ git 仓库 → clone --depth 1 到临时目录 → 拷贝 → 删除临时目录
  │
  ├─ ③ _verify()
  │     ├─ REQUIRED_FILES 是否都在（schemas/tools.json）
  │     └─ schemas/tools.json 是否是合法 JSON
  │
  ├─ 校验失败 ──► _rollback() ──► 抛 UPSTREAM_INCOMPATIBLE（detail.rolled_back）
  │
  └─ ④ 写 upstream.version.json
       └─ _prune_backups()     ← 只留最近 N 份
```

## L4-3 回滚的粒度与边界

```
_rollback(backup_path)
  ├─ 删当前 vendor/（rmtree）
  ├─ copytree(backup/vendor → vendor)
  └─ 若备份里有 version.json 则覆盖回去
```

三个已知边界（文档化，避免误以为万能）：

| 边界 | 说明 |
| --- | --- |
| 只回滚 `vendor/` 与版本文件 | 如果上游更新还改了别的地方（本项目不会），回滚不覆盖 |
| 不自动重启服务 | `vendor/` 里是 schema 与 CLI 参数清单，服务运行期不会热读，需 `reload_config` 或重启才生效 |
| 磁盘故障时回滚可能失败 | 此时 `detail.rolled_back=false`，要求人工从 `backups/` 恢复；命令输出会提示路径 |

## L4-4 为什么"校验"是独立一步

上游最常见的问题不是"文件没拉到"，而是**内容坏了**：

- `schemas/tools.json` 被改成非法 JSON
- 关键文件被删除或改名
- 结构变了但仍是合法 JSON（这种情况自动校验发现不了，靠 `update_upstream --check` + 冒烟任务兜底）

所以 `_verify()` 至少保证"能被解析、关键文件在"，把最坏情况挡在写版本号之前。

## L4-5 备份轮转

```
keep_backups = 5
backups/upstream-20261022-100000/   ← 最旧，被删
backups/upstream-20261022-110000/
backups/upstream-20261022-120000/
backups/upstream-20261022-130000/
backups/upstream-20261022-140000/   ← 最新，保留
```

按目录名（含时间戳）倒序排序后删除超出部分。命名必须时间戳在前才能字典序 = 时间序。

## L4-6 已被测试覆盖

| 行为 | 测试 |
| --- | --- |
| `--check` 无副作用 | `test_check_only_does_not_touch_files` |
| 正常更新 + 版本文件 | `test_update_copies_files_and_writes_version` |
| 同版本 noop | `test_second_update_same_version_is_noop` |
| `--force` 绕过版本比较 | `test_force_update_ignores_version` |
| 先备份再改 | `test_backup_created_before_sync` |
| 内容损坏 ⇒ 回滚到原状 | `test_incompatible_upstream_rolls_back` |
| 缺关键文件 ⇒ 回滚 | `test_missing_required_file_rolls_back` |
| 未配 repo / 仓库不存在 | `test_missing_repo_config_reports_error` / `test_nonexistent_repo_reports_error` |
| 备份只留 N 份 | `test_keep_backups_prunes_old` |

## L4-7 分层图完结

四层已全部落地：

| 层 | 文件 | 关注点 |
| --- | --- | --- |
| L0 | `01-layers.md` | 谁依赖谁、并行度 |
| L1 | `02-account-rotation.md` | 账号选择与故障切换时序 |
| L2 | `03-process-lifecycle.md` | 子进程拉起/监督/终止/脱敏 |
| L3 | `04-task-file-protocol.md` | 文件写入时序与原子性 |
| L4 | `05-upstream-rollback.md` | 上游更新的备份与回滚 |
