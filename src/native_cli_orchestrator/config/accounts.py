"""``accounts.yml``：账号池配置。

支持两类分组，由 ``kind`` 区分：

```yaml
config_version: "2026-01-01"

groups:
  # ---- 类型一：CLI 分组（三家客户端，免费额度）----
  trae_group:
    kind: cli                       # 可省略，默认就是 cli
    cli: ["traecli", "mcp-server"]  # 可选，覆盖内置默认
    accounts:
      - label: trae-a
        env:
          TRAE_TOKEN: env:TRAE_TOKEN_1

  # ---- 类型二：API 分组（任意 OpenAI 兼容供应商，花真钱）----
  deepseek_group:
    kind: api
    api:
      protocol: openai                              # 一期只支持 openai 兼容
      base_url: https://api.deepseek.com/v1          # 任意家/中转/本地 vLLM
      model: deepseek-chat
      max_output_tokens: 4096
      temperature: 0.3
      daily_budget: 20                               # 日预算（元），0=不限额
      input_price_per_mtok: 1.0                      # 每百万输入 token 价格（元）
      output_price_per_mtok: 2.0                     # 每百万输出 token 价格（元）
    accounts:
      - label: ds-a
        env:
          DEEPSEEK_API_KEY: env:DEEPSEEK_KEY_1
```

设计约束：
- **凭据不入文件**：``env`` 的值写成 ``env:VAR_NAME``，运行时从进程环境读取；
- 组内顺序即优先级，序号 0 优先；
- 同组单实例是**运行期不变量**，由调度器保证，配置层只提供有序列表；
- API 组同样遵守单实例：同一时刻只有一个 key 在跑，其余休眠（与 CLI 组语义一致）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ..contracts.constants import (
    DEFAULT_API_BASE_URL,
    DEFAULT_API_DAILY_BUDGET,
    DEFAULT_API_MAX_OUTPUT_TOKENS,
    DEFAULT_API_MODEL,
    DEFAULT_CLI_COMMANDS,
    GROUP_NAME_PATTERN,
    ApiProtocol,
    GroupKind,
)
from ..contracts.errors import ErrorCode, ToolError

#: 环境变量引用前缀。
ENV_PREFIX = "env:"

#: API key 的环境变量名候选（用于自动挑选 Authorization 头）。
API_KEY_ENV_NAMES: tuple[str, ...] = ("API_KEY", "OPENAI_API_KEY", "TOKEN")


class AccountConfig(BaseModel):
    """单个账号的配置。

    CLI 组：``env`` 注入到子进程；
    API 组：``env`` 里的 API key 用于 ``Authorization: Bearer``。
    """

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, description="人可读标签，出现在出参与日志里")
    #: 环境变量名 → 值。值以 ``env:`` 开头时表示"从进程环境取"。
    env: dict[str, str] = Field(default_factory=dict)
    #: 该账号专用的 CLI 参数（仅 CLI 组有意义）。
    args: list[str] = Field(default_factory=list)
    #: API 组专用：覆盖分组默认 model（例如同 key 下换模型）。
    model: str = ""
    #: 配置层直接禁用该账号（不参与调度）。
    disabled: bool = False
    notes: str = ""

    @field_validator("env")
    @classmethod
    def _no_plaintext_secret(cls, value: dict[str, str]) -> dict[str, str]:
        """凭据引用必须是 ``env:NAME``，禁止把明文 token 写进配置文件。"""
        for key, raw in value.items():
            if ENV_PREFIX in raw:
                continue
            # 允许非敏感项（如 mode=fast），但名字里带敏感词时必须用 env:
            lowered = key.lower()
            if any(word in lowered for word in ("token", "key", "secret", "password", "cookie")):
                raise ValueError(f"环境变量 {key} 看起来是凭据，必须写成 env:变量名 形式")
        return value

    def resolve_env(self, environ: dict[str, str]) -> dict[str, str]:
        """把 ``env:`` 引用解析成实际值；缺失时报 ACCOUNT_NOT_READY。"""
        resolved: dict[str, str] = {}
        for key, raw in self.env.items():
            if raw.startswith(ENV_PREFIX):
                source = raw[len(ENV_PREFIX) :]
                if source not in environ or not environ[source]:
                    raise ToolError(
                        ErrorCode.ACCOUNT_NOT_READY,
                        f"账号 {self.label} 的环境变量 {source} 未设置",
                        detail={"label": self.label, "missing_env": source},
                    )
                resolved[key] = environ[source]
            else:
                resolved[key] = raw
        return resolved

    def api_key(self, environ: dict[str, str]) -> str:
        """从 ``env`` 里挑出 API key（API 组用）。

        优先精确名，其次常见别名；都找不到则把 ``env`` 里唯一一个看起来像 key 的值当 key。
        """
        resolved = self.resolve_env(environ)
        for name in API_KEY_ENV_NAMES:
            if resolved.get(name):
                return resolved[name]
        for name, value in resolved.items():
            lowered = name.lower()
            if any(word in lowered for word in ("key", "token")):
                return value
        raise ToolError(
            ErrorCode.ACCOUNT_NOT_READY,
            f"账号 {self.label} 没有可用的 API key（env 里需有 API_KEY / OPENAI_API_KEY 等）",
            detail={"label": self.label, "env_names": sorted(self.env)},
        )


class ApiSpec(BaseModel):
    """API 分组的供应商参数（OpenAI 兼容）。"""

    model_config = ConfigDict(extra="forbid")

    protocol: ApiProtocol = ApiProtocol.OPENAI
    base_url: str = Field(default=DEFAULT_API_BASE_URL, min_length=1)
    #: 分组默认模型；账号级可覆盖。
    model: str = Field(default=DEFAULT_API_MODEL, min_length=1)
    max_output_tokens: int = Field(default=DEFAULT_API_MAX_OUTPUT_TOKENS, ge=1, le=200_000)
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    #: 单次请求超时（秒）。
    request_timeout_s: float = Field(default=300.0, gt=0, le=3600)
    #: 日预算（元），0 表示不限额。超出后拒绝新任务（POOL_EXHAUSTED）。
    daily_budget: float = Field(default=DEFAULT_API_DAILY_BUDGET, ge=0.0)
    #: 计价（每百万 token，元）。0 表示未知价格 —— 只统计 token，不算钱。
    input_price_per_mtok: float = Field(default=0.0, ge=0.0)
    output_price_per_mtok: float = Field(default=0.0, ge=0.0)
    #: 额外的请求头（某些中转站要求，如 X-Title）。
    extra_headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("base_url 必须以 http:// 或 https:// 开头")
        return value.rstrip("/")

    @property
    def priced(self) -> bool:
        """是否配置了价格（决定能否估算花费）。"""
        return self.input_price_per_mtok > 0 or self.output_price_per_mtok > 0


class GroupConfig(BaseModel):
    """一个分组。

    ``kind=cli``：走子进程 + 文件协议；
    ``kind=api``：走 OpenAI 兼容 HTTP（必须给 ``api`` 节）。
    """

    model_config = ConfigDict(extra="forbid")

    kind: GroupKind = GroupKind.CLI
    #: CLI 组：覆盖默认 CLI 命令。
    cli: list[str] | None = None
    #: API 组：供应商参数。
    api: ApiSpec | None = None
    accounts: list[AccountConfig] = Field(min_length=1)
    notes: str = ""

    @model_validator(mode="after")
    def _kind_consistency(self) -> GroupConfig:
        if self.kind is GroupKind.API:
            if self.api is None:
                raise ValueError("kind=api 的分组必须提供 api 节（base_url / model 等）")
            if self.cli:
                raise ValueError("kind=api 的分组不能同时配置 cli（两者互斥）")
        else:
            if self.api is not None:
                raise ValueError("kind=cli 的分组不能配置 api 节；要接 API 请把 kind 改成 api")
        return self

    @property
    def command(self) -> list[str]:
        return list(self.cli) if self.cli else []

    @property
    def is_api(self) -> bool:
        return self.kind is GroupKind.API


class AccountsConfig(BaseModel):
    """账号池根配置。"""

    model_config = ConfigDict(extra="forbid")

    config_version: str = ""
    groups: dict[str, GroupConfig]

    @field_validator("groups")
    @classmethod
    def _validate_groups(cls, value: dict[str, GroupConfig]) -> dict[str, GroupConfig]:
        """校验分组名与必需分组。

        v1.1.0 起分组可自定义（挂任意 API 供应商），但仍要求内置三家 CLI 分组存在，
        避免"只配了 API 却以为 CLI 还能用"的误解。
        """
        import re

        if not value:
            raise ValueError("groups 不能为空")

        bad_names = [name for name in value if not re.match(GROUP_NAME_PATTERN, name)]
        if bad_names:
            raise ValueError(
                f"分组名非法：{bad_names}；须以字母开头，只含字母/数字/下划线/连字符"
            )

        missing = sorted(set(DEFAULT_CLI_COMMANDS) - set(value))
        if missing:
            raise ValueError(
                f"缺少内置 CLI 分组：{missing}。若确实不用某家，请保留该分组并给账号加 disabled: true"
            )
        return value

    def group(self, name: str) -> GroupConfig:
        """按名取分组，不存在抛 GROUP_NOT_FOUND。"""
        try:
            return self.groups[name]
        except KeyError as exc:
            raise ToolError(
                ErrorCode.GROUP_NOT_FOUND,
                f"未配置分组：{name}",
                detail={"group": name, "available": sorted(self.groups)},
            ) from exc

    def command_for(self, name: str) -> list[str]:
        """取该分组的 CLI 命令：优先配置，其次内置默认。"""
        group = self.group(name)
        if group.is_api:
            raise ToolError(
                ErrorCode.CONFIG_INVALID,
                f"分组 {name} 是 api 类型，没有 CLI 命令",
                detail={"group": name, "kind": group.kind.value},
            )
        if group.command:
            return group.command
        try:
            return list(DEFAULT_CLI_COMMANDS[name])
        except KeyError as exc:
            raise ToolError(
                ErrorCode.CONFIG_INVALID,
                f"分组 {name} 没有内置默认 CLI，请在 accounts.yml 里显式配置 cli",
                detail={"group": name},
            ) from exc

    def api_spec(self, name: str) -> ApiSpec:
        """取分组 API 参数；非 api 分组报错。"""
        group = self.group(name)
        if group.api is None:
            raise ToolError(
                ErrorCode.CONFIG_INVALID,
                f"分组 {name} 未配置 api 节",
                detail={"group": name, "kind": group.kind.value},
            )
        return group.api

    @property
    def account_count(self) -> int:
        return sum(len(group.accounts) for group in self.groups.values())

    @property
    def group_names(self) -> list[str]:
        return list(self.groups)

    @property
    def cli_group_names(self) -> list[str]:
        return [name for name, group in self.groups.items() if not group.is_api]

    @property
    def api_group_names(self) -> list[str]:
        return [name for name, group in self.groups.items() if group.is_api]


def _strip_comments(node: Any) -> Any:
    """剔除下划线开头的说明键（YAML 不支持"行内注释键"时的替代写法）。"""
    if isinstance(node, dict):
        return {
            key: _strip_comments(value)
            for key, value in node.items()
            if not (isinstance(key, str) and key.startswith("_"))
        }
    if isinstance(node, list):
        return [_strip_comments(item) for item in node]
    return node


def parse_accounts(document: Any, *, source: str = "<memory>") -> AccountsConfig:
    """从已解析的 YAML/JSON 对象构造账号池配置。"""
    try:
        return AccountsConfig.model_validate(_strip_comments(document))
    except ValidationError as exc:
        raise ToolError(
            ErrorCode.CONFIG_INVALID,
            f"accounts 配置校验失败：{source}",
            detail={
                "source": source,
                "errors": [
                    {"loc": ".".join(str(p) for p in err["loc"]), "msg": err["msg"]}
                    for err in exc.errors()
                ],
            },
        ) from exc


def load_accounts(path: str | Path) -> AccountsConfig:
    """读取并校验账号池配置。"""
    target = Path(path)
    if not target.is_file():
        raise ToolError(
            ErrorCode.CONFIG_INVALID,
            f"账号池配置不存在：{target}",
            detail={"path": str(target)},
        )

    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ToolError(
            ErrorCode.CONFIG_INVALID,
            f"accounts YAML 语法错误：{target}",
            detail={"path": str(target), "reason": str(exc)},
        ) from exc

    if not isinstance(raw, dict):
        raise ToolError(
            ErrorCode.CONFIG_INVALID,
            f"accounts 配置顶层必须是映射：{target}",
            detail={"path": str(target), "actual_type": type(raw).__name__},
        )

    return parse_accounts(raw, source=str(target))


__all__ = [
    "API_KEY_ENV_NAMES",
    "ENV_PREFIX",
    "AccountConfig",
    "AccountsConfig",
    "ApiSpec",
    "GroupConfig",
    "load_accounts",
    "parse_accounts",
]
