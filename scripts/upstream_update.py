"""命令行版上游更新入口（供 upstream-update.bat 调用）。

用法：
    python scripts/upstream_update.py          # 执行更新（失败自动回滚）
    python scripts/upstream_update.py --check  # 只检查版本
    python scripts/upstream_update.py --force  # 忽略版本比较
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from native_cli_orchestrator.config.loader import load_settings  # noqa: E402
from native_cli_orchestrator.contracts.errors import ToolError  # noqa: E402
from native_cli_orchestrator.upstream.updater import UpstreamUpdater  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="上游一键更新（带备份回滚）")
    parser.add_argument("--check", action="store_true", help="只检查版本，不修改文件")
    parser.add_argument("--force", action="store_true", help="忽略版本比较强制更新")
    parser.add_argument("--config", default=None, help="配置文件路径")
    args = parser.parse_args()

    try:
        settings = load_settings(args.config)
    except ToolError as exc:
        print(f"[配置错误] {exc.code.value}: {exc.message}", file=sys.stderr)
        return 2

    if not settings.upstream.enabled:
        print("[跳过] 上游更新未启用（config.yaml → upstream.enabled=false）")
        return 0

    updater = UpstreamUpdater(
        settings=settings.upstream,
        project_root=PROJECT_ROOT,
        backup_root=settings.backup_root,
    )

    print(f"上游仓库: {settings.upstream.repo or '(未配置)'}")
    print(f"分支/标签: {settings.upstream.ref}")
    print(f"备份目录: {settings.backup_root}")
    print()

    try:
        result = updater.update(check_only=args.check, force=args.force)
    except ToolError as exc:
        print(f"[失败] {exc.code.value}: {exc.message}", file=sys.stderr)
        if exc.detail:
            print(json.dumps(exc.detail, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1

    if result.updated:
        print(f"[成功] {result.from_version or '(空)'} → {result.to_version}")
        print(f"       备份: {result.backup_path}")
        print(f"       变更 {len(result.changed_files)} 个文件")
        for name in result.changed_files[:20]:
            print(f"         - {name}")
        if len(result.changed_files) > 20:
            print(f"         … 其余 {len(result.changed_files) - 20} 个")
        print("       兼容性校验: 通过")
        print()
        print("提示：更新后建议执行 reload_config，并跑一次冒烟任务确认 CLI 参数未变。")
    else:
        print(f"[跳过] {result.from_version or '(空)'} → {result.to_version or '(未知)'}")
        for note in result.notes:
            print(f"       {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
