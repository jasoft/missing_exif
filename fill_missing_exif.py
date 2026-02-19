#!/usr/bin/env python3
"""扫描目录中的媒体文件，并为缺失拍摄时间元数据的文件补写时间信息。

该脚本会递归扫描图片和视频文件（包含 HEIF/HEIC）。当文件缺失常见拍摄时间标签时，
会使用文件系统中的“最后修改时间”进行写入。

安全策略：
1. 实际写入前先输出将被修改的文件清单；
2. 支持 `--dry-run` 仅预览不写入；
3. 写入前先备份原文件，失败时尝试自动回滚。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Literal, Sequence

MediaKind = Literal["image", "video"]
PlanItem = tuple[Path, MediaKind, str, str, Path]

IMAGE_EXTENSIONS = {
    ".avif",
    ".dng",
    ".heic",
    ".heif",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}

VIDEO_EXTENSIONS = {
    ".3gp",
    ".avi",
    ".flv",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mts",
    ".webm",
    ".wmv",
}

IMAGE_METADATA_TAGS = (
    "EXIF:all",
    "XMP:all",
    "IPTC:all",
)

VIDEO_METADATA_TAGS = (
    "QuickTime:all",
    "Keys:all",
    "XMP:all",
    "EXIF:all",
)


def parse_args() -> argparse.Namespace:
    """解析命令行参数。

    Returns:
        argparse.Namespace: 命令行参数对象。
    """
    parser = argparse.ArgumentParser(
        description=(
            "扫描目录中的图片/视频文件（含 HEIF），并为缺失拍摄时间元数据"
            "的文件写入最后修改时间。"
        )
    )
    parser.add_argument("target_dir", type=Path, help="要扫描的目录路径。")
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=Path("__exif_backups"),
        help="备份目录。相对路径会自动拼接到 target_dir 下。",
    )
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
        "--progress-interval",
        type=int,
        default=50,
        help="扫描阶段每处理多少个媒体文件输出一次进度，默认 50。",
    )
    return parser.parse_args()


def ensure_exiftool_available() -> None:
    """检查 `exiftool` 是否可用。

    Raises:
        RuntimeError: 当系统中找不到 `exiftool` 时抛出。
    """
    result = subprocess.run(
        ["exiftool", "-ver"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "未检测到 exiftool。请先安装 exiftool 并确保命令可执行。"
        )


def run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """执行外部命令并在失败时抛出异常。

    Args:
        command: 待执行命令及参数。

    Returns:
        subprocess.CompletedProcess[str]: 命令执行结果。

    Raises:
        RuntimeError: 命令执行失败时抛出，并附带 stderr 信息。
    """
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(f"命令失败: {' '.join(command)}\n{stderr}")
    return result


def resolve_backup_dir(target_dir: Path, backup_dir: Path) -> Path:
    """规范化备份目录路径。

    Args:
        target_dir: 扫描根目录。
        backup_dir: 用户传入的备份目录。

    Returns:
        Path: 绝对备份目录路径。
    """
    if backup_dir.is_absolute():
        return backup_dir
    return target_dir / backup_dir


def detect_media_kind(file_path: Path) -> MediaKind | None:
    """根据扩展名识别媒体类型。

    Args:
        file_path: 文件路径。

    Returns:
        MediaKind | None: 图片/视频类型，若不支持则返回 None。
    """
    ext = file_path.suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    return None


def iter_media_files(target_dir: Path, backup_dir: Path) -> list[tuple[Path, MediaKind]]:
    """递归收集目标目录中的媒体文件。

    Args:
        target_dir: 扫描根目录。
        backup_dir: 备份目录（用于避免扫描备份文件）。

    Returns:
        list[tuple[Path, MediaKind]]: 媒体文件与类型列表。
    """
    media_files: list[tuple[Path, MediaKind]] = []
    for file_path in target_dir.rglob("*"):
        if not file_path.is_file():
            continue
        if backup_dir in file_path.parents:
            continue
        media_kind = detect_media_kind(file_path)
        if media_kind is None:
            continue
        media_files.append((file_path, media_kind))
    return media_files


def read_selected_tags(file_path: Path, tags: Sequence[str]) -> dict[str, str]:
    """读取文件中的指定元数据标签。

    Args:
        file_path: 待读取文件路径。
        tags: 需要读取的标签列表。

    Returns:
        dict[str, str]: 标签与值映射，缺失标签不会出现在结果中。

    Raises:
        RuntimeError: exiftool 返回值异常或 JSON 解析失败时抛出。
    """
    command = ["exiftool", "-j", "-n", "-G1"]
    command.extend(f"-{tag}" for tag in tags)
    command.append(str(file_path))
    result = run_command(command)

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"解析 exiftool 输出失败: {file_path}") from exc

    if not payload:
        return {}
    row = payload[0]
    return {str(key): str(value) for key, value in row.items()}


def has_existing_metadata(file_path: Path, media_kind: MediaKind) -> bool:
    """判断文件是否已存在媒体元数据。

    Args:
        file_path: 文件路径。
        media_kind: 媒体类型（image/video）。

    Returns:
        bool: 若存在任意 EXIF/XMP/QuickTime/Keys/IPTC 标签则返回 True。
    """
    tags = IMAGE_METADATA_TAGS if media_kind == "image" else VIDEO_METADATA_TAGS
    metadata = read_selected_tags(file_path, tags)
    for key, value in metadata.items():
        if key == "SourceFile":
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if value is None:
            continue
        return True
    return False


def should_report_scan_progress(
    index: int, total_files: int, effective_interval: int
) -> bool:
    """判断是否应输出扫描进度。

    Args:
        index: 当前处理序号（从 1 开始）。
        total_files: 媒体总数。
        effective_interval: 有效进度间隔。

    Returns:
        bool: True 表示应输出进度信息。
    """
    return index == 1 or index == total_files or index % effective_interval == 0


def print_scan_progress(
    index: int,
    total_files: int,
    file_path: Path,
    target_dir: Path,
    pending_count: int,
) -> None:
    """输出扫描进度信息。

    Args:
        index: 当前处理序号（从 1 开始）。
        total_files: 媒体总数。
        file_path: 当前处理文件路径。
        target_dir: 扫描根目录。
        pending_count: 当前待修改数量。
    """
    percent = (index / total_files) * 100
    current_file = safe_relative_path(file_path, target_dir)
    print(
        "[扫描进度] "
        f"{index}/{total_files} ({percent:.1f}%) | "
        f"当前: {current_file} | 待修改: {pending_count}",
        flush=True,
    )


def build_modification_plan(
    target_dir: Path, backup_dir: Path, progress_interval: int
) -> list[PlanItem]:
    """构建需要修改的文件清单。

    Args:
        target_dir: 扫描根目录。
        backup_dir: 备份目录。
        progress_interval: 扫描进度输出间隔（媒体文件数量）。

    Returns:
        list[PlanItem]: 待处理清单，每项为：
            (文件路径, 媒体类型, exif_time, iso_time, 备份路径)。
    """
    print("跳过规则: 文件存在 EXIF/XMP/QuickTime/Keys/IPTC 元数据即跳过。")
    media_files = iter_media_files(target_dir, backup_dir)
    total_files = len(media_files)
    print(f"扫描到媒体文件总数: {total_files}", flush=True)

    plan: list[PlanItem] = []
    if total_files == 0:
        return plan

    effective_interval = max(progress_interval, 1)
    for index, (file_path, media_kind) in enumerate(media_files, start=1):
        if should_report_scan_progress(index, total_files, effective_interval):
            print_scan_progress(
                index=index,
                total_files=total_files,
                file_path=file_path,
                target_dir=target_dir,
                pending_count=len(plan),
            )

        if has_existing_metadata(file_path, media_kind):
            continue

        exif_time, iso_time = format_exif_time(file_path)
        relative_path = file_path.relative_to(target_dir)
        backup_path = backup_dir / relative_path
        plan.append((file_path, media_kind, exif_time, iso_time, backup_path))
    return plan


def format_exif_time(file_path: Path) -> tuple[str, str]:
    """将文件最后修改时间格式化为元数据写入格式。

    Args:
        file_path: 文件路径。

    Returns:
        tuple[str, str]: `EXIF` 时间格式和带时区的 `ISO 8601` 时间格式。
    """
    modified_at = datetime.fromtimestamp(file_path.stat().st_mtime).astimezone()
    exif_time = modified_at.strftime("%Y:%m:%d %H:%M:%S")
    iso_time = modified_at.isoformat(timespec="seconds")
    return exif_time, iso_time
def print_plan(plan: Sequence[PlanItem], target_dir: Path) -> None:
    """输出待修改文件清单。

    Args:
        plan: 待处理清单。
        target_dir: 扫描根目录，用于相对路径展示。
    """
    print(f"将修改文件数量: {len(plan)}")
    for index, (file_path, media_kind, exif_time, _, backup_path) in enumerate(
        plan, start=1
    ):
        rel_file = safe_relative_path(file_path, target_dir)
        rel_backup = safe_relative_path(backup_path, target_dir)
        print(
            f"[{index}] {rel_file} | 类型: {media_kind} | 写入时间: {exif_time} | "
            f"备份: {rel_backup}"
        )


def safe_relative_path(path: Path, base_dir: Path) -> Path:
    """尽可能返回相对路径，失败时返回绝对路径。

    Args:
        path: 目标路径。
        base_dir: 参考目录。

    Returns:
        Path: 相对路径或原始绝对路径。
    """
    try:
        return path.relative_to(base_dir)
    except ValueError:
        return path


def confirm_execution(force_yes: bool) -> bool:
    """在执行写入前进行交互确认。

    Args:
        force_yes: 是否跳过确认。

    Returns:
        bool: True 表示允许执行，False 表示取消执行。
    """
    if force_yes:
        return True
    answer = input("确认开始写入并创建备份吗？输入 yes 继续: ").strip().lower()
    return answer in {"y", "yes"}


def allocate_backup_path(backup_path: Path) -> Path:
    """分配可用备份路径，避免覆盖已存在备份。

    Args:
        backup_path: 初始备份路径。

    Returns:
        Path: 实际可写入的备份路径。
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
        Path: 实际备份文件路径。
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
        iso_time: ISO 8601 时间字符串（含时区）。
    """
    command = ["exiftool", "-overwrite_original", "-P", "-m"]
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


def process_plan(plan: Sequence[PlanItem]) -> tuple[int, list[str]]:
    """执行备份与写入，并在失败时回滚。

    Args:
        plan: 待处理清单。

    Returns:
        tuple[int, list[str]]: 成功数量与失败信息列表。
    """
    success_count = 0
    errors: list[str] = []

    for file_path, media_kind, exif_time, iso_time, backup_path in plan:
        rollback_from: Path | None = None
        try:
            rollback_from = backup_file(file_path, backup_path)
            write_capture_time(file_path, media_kind, exif_time, iso_time)
            success_count += 1
        except Exception as exc:  # noqa: BLE001
            if rollback_from and rollback_from.exists():
                shutil.copy2(rollback_from, file_path)
            errors.append(f"{file_path}: {exc}")
    return success_count, errors


def main() -> int:
    """主函数。

    Returns:
        int: 进程退出码。0 表示成功，非 0 表示失败或取消。
    """
    args = parse_args()
    target_dir = args.target_dir.resolve()
    backup_dir = resolve_backup_dir(target_dir, args.backup_dir).resolve()

    if not target_dir.exists() or not target_dir.is_dir():
        print(f"目标目录不存在或不是目录: {target_dir}", file=sys.stderr)
        return 2

    try:
        ensure_exiftool_available()
        print(f"开始扫描目录: {target_dir}", flush=True)
        plan = build_modification_plan(
            target_dir, backup_dir, args.progress_interval
        )
    except Exception as exc:  # noqa: BLE001
        print(f"初始化失败: {exc}", file=sys.stderr)
        return 2

    print_plan(plan, target_dir)
    if not plan:
        print("没有需要修改的文件。")
        return 0

    if args.dry_run:
        print("Dry Run 模式：仅预览，不执行写入。")
        return 0

    if not confirm_execution(args.yes):
        print("用户取消，未执行写入。")
        return 1

    success_count, errors = process_plan(plan)
    print(f"写入完成: 成功 {success_count}，失败 {len(errors)}")
    if errors:
        print("失败明细：")
        for item in errors:
            print(f"- {item}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
