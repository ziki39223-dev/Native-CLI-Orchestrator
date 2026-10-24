"""``--init``：在项目根生成最小可用的配置骨架。

用途：用户只拿到源码目录（没带 config/）或误删了配置时，一条命令补齐。
已存在的文件**绝不覆盖**（避免把用户填好的 key 抹掉）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: 生成的最小 accounts.yml。三家 CLI 各留一个账号位，API 组留注释示例。
ACCOUNTS_TEMPLATE = """\
# 账号池配置（由 --init 生成）
#
# 三条纪律：
# 1. 本文件不放真实凭据，只写环境变量名（env:变量名）；
#    真实值用 setx 放进系统环境变量，重开终端生效。
# 2. 组内顺序 = 优先级：index 0 最先启用，额度耗尽自动下移。
# 3. 同一时刻每组只有一个账号在线（CLI 是进程不拉起，API 是不发请求）。
#
# 配好之后跑一次自检：python -m native_cli_orchestrator.entry --doctor

config_version: "2026-01-01"

groups:
  # ============================================================ Trae
  trae_group:
    # cli: ["traecli", "mcp-server"]     # 真实命令不同时取消注释并修改
    accounts:
      - label: trae-a
        env:
          TRAE_TOKEN: env:TRAE_TOKEN_1
        notes: "主用；额度耗尽后自动切下一个"
      # 不用这家时把下面改成 disabled: true，或直接删掉账号条目
      # - label: trae-b
      #   env:
      #     TRAE_TOKEN: env:TRAE_TOKEN_2

  # ============================================================ Qoder
  qoder_group:
    # cli: ["qoder", "mcp", "serve"]
    accounts:
      - label: qoder-a
        env:
          QODER_TOKEN: env:QODER_TOKEN_1

  # ============================================================ WorkBuddy
  workbuddy_group:
    # cli: ["workbuddy-mcp"]
    accounts:
      - label: wb-a
        env:
          WORKBUDDY_TOKEN: env:WORKBUDDY_TOKEN_1

  # ============================================================ API（可选）
  # 取消注释即可接入任意 OpenAI 兼容供应商（DeepSeek / Kimi / GLM / Qwen /
  # OpenRouter / one-api / 本地 Ollama 等）。
  # 详见 docs/15-api-providers.md
  #
  # deepseek_group:
  #   kind: api
  #   api:
  #     base_url: https://api.deepseek.com/v1
  #     model: deepseek-chat
  #     daily_budget: 20                 # 日预算（元），0=不限额
  #     input_price_per_mtok: 1.0        # 用于估算花费
  #     output_price_per_mtok: 2.0
  #   accounts:
  #     - label: ds-a
  #       env:
  #         OPENAI_API_KEY: env:DEEPSEEK_KEY_1
"""

#: 最小 config.yaml（只在缺失时生成；正常交付包里已有完整版）。
CONFIG_TEMPLATE = """\
# 全局配置（由 --init 生成）
mode: mock            # 先 mock 跑通，再改 real
transport: sse

accounts_path: config/accounts.yml
workspace_root: workspace
backup_root: backups
log_dir: logs

server:
  host: 127.0.0.1     # 局域网访问改 0.0.0.0，并设 auth_token
  port: 8770
  auth_token: ""
  log_level: INFO

runner:
  default_timeout_s: 900
  max_account_attempts: 3
  group_cooldown_s: 2
  switch_cooldown_s: 5
  failure_threshold: 2

upstream:
  enabled: false      # 不跟上游就用 false，免得 update_upstream 报错
  repo: ""
"""

#: 生成环境变量模板（.env.example 风格，给用户照着 setx）。
ENV_TEMPLATE = """\
@echo off
chcp 936 >nul
REM ===================================================================
REM 账号凭据设置模板（把这个文件改成 set_keys.bat 后填真实值执行）
REM
REM 说明：
REM   1. setx 写入的是「当前用户的持久环境变量」，执行后需要**重开终端**才生效
REM   2. 这里的变量名必须与 config\\accounts.yml 里的 env:变量名 一致
REM   3. 真实 key 只写在这个 bat 里，不要写进 accounts.yml
REM ===================================================================

REM ---- Trae ----
setx TRAE_TOKEN_1 "在这里填 Trae 第 1 个账号的 token"
REM setx TRAE_TOKEN_2 "第 2 个账号（备用，额度耗尽后自动切）"

REM ---- Qoder ----
setx QODER_TOKEN_1 "在这里填 Qoder 第 1 个账号的 token"

REM ---- WorkBuddy ----
setx WORKBUDDY_TOKEN_1 "在这里填 WorkBuddy 第 1 个账号的 token"

REM ---- API 供应商（可选，取消注释并填写）----
REM setx DEEPSEEK_KEY_1 "sk-xxxxxxxx"

echo.
echo 环境变量已写入。请**关闭并重开终端**，然后运行：
echo   python -m native_cli_orchestrator.entry --doctor
"""


@dataclass(slots=True)
class InitResult:
    """初始化结果：哪些文件被创建、哪些已存在被跳过。"""

    created: list[Path]
    skipped: list[Path]

    @property
    def anything_created(self) -> bool:
        return bool(self.created)


def _write_if_absent(path: Path, content: str, result: InitResult) -> None:
    """只写不存在的文件；``.bat`` 用 GBK + CRLF，其余用 UTF-8。

    bat 必须 GBK：cmd 默认代码页是 936，UTF-8 中文会乱码。
    """
    if path.exists():
        result.skipped.append(path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".bat":
        # bat 必须 GBK + CRLF：cmd 默认代码页 936，UTF-8 中文会乱码。
        normalized = "\n".join(content.splitlines()) + "\n"
        path.write_bytes(normalized.replace("\n", "\r\n").encode("gbk"))
    else:
        path.write_text(content, encoding="utf-8")
    result.created.append(path)


def init_project(project_root: Path) -> InitResult:
    """在 ``project_root`` 下补齐缺失的配置文件。

    只创建不存在的文件；已存在的一律跳过，避免覆盖用户填好的凭据。
    """
    result = InitResult(created=[], skipped=[])

    _write_if_absent(project_root / "config" / "config.yaml", CONFIG_TEMPLATE, result)
    _write_if_absent(project_root / "config" / "accounts.yml", ACCOUNTS_TEMPLATE, result)
    _write_if_absent(project_root / "scripts" / "set_keys.example.bat", ENV_TEMPLATE, result)

    for directory in ("workspace", "logs", "backups"):
        (project_root / directory).mkdir(parents=True, exist_ok=True)

    return result


def format_init_result(result: InitResult) -> str:
    lines: list[str] = []
    if result.created:
        lines.append("已创建：")
        lines.extend(f"  + {path}" for path in result.created)
    if result.skipped:
        lines.append("已存在（未改动，避免覆盖你的配置）：")
        lines.extend(f"  = {path}" for path in result.skipped)
    if result.anything_created:
        lines.extend(
            [
                "",
                "下一步：",
                "  1. 编辑 config\\accounts.yml，把 env:变量名 对上你的环境变量",
                "  2. 参考 scripts\\set_keys.example.bat 写一个 set_keys.bat 并执行（或用 setx）",
                "  3. 重开终端，然后自检：python -m native_cli_orchestrator.entry --doctor",
                "  4. 自检通过后启动：scripts\\start_sse.bat",
            ]
        )
    else:
        lines.append("配置已齐全，无需初始化。可运行 --doctor 自检。")
    return "\n".join(lines)


__all__ = [
    "ACCOUNTS_TEMPLATE",
    "CONFIG_TEMPLATE",
    "ENV_TEMPLATE",
    "InitResult",
    "format_init_result",
    "init_project",
]
