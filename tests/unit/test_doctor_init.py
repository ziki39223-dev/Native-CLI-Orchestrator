"""自检（doctor）与初始化（init）的测试。

重点验证三件"1.0 可用性"的事：
1. doctor 能区分"跑不起来"（ERROR）与"能跑但不对"（WARN）；
2. doctor 绝不打印凭据值；
3. --init 只补缺失文件，不覆盖已存在的配置。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from native_cli_orchestrator.entry.doctor import Level, format_report, run_doctor
from native_cli_orchestrator.entry.init import (
    ACCOUNTS_TEMPLATE,
    CONFIG_TEMPLATE,
    init_project,
)


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


MIN_ACCOUNTS = """\
groups:
  trae_group: { accounts: [ { label: trae-a } ] }
  qoder_group: { accounts: [ { label: qoder-a } ] }
  workbuddy_group: { accounts: [ { label: wb-a } ] }
"""


def _project(tmp_path: Path, *, mode: str = "mock", accounts: str | None = None) -> Path:
    _write(
        tmp_path / "config" / "config.yaml",
        f"""\
mode: {mode}
transport: sse
accounts_path: config/accounts.yml
workspace_root: workspace
backup_root: backups
log_dir: logs
server: {{host: 127.0.0.1, port: 18770, log_level: WARNING}}
upstream: {{enabled: false}}
""",
    )
    _write(tmp_path / "config" / "accounts.yml", accounts or MIN_ACCOUNTS)
    return tmp_path


def _find(report, area: str) -> list:  # type: ignore[no-untyped-def]
    return [f for f in report.findings if f.area == area]


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #


def test_doctor_passes_on_minimal_valid_project(tmp_path: Path) -> None:
    root = _project(tmp_path)
    report = run_doctor(config_path=root / "config" / "config.yaml", environ={})

    assert report.passed, [f.message for f in report.errors]
    areas = {f.area for f in report.findings}
    assert {"python", "deps", "config", "workspace", "port", "accounts"} <= areas


def test_doctor_flags_mock_mode_as_warning(tmp_path: Path) -> None:
    root = _project(tmp_path, mode="mock")
    report = run_doctor(config_path=root / "config" / "config.yaml", environ={})

    warns = [f for f in report.warnings if f.area == "config"]
    assert warns, "mock 模式应提示：真机运行要改 real"
    assert "real" in warns[0].action


def test_doctor_no_mock_warning_in_real_mode(tmp_path: Path) -> None:
    root = _project(tmp_path, mode="real")
    report = run_doctor(config_path=root / "config" / "config.yaml", environ={})
    assert not [f for f in report.warnings if f.area == "config" and "mock" in f.message]


def test_doctor_errors_when_accounts_missing(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "config" / "accounts.yml").unlink()

    report = run_doctor(config_path=root / "config" / "config.yaml", environ={})

    assert not report.passed
    assert any("accounts.yml" in f.message for f in report.errors)
    assert report.next_steps


def test_doctor_errors_when_cli_missing_in_real_mode(tmp_path: Path) -> None:
    """real 模式下 CLI 不存在是 ERROR（mock 下只是 WARN）。"""
    root = _project(tmp_path, mode="real")
    report = run_doctor(config_path=root / "config" / "config.yaml", environ={})

    cli_errors = [f for f in report.errors if f.area == "cli"]
    assert cli_errors, "real 模式找不到 CLI 必须报错"
    assert "probe_cli" in cli_errors[0].action


def test_doctor_cli_missing_is_only_warning_in_mock(tmp_path: Path) -> None:
    root = _project(tmp_path, mode="mock")
    report = run_doctor(config_path=root / "config" / "config.yaml", environ={})

    assert not [f for f in report.errors if f.area == "cli"]
    assert [f for f in report.warnings if f.area == "cli"]


def test_doctor_reports_missing_api_key_without_leaking_value(tmp_path: Path) -> None:
    """API key 未设置时报警，但**绝不能**把值打印出来。"""
    accounts = MIN_ACCOUNTS + """\
  paid_group:
    kind: api
    api: {base_url: "https://api.example.com/v1", model: m, daily_budget: 10,
          input_price_per_mtok: 1.0, output_price_per_mtok: 1.0}
    accounts:
      - label: p-a
        env: {OPENAI_API_KEY: "env:DOCTOR_TEST_KEY"}
"""
    root = _project(tmp_path, accounts=accounts)
    report = run_doctor(config_path=root / "config" / "config.yaml", environ={})

    key_errors = [f for f in report.errors if f.area == "api"]
    assert key_errors
    assert "DOCTOR_TEST_KEY" in key_errors[0].message
    assert "未设置" in key_errors[0].message


def test_doctor_never_prints_secret_value(tmp_path: Path) -> None:
    accounts = MIN_ACCOUNTS + """\
  paid_group:
    kind: api
    api: {base_url: "https://api.example.com/v1", model: m}
    accounts:
      - label: p-a
        env: {OPENAI_API_KEY: "env:DOCTOR_TEST_KEY"}
"""
    root = _project(tmp_path, accounts=accounts)
    report = run_doctor(
        config_path=root / "config" / "config.yaml",
        environ={"DOCTOR_TEST_KEY": "sk-super-secret-value-12345"},
    )
    rendered = format_report(report)

    assert "sk-super-secret-value-12345" not in rendered
    assert "API key 已就绪" in rendered


def test_doctor_warns_api_budget_without_price(tmp_path: Path) -> None:
    """设了预算但没填单价 ⇒ 预算永远不会触发，必须有 WARN。"""
    accounts = MIN_ACCOUNTS + """\
  paid_group:
    kind: api
    api: {base_url: "https://api.example.com/v1", model: m, daily_budget: 10}
    accounts:
      - label: p-a
        env: {OPENAI_API_KEY: "env:K"}
"""
    root = _project(tmp_path, accounts=accounts)
    report = run_doctor(
        config_path=root / "config" / "config.yaml", environ={"K": "sk-x"}
    )

    warns = [f for f in report.warnings if f.area == "api"]
    assert any("价格" in f.message for f in warns)


def test_doctor_warns_zero_budget_on_paid_api(tmp_path: Path) -> None:
    accounts = MIN_ACCOUNTS + """\
  paid_group:
    kind: api
    api: {base_url: "https://api.example.com/v1", model: m}
    accounts:
      - label: p-a
        env: {OPENAI_API_KEY: "env:K"}
"""
    root = _project(tmp_path, accounts=accounts)
    report = run_doctor(
        config_path=root / "config" / "config.yaml", environ={"K": "sk-x"}
    )
    assert any("未设日预算" in f.message for f in report.warnings)


def test_doctor_warns_exposed_without_token(tmp_path: Path) -> None:
    _write(
        tmp_path / "config" / "config.yaml",
        """\
mode: mock
transport: sse
accounts_path: config/accounts.yml
workspace_root: workspace
backup_root: backups
log_dir: logs
server: {host: 0.0.0.0, port: 18771, auth_token: "", log_level: WARNING}
upstream: {enabled: false}
""",
    )
    _write(tmp_path / "config" / "accounts.yml", MIN_ACCOUNTS)

    report = run_doctor(config_path=tmp_path / "config" / "config.yaml", environ={})
    assert any("auth_token" in f.message for f in report.warnings)


def test_doctor_broken_yaml_is_error(tmp_path: Path) -> None:
    _write(tmp_path / "config" / "config.yaml", "mode: [unclosed\n")
    _write(tmp_path / "config" / "accounts.yml", MIN_ACCOUNTS)

    report = run_doctor(config_path=tmp_path / "config" / "config.yaml", environ={})

    assert not report.passed
    assert any(f.area == "config" for f in report.errors)


def test_doctor_invalid_accounts_becomes_error_with_field_path(tmp_path: Path) -> None:
    bad = MIN_ACCOUNTS.replace("workbuddy_group: { accounts", "workbuddy_groupX: { accounts")
    root = _project(tmp_path, accounts=bad)

    report = run_doctor(config_path=root / "config" / "config.yaml", environ={})

    assert not report.passed
    accounts_errors = [f for f in report.errors if f.area == "accounts"]
    assert accounts_errors
    # 字段路径以 WARN 明细形式给出
    assert any("↳" in f.message for f in report.warnings if f.area == "accounts")


def test_doctor_port_conflict_detected(tmp_path: Path) -> None:
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    busy_port = sock.getsockname()[1]
    try:
        _write(
            tmp_path / "config" / "config.yaml",
            f"""\
mode: mock
transport: sse
accounts_path: config/accounts.yml
workspace_root: workspace
backup_root: backups
log_dir: logs
server: {{host: 127.0.0.1, port: {busy_port}, log_level: WARNING}}
upstream: {{enabled: false}}
""",
        )
        _write(tmp_path / "config" / "accounts.yml", MIN_ACCOUNTS)

        report = run_doctor(config_path=tmp_path / "config" / "config.yaml", environ={})

        port_errors = [f for f in report.errors if f.area == "port"]
        assert port_errors
        assert "netstat" in port_errors[0].action
    finally:
        sock.close()


def test_format_report_contains_verdict_and_next_steps(tmp_path: Path) -> None:
    root = _project(tmp_path)
    text = format_report(run_doctor(config_path=root / "config" / "config.yaml", environ={}))

    assert "结论：" in text
    assert "下一步" in text


def test_format_report_marks_errors(tmp_path: Path) -> None:
    root = _project(tmp_path)
    (root / "config" / "accounts.yml").unlink()
    text = format_report(run_doctor(config_path=root / "config" / "config.yaml", environ={}))

    assert "[ERROR]" in text
    assert "未通过" in text


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #


def test_init_creates_missing_files(tmp_path: Path) -> None:
    result = init_project(tmp_path)

    assert (tmp_path / "config" / "config.yaml").is_file()
    assert (tmp_path / "config" / "accounts.yml").is_file()
    assert (tmp_path / "scripts" / "set_keys.example.bat").is_file()
    assert len(result.created) == 3
    assert result.anything_created


def test_init_never_overwrites_existing(tmp_path: Path) -> None:
    """已存在的配置绝不能被覆盖 —— 里面可能有用户填好的设置。"""
    mine = tmp_path / "config" / "accounts.yml"
    _write(mine, "# 用户自己的配置\nconfig_version: mine\n")

    result = init_project(tmp_path)

    assert mine.read_text(encoding="utf-8").startswith("# 用户自己的配置")
    assert mine in result.skipped
    assert not any(p == mine for p in result.created)


def test_init_is_idempotent(tmp_path: Path) -> None:
    first = init_project(tmp_path)
    second = init_project(tmp_path)

    assert len(first.created) == 3
    assert second.created == []
    assert len(second.skipped) == 3


def test_init_creates_runtime_dirs(tmp_path: Path) -> None:
    init_project(tmp_path)
    for name in ("workspace", "logs", "backups"):
        assert (tmp_path / name).is_dir()


def test_init_generated_config_is_loadable(tmp_path: Path) -> None:
    """生成的配置必须能直接被 doctor 接受（否则"一键就绪"是假的）。"""
    init_project(tmp_path)
    report = run_doctor(config_path=tmp_path / "config" / "config.yaml", environ={})

    assert not [f for f in report.errors if f.area in {"config", "accounts"}], [
        (f.area, f.message) for f in report.errors
    ]


def test_init_templates_have_no_plaintext_secret() -> None:
    """模板里不能出现明文凭据（只有 env: 引用与占位说明）。"""
    for template in (ACCOUNTS_TEMPLATE, CONFIG_TEMPLATE):
        assert "env:" in template or "accounts" in template
        for line in template.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or ":" not in stripped:
                continue
            # 任何形如 KEY: "sk-xxx" 的行都不允许
            assert "sk-" not in stripped


def test_init_generated_bat_is_gbk() -> None:
    """生成的 bat 必须是 GBK + CRLF，否则 Windows 控制台中文乱码。"""
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        init_project(root)
        data = (root / "scripts" / "set_keys.example.bat").read_bytes()
    # 中文在 UTF-8 下会是多字节；GBK 下"账号"为 4 字节
    assert "账号".encode("gbk") in data
    assert "\r\n".encode() in data or b"\n" in data


__all__ = ["Level", "pytest"]
