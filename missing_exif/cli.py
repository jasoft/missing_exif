"""命令行入口与三阶段流水线编排。"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time
from typing import Sequence

from .common import (
    DEFAULT_SCAN_WORKERS,
    log_info,
    normalize_excluded_dir_names,
    resolve_backup_dir,
    resolve_state_paths,
)
from .discover_stage import discover_media_to_jsonl
from .filter_stage import filter_discover_to_plan_jsonl
from .shell import ensure_exiftool_available
from .write_stage import (
    confirm_execution,
    load_plan_items,
    print_plan,
    process_plan,
)


def print_error_summary(
    title: str,
    errors: Sequence[str],
    preview_count: int = 20,
    to_stderr: bool = False,
) -> None:
    """输出错误摘要。

    Args:
        title: 摘要标题。
        errors: 错误信息列表。
        preview_count: 最多展示条目数。
        to_stderr: 是否输出到标准错误。
    """
    if not errors:
        return

    stream = sys.stderr if to_stderr else sys.stdout
    print(f"{title}: {len(errors)}", file=stream)
    for message in errors[:preview_count]:
        print(f"- {message}", file=stream)

    remaining = len(errors) - preview_count
    if remaining > 0:
        print(f"... 其余 {remaining} 条已省略。", file=stream)


def parse_common_target_and_backup(
    target_dir: Path,
    backup_dir: Path,
) -> tuple[Path, Path]:
    """规范化目标目录与备份目录。

    Args:
        target_dir: 用户输入目标目录。
        backup_dir: 用户输入备份目录。

    Returns:
        tuple[Path, Path]: 规范化后的目录路径。

    Raises:
        ValueError: 目标目录非法时抛出。
    """
    resolved_target = target_dir.resolve()
    if not resolved_target.exists() or not resolved_target.is_dir():
        raise ValueError(f"目标目录不存在或不是目录: {resolved_target}")

    resolved_backup = resolve_backup_dir(resolved_target, backup_dir).resolve()
    return resolved_target, resolved_backup


def run_discover_command(args: argparse.Namespace) -> int:
    """执行预扫描子命令。

    Args:
        args: 命令行参数。

    Returns:
        int: 退出码。
    """
    try:
        target_dir, backup_dir = parse_common_target_and_backup(
            args.target_dir,
            args.backup_dir,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    excluded_names = normalize_excluded_dir_names(args.exclude_dir)

    log_info(f"开始预扫描目录: {target_dir}")
    if excluded_names:
        log_info(f"目录排除: {', '.join(sorted(excluded_names))}")

    discover_media_to_jsonl(
        target_dir=target_dir,
        backup_dir=backup_dir,
        excluded_dir_names=excluded_names,
        output_path=args.output,
    )
    return 0


def run_filter_command(args: argparse.Namespace) -> int:
    """执行筛选子命令。

    Args:
        args: 命令行参数。

    Returns:
        int: 退出码。
    """
    try:
        target_dir, backup_dir = parse_common_target_and_backup(
            args.target_dir,
            args.backup_dir,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        ensure_exiftool_available()
    except Exception as exc:  # noqa: BLE001
        print(f"初始化失败: {exc}", file=sys.stderr)
        return 2

    processed_count, pending_count, errors = filter_discover_to_plan_jsonl(
        input_path=args.input,
        output_path=args.output,
        target_dir=target_dir,
        backup_dir=backup_dir,
        worker_count=args.scan_workers,
        progress_interval=args.progress_interval,
    )
    print_error_summary("筛选阶段错误（已跳过）", errors, to_stderr=True)
    log_info(f"筛选结果: 已处理 {processed_count}，待写回 {pending_count}")
    return 0 if not errors else 2


def run_write_command(args: argparse.Namespace) -> int:
    """执行写回子命令。

    Args:
        args: 命令行参数。

    Returns:
        int: 退出码。
    """
    target_dir = args.target_dir.resolve()
    if not target_dir.exists() or not target_dir.is_dir():
        print(f"目标目录不存在或不是目录: {target_dir}", file=sys.stderr)
        return 2

    try:
        ensure_exiftool_available()
    except Exception as exc:  # noqa: BLE001
        print(f"初始化失败: {exc}", file=sys.stderr)
        return 2

    plan, parse_errors = load_plan_items(args.input)
    print_error_summary("计划文件解析错误（已跳过）", parse_errors)

    print_plan(plan, target_dir)
    if not plan:
        print("没有需要修改的文件。")
        return 0 if not parse_errors else 2

    if args.dry_run:
        print("Dry Run 模式：仅预览，不执行写入。")
        return 0 if not parse_errors else 2

    if not confirm_execution(args.yes):
        print("用户取消，未执行写入。")
        return 1

    retry_until_success = args.retry_until_success
    retry_interval_seconds = max(args.retry_interval_seconds, 0)
    retry_max_rounds = max(args.retry_max_rounds, 0)

    round_number = 1
    total_success_count = 0
    remaining_plan = list(plan)
    last_errors: list[str] = []

    while remaining_plan:
        if round_number == 1:
            print(f"开始写回，共 {len(remaining_plan)} 个文件。")
        else:
            print(
                f"开始第 {round_number} 轮重试，待处理 {len(remaining_plan)} 个文件。"
            )

        success_count, failed_items, errors = process_plan(
            remaining_plan,
            args.write_progress_interval,
        )
        total_success_count += success_count
        last_errors = errors

        if not failed_items:
            break

        print_error_summary(f"第 {round_number} 轮失败明细", errors)

        if not retry_until_success:
            print(
                f"写入完成: 成功 {total_success_count}，失败 {len(failed_items)}"
            )
            return 2

        if retry_max_rounds > 0 and round_number >= retry_max_rounds:
            print(
                f"达到最大重试轮次 {retry_max_rounds}，"
                f"仍有 {len(failed_items)} 个文件失败。"
            )
            print_error_summary("最终失败明细", errors)
            return 2

        print(
            f"第 {round_number} 轮后剩余失败 {len(failed_items)} 个，"
            f"{retry_interval_seconds} 秒后重试。"
        )
        remaining_plan = failed_items
        round_number += 1
        if retry_interval_seconds > 0:
            time.sleep(retry_interval_seconds)

    print(f"写入完成: 成功 {total_success_count}，失败 0")
    if parse_errors:
        return 2
    if last_errors:
        print_error_summary("写回失败明细", last_errors)
        return 2
    return 0


def run_pipeline_command(args: argparse.Namespace) -> int:
    """执行完整流水线：预扫描 -> 筛选 -> 写回。

    Args:
        args: 命令行参数。

    Returns:
        int: 退出码。
    """
    try:
        target_dir, backup_dir = parse_common_target_and_backup(
            args.target_dir,
            args.backup_dir,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        ensure_exiftool_available()
    except Exception as exc:  # noqa: BLE001
        print(f"初始化失败: {exc}", file=sys.stderr)
        return 2

    excluded_names = normalize_excluded_dir_names(args.exclude_dir)
    discover_file, plan_file = resolve_state_paths(target_dir, backup_dir)

    log_info(f"开始扫描目录: {target_dir}")
    log_info("流程: 预扫描 -> 筛选 -> 写回")
    if excluded_names:
        log_info(f"目录排除: {', '.join(sorted(excluded_names))}")

    refresh_discover = args.refresh_discover
    refresh_filter = args.refresh_filter or refresh_discover

    if discover_file.exists() and not refresh_discover:
        log_info(f"复用预扫描结果: {discover_file}")
    else:
        log_info(f"写入预扫描结果: {discover_file}")
        discover_media_to_jsonl(
            target_dir=target_dir,
            backup_dir=backup_dir,
            excluded_dir_names=excluded_names,
            output_path=str(discover_file),
        )

    filter_errors: list[str] = []
    if plan_file.exists() and not refresh_filter:
        log_info(f"复用筛选结果: {plan_file}")
    else:
        log_info(f"写入筛选结果: {plan_file}")
        _, _, filter_errors = filter_discover_to_plan_jsonl(
            input_path=str(discover_file),
            output_path=str(plan_file),
            target_dir=target_dir,
            backup_dir=backup_dir,
            worker_count=args.scan_workers,
            progress_interval=args.progress_interval,
        )
        print_error_summary("筛选阶段错误（已跳过）", filter_errors)

    write_args = argparse.Namespace(
        target_dir=target_dir,
        input=str(plan_file),
        dry_run=args.dry_run,
        yes=args.yes,
        write_progress_interval=args.write_progress_interval,
        retry_until_success=args.retry_until_success,
        retry_interval_seconds=args.retry_interval_seconds,
        retry_max_rounds=args.retry_max_rounds,
    )
    write_code = run_write_command(write_args)

    if filter_errors and write_code == 0:
        return 2
    return write_code


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。

    Returns:
        argparse.ArgumentParser: 解析器对象。
    """
    parser = argparse.ArgumentParser(
        description=(
            "扫描目录中的图片/视频文件（含 HEIF），并为缺失拍摄时间"
            "元数据的文件写入最后修改时间。"
        )
    )

    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="执行完整流水线（默认）")
    add_common_target_args(run_parser)
    add_common_scan_args(run_parser)
    add_exclude_dir_arg(run_parser)
    add_common_execution_args(run_parser)
    run_parser.add_argument(
        "--refresh-discover",
        action="store_true",
        help="强制重新执行预扫描阶段（不复用 discover JSONL）。",
    )
    run_parser.add_argument(
        "--refresh-filter",
        action="store_true",
        help="强制重新执行筛选阶段（不复用 plan JSONL）。",
    )

    discover_parser = subparsers.add_parser("discover", help="仅执行预扫描阶段")
    add_common_target_args(discover_parser)
    add_exclude_dir_arg(discover_parser)
    discover_parser.add_argument(
        "--output",
        default="-",
        help="输出 JSONL 路径，默认 `-`（标准输出）。",
    )

    filter_parser = subparsers.add_parser("filter", help="仅执行筛选阶段")
    add_common_target_args(filter_parser)
    add_common_scan_args(filter_parser)
    filter_parser.add_argument(
        "--input",
        default="-",
        help="输入 JSONL 路径，默认 `-`（标准输入）。",
    )
    filter_parser.add_argument(
        "--output",
        default="-",
        help="输出 JSONL 路径，默认 `-`（标准输出）。",
    )

    write_parser = subparsers.add_parser("write", help="仅执行写回阶段")
    write_parser.add_argument("target_dir", type=Path, help="要扫描的目录路径。")
    write_parser.add_argument(
        "--input",
        default="-",
        help="输入 JSONL 路径，默认 `-`（标准输入）。",
    )
    add_common_execution_args(write_parser)

    return parser


def add_common_target_args(parser: argparse.ArgumentParser) -> None:
    """添加目标目录与备份目录参数。

    Args:
        parser: 子命令解析器。
    """
    parser.add_argument("target_dir", type=Path, help="要扫描的目录路径。")
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=Path("__exif_backups"),
        help="备份目录。相对路径会自动拼接到 target_dir 下。",
    )


def add_common_scan_args(parser: argparse.ArgumentParser) -> None:
    """添加筛选阶段相关参数。

    Args:
        parser: 子命令解析器。
    """
    parser.add_argument(
        "--progress-interval",
        type=int,
        default=50,
        help="筛选阶段每处理多少个文件输出一次进度，默认 50。",
    )
    parser.add_argument(
        "--scan-workers",
        type=int,
        default=DEFAULT_SCAN_WORKERS,
        help=f"筛选阶段并发线程数，默认 {DEFAULT_SCAN_WORKERS}。",
    )


def add_exclude_dir_arg(parser: argparse.ArgumentParser) -> None:
    """添加目录排除参数。

    Args:
        parser: 子命令解析器。
    """
    parser.add_argument(
        "--exclude-dir",
        action="append",
        default=[],
        metavar="DIR_NAME",
        help=(
            "按目录名排除扫描。可重复传入，也支持逗号分隔，例如 "
            '--exclude-dir "#recycle" --exclude-dir .thumb'
        ),
    )


def add_common_execution_args(parser: argparse.ArgumentParser) -> None:
    """添加写回阶段相关参数。

    Args:
        parser: 子命令解析器。
    """
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅预览将修改哪些文件，不执行实际写入。",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="跳过交互确认，直接执行写入（不影响 --dry-run）。",
    )
    parser.add_argument(
        "--write-progress-interval",
        type=int,
        default=20,
        help="写回阶段每处理多少个文件输出一次进度，默认 20。",
    )
    parser.add_argument(
        "--retry-until-success",
        action="store_true",
        help=(
            "写回失败后自动按失败项重试，直到全部成功。"
            "可配合 --retry-max-rounds 限制轮次。"
        ),
    )
    parser.add_argument(
        "--retry-interval-seconds",
        type=int,
        default=10,
        help="写回重试间隔秒数，默认 10。",
    )
    parser.add_argument(
        "--retry-max-rounds",
        type=int,
        default=0,
        help="写回最大重试轮次，0 表示不限制。",
    )


def normalize_legacy_argv(argv: list[str]) -> list[str]:
    """将旧用法参数转换为默认 `run` 子命令。

    Args:
        argv: 原始参数列表。

    Returns:
        list[str]: 归一化后的参数列表。
    """
    if not argv:
        return argv

    known_commands = {"run", "discover", "filter", "write", "-h", "--help"}
    if argv[0] in known_commands:
        return argv

    return ["run", *argv]


def dispatch(args: argparse.Namespace) -> int:
    """根据子命令分发执行。

    Args:
        args: 解析后的参数。

    Returns:
        int: 退出码。
    """
    command = args.command or "run"

    if command == "discover":
        return run_discover_command(args)
    if command == "filter":
        return run_filter_command(args)
    if command == "write":
        return run_write_command(args)
    return run_pipeline_command(args)


def main(argv: Sequence[str] | None = None) -> int:
    """程序入口。

    Args:
        argv: 可选参数列表，默认使用 `sys.argv[1:]`。

    Returns:
        int: 进程退出码。
    """
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    normalized_argv = normalize_legacy_argv(raw_argv)

    parser = build_parser()
    if not normalized_argv:
        parser.print_help(sys.stderr)
        return 2

    args = parser.parse_args(normalized_argv)
    return dispatch(args)
