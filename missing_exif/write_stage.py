"""写回阶段：备份文件并写入拍摄时间元数据。"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
from typing import Sequence

from .common import MediaKind, PlanItem, safe_relative_path
from .jsonl_io import open_jsonl_reader
from .shell import exiftool_base_command, run_command


def load_plan_items(input_path: str) -> tuple[list[PlanItem], list[str]]:
    """从 JSONL 读取待写回计划。

    Args:
        input_path: 输入路径，`-` 代表标准输入。

    Returns:
        tuple[list[PlanItem], list[str]]: 计划列表与解析错误列表。
    """
    items: list[PlanItem] = []
    errors: list[str] = []

    with open_jsonl_reader(input_path) as reader:
        for line_no, raw_line in enumerate(reader, start=1):
            line_text = raw_line.strip()
            if not line_text:
                continue

            try:
                payload = json.loads(line_text)
            except json.JSONDecodeError as exc:
                errors.append(f"第 {line_no} 行 JSON 解析失败: {exc}")
                continue

            if not isinstance(payload, dict):
                errors.append(f"第 {line_no} 行不是 JSON 对象")
                continue

            try:
                items.append(PlanItem.from_dict(payload))
            except ValueError as exc:
                errors.append(f"第 {line_no} 行记录无效: {exc}")

    return items, errors


def print_plan(plan: Sequence[PlanItem], target_dir: Path) -> None:
    """输出待修改文件清单。

    Args:
        plan: 待处理清单。
        target_dir: 扫描根目录，用于相对路径展示。
    """
    print(f"将修改文件数量: {len(plan)}")
    for index, item in enumerate(plan, start=1):
        rel_file = safe_relative_path(item.file_path, target_dir)
        rel_backup = safe_relative_path(item.backup_path, target_dir)
        print(
            f"[{index}] {rel_file} | 类型: {item.media_kind} | "
            f"写入时间: {item.exif_time} | 备份: {rel_backup}"
        )


def confirm_execution(force_yes: bool) -> bool:
    """在执行写入前进行交互确认。

    Args:
        force_yes: 是否跳过确认。

    Returns:
        bool: True 表示允许执行。
    """
    if force_yes:
        return True
    answer = input("确认开始写入并创建备份吗？输入 yes 继续: ").strip().lower()
    return answer in {"y", "yes"}


def allocate_backup_path(backup_path: Path) -> Path:
    """分配可用备份路径，避免覆盖已有备份。

    Args:
        backup_path: 期望备份路径。

    Returns:
        Path: 实际可写入备份路径。
    """
    if not backup_path.exists():
        return backup_path

    counter = 1
    while True:
        candidate = backup_path.with_name(f"{backup_path.name}.bak{counter}")
        if not candidate.exists():
            return candidate
        counter += 1


def backup_file(file_path: Path, backup_path: Path) -> Path:
    """备份原始文件。

    Args:
        file_path: 原始文件路径。
        backup_path: 期望备份路径。

    Returns:
        Path: 实际备份路径。
    """
    actual_backup = allocate_backup_path(backup_path)
    actual_backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(file_path, actual_backup)
    return actual_backup


def write_capture_time(
    file_path: Path,
    media_kind: MediaKind,
    exif_time: str,
    iso_time: str,
) -> None:
    """将拍摄时间写入目标文件。

    Args:
        file_path: 目标文件路径。
        media_kind: 媒体类型（image/video）。
        exif_time: EXIF 风格时间字符串。
        iso_time: ISO 8601 时间字符串。
    """
    command = exiftool_base_command()
    command.extend(["-overwrite_original", "-P", "-m"])

    if media_kind == "image":
        command.extend(
            [
                f"-EXIF:DateTimeOriginal={exif_time}",
                f"-EXIF:CreateDate={exif_time}",
                f"-EXIF:ModifyDate={exif_time}",
                f"-XMP:DateTimeOriginal={exif_time}",
                f"-XMP:CreateDate={exif_time}",
            ]
        )
    else:
        command.extend(
            [
                f"-QuickTime:CreateDate={exif_time}",
                f"-QuickTime:ModifyDate={exif_time}",
                f"-QuickTime:TrackCreateDate={exif_time}",
                f"-QuickTime:TrackModifyDate={exif_time}",
                f"-QuickTime:MediaCreateDate={exif_time}",
                f"-QuickTime:MediaModifyDate={exif_time}",
                f"-Keys:CreationDate={iso_time}",
                f"-XMP:CreateDate={exif_time}",
            ]
        )

    command.append(str(file_path))
    run_command(command)


def process_plan(
    plan: Sequence[PlanItem],
    progress_interval: int,
) -> tuple[int, list[str]]:
    """执行备份与写入，并在失败时回滚。

    Args:
        plan: 待处理清单。
        progress_interval: 进度输出间隔。

    Returns:
        tuple[int, list[str]]: 成功数量与失败列表。
    """
    success_count = 0
    errors: list[str] = []
    total = len(plan)
    interval = max(progress_interval, 1)

    for index, item in enumerate(plan, start=1):
        rollback_from: Path | None = None
        try:
            rollback_from = backup_file(item.file_path, item.backup_path)
            write_capture_time(
                file_path=item.file_path,
                media_kind=item.media_kind,
                exif_time=item.exif_time,
                iso_time=item.iso_time,
            )
            success_count += 1
        except Exception as exc:  # noqa: BLE001
            if rollback_from and rollback_from.exists():
                shutil.copy2(rollback_from, item.file_path)
            errors.append(f"{item.file_path}: {exc}")

        if index == 1 or index == total or index % interval == 0:
            percent = (index / total) * 100
            print(
                "[写回进度] "
                f"{index}/{total} ({percent:.1f}%) | "
                f"成功: {success_count} | "
                f"失败: {len(errors)}"
            )

    return success_count, errors
