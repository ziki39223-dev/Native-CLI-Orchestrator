# 部署与打包

> 状态：P3 阶段回填实测。当前版本已列出可执行步骤，标 🔬 的项需工作机实测补数据。

## 1. 打包（开发机）

### 方式 A：文件夹（默认，推荐）

```bat
REM 在项目根执行；产物在 dist\Native-CLI-Orchestrator\
scripts\pack.bat
```

排除项：`.venv/`、`__pycache__/`、`workspace/`、`backups/`、`logs/`、`.pools.state.json`、
`config/config.local.yaml`、`config/window_rules.local.json`（历史遗留）、`*.pyc`。

### 方式 B：PyInstaller 单文件（可选，工作机无 Python 时）

```bat
pip install -r requirements-build.txt
scripts\build_exe.bat
```

产物 `dist\orchestrator.exe`，用法与 `python -m ...` 相同：

```bat
orchestrator.exe sse --config config\config.yaml
orchestrator.exe stdio --config config\config.yaml
```

🔬 待实测：打包含 `mcp` 时需 `--hidden-import` 的模块清单、打包体积、冷启动耗时。

## 2. 迁移到工作机

1. 解压到固定路径（如 `D:\Native-CLI-Orchestrator`）。
2. 判断环境：
   - 有 Python 3.12 → `pip install -r requirements.txt && pip install -e .`
   - 无 Python → 用方式 B 的 exe
3. **跑探针摸清真实命令**（强烈建议先做，见 `docs/14-cli-probe.md`）：
   ```bat
   python devtools\probe_cli.py
   ```
   报告会给出可直接粘贴的 `accounts.yml` 的 `cli` 片段，并告诉你哪家不是标准 MCP server。

4. 装三家 CLI，逐个确认能跑：
   ```bat
   traecli --version
   qoder --version
   workbuddy-mcp --version
   ```
   🔬 实际命令名与版本参数需按各家文档核对；不一致时改 `config\accounts.yml` 的 `cli` 字段。
5. 录账号凭据到**系统环境变量**（不要写进配置文件）：
   ```bat
   setx TRAE_TOKEN_1 "***"
   setx QODER_TOKEN_1 "***"
   setx WORKBUDDY_TOKEN_1 "***"
   REM 多账号依次 TRAE_TOKEN_2 / _3 ...
   ```
   录完需重开终端生效。
6. 编辑 `config\accounts.yml`：填 `env:变量名`，按优先级排好组内顺序，禁用无额度账号；
   把探针报告给出的 `cli` 片段贴进对应分组。
7. 编辑 `config\config.yaml`：`mode: real`（局域网访问再改 `server.host: 0.0.0.0` + `auth_token`）。
8. 启动：`scripts\start_sse.bat 8770 real`。

## 3. 首次真机验证清单（每次发版勾选）

- [ ] `get_pool_status`：三组都 `healthy=true`，每组只有一个 `active=true`
- [ ] 每组各提交 1 个真实任务，`state=succeeded`，`artifacts` 可打开
- [ ] 确认任务目录结构：`manifest.json` / `inbox/request.json` / `stdout.log` / `.DONE` / `artifacts/`
- [ ] `stdout.log` 里**没有**明文 token（脱敏生效）
- [ ] 任务结束后进程已退出（任务管理器查不到残留 CLI 进程）
- [ ] 额度耗尽场景：某组第 1 个账号模拟耗尽 → 自动切第 2 个 → 任务成功（`account_attempts=2`）
- [ ] 锁屏 / 黑屏下提交任务仍能完成 🔬
- [ ] 注销登录 → 进程全部终止（预期行为，写进用户须知）

## 4. 上限与硬约束（写进运维须知）

| 项 | 事实 | 后果 |
| --- | --- | --- |
| 进程会话 | 所有 CLI 属当前登录用户会话 | 锁屏可跑；**注销即全终止** |
| 同组并行 | 不支持 | 第二个任务排队，不是失败 |
| 凭据来源 | 只从环境变量 | 换账号 = 换环境变量 + `reload_config` |
| 上游更新 | 可选能力 | 上游改参数会让调用报错，必须先 `--check` |

## 5. 回滚

| 场景 | 操作 |
| --- | --- |
| 上游更新把环境弄坏 | `backups\upstream-<时间戳>\vendor` 拷回项目根 |
| 账号池状态错乱 | 删 `workspace\.pools.state.json`，重启服务（账号全部回到初始顺序） |
| 代码回退 | 用上一版压缩包覆盖（`config/` 与 `workspace/` 单独保留） |

## 6. 交付包内容检查

```bat
REM 不应出现以下内容
dir /s /b dist\Native-CLI-Orchestrator | findstr /i "workspace\\ backups\\ logs\\ .venv __pycache__ .pools.state.json"
REM 无输出 = 干净
```

## 7. 待回填（工作机实测）

- [ ] 三家 CLI 实际可执行名与参数 —— **用 `devtools/probe_cli.py` 自动产出**
- [ ] 三家是否都说 JSON-RPC（决定 `long_running`）—— 探针报告的"MCP 握手"列
- [ ] 单个任务典型耗时（用于默认 `timeout_s`）
- [ ] 触发限流的实际阈值（用于冷却参数）
- [ ] 多账号额度是否互通
- [ ] PyInstaller 打包清单与体积
