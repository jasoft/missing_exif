"""公共常量、类型与路径辅助函数。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import os
from pathlib import Path
import sys
from typing import Literal, Sequence, cast

MediaKind = Literal["image", "video"]

STATE_DIR_NAME = ".missing_exif_state"
DEFAULT_SCAN_WORKERS = max(4, min(32, (os.cpu_count() or 4) * 2))

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


@dataclass(frozen=True)
class DiscoverItem:
    """预扫描阶段输出的媒体项。"""

    file_path: Path
    relative_path: Path
    media_kind: MediaKind

    def to_dict(self) -> dict[str, str]:
        """转换为可序列化字典。

        Returns:
            dict[str, str]: JSON 可序列化字典。
        """
        return {
            "file_path": str(self.file_path),
            "relative_path": str(self.relative_path),
            "media_kind": self.media_kind,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "DiscoverItem":
        """从字典恢复对象。

        Args:
            payload: JSON 字典。

        Returns:
            DiscoverItem: 恢复后的对象。

        Raises:
            ValueError: 当字段缺失或类型不合法时抛出。
        """
        file_path = payload.get("file_path")
        relative_path = payload.get("relative_path")
        media_kind = payload.get("media_kind")

        if not isinstance(file_path, str) or not file_path:
            raise ValueError("discover 记录缺少 file_path")
        if not isinstance(relative_path, str) or not relative_path:
            raise ValueError("discover 记录缺少 relative_path")
        if media_kind not in {"image", "video"}:
            raise ValueError("discover 记录缺少或包含非法 media_kind")

        checked_kind = cast(MediaKind, media_kind)
        return cls(
            file_path=Path(file_path),
            relative_path=Path(relative_path),
            media_kind=checked_kind,
        )


@dataclass(frozen=True)
class PlanItem:
    """写回阶段输入的计划项。"""

    file_path: Path
    relative_path: Path
    media_kind: MediaKind
    exif_time: str
    iso_time: str
    backup_path: Path

    def to_dict(self) -> dict[str, str]:
        """转换为可序列化字典。

        Returns:
            dict[str, str]: JSON 可序列化字典。
        """
        return {
            "file_path": str(self.file_path),
            "relative_path": str(self.relative_path),
            "media_kind": self.media_kind,
            "exif_time": self.exif_time,
            "iso_time": self.iso_time,
            "backup_path": str(self.backup_path),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "PlanItem":
        """从字典恢复对象。

        Args:
            payload: JSON 字典。

        Returns:
            PlanItem: 恢复后的对象。

        Raises:
            ValueError: 当字段缺失或类型不合法时抛出。
        """
        required = {
            "file_path",
            "relative_path",
            "media_kind",
            "exif_time",
            "iso_time",
            "backup_path",
        }
        missing = required.difference(payload)
        if missing:
            raise ValueError(f"plan 记录缺少字段: {sorted(missing)}")

        media_kind = payload["media_kind"]
        if media_kind not in {"image", "video"}:
            raise ValueError("plan 记录包含非法 media_kind")
        checked_kind = cast(MediaKind, media_kind)

        return cls(
            file_path=Path(str(payload["file_path"])),
            relative_path=Path(str(payload["relative_path"])),
            media_kind=checked_kind,
            exif_time=str(payload["exif_time"]),
            iso_time=str(payload["iso_time"]),
            backup_path=Path(str(payload["backup_path"])),
        )


def log_info(message: str) -> None:
    """输出日志到标准错误。

    Args:
        message: 日志文本。
    """
    print(message, file=sys.stderr, flush=True)


def normalize_excluded_dir_names(raw_values: Sequence[str]) -> set[str]:
    """规范化目录排除规则。

    Args:
        raw_values: 用户输入的目录名，支持逗号分隔与重复传入。

    Returns:
        set[str]: 去重并小写化后的目录名集合。
    """
    names: set[str] = set()
    for raw in raw_values:
        for item in raw.split(","):
            normalized = item.strip().lower()
            if normalized:
                names.add(normalized)
    return names


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


def resolve_state_paths(target_dir: Path, backup_dir: Path) -> tuple[Path, Path]:
    """生成自动管理的中间文件路径。

    Args:
        target_dir: 扫描根目录。
        backup_dir: 备份目录。

    Returns:
        tuple[Path, Path]: (discover_jsonl, plan_jsonl)。
    """
    target_hash = hashlib.sha1(
        str(target_dir).encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()[:16]
    state_dir = backup_dir / STATE_DIR_NAME
    discover_file = state_dir / f"discover_{target_hash}.jsonl"
    plan_file = state_dir / f"plan_{target_hash}.jsonl"
    return discover_file, plan_file


def safe_relative_path(path: Path, base_dir: Path) -> Path:
    """尽可能返回相对路径，失败时返回原路径。

    Args:
        path: 目标路径。
        base_dir: 参考目录。

    Returns:
        Path: 相对路径或原始路径。
    """
    try:
        return path.relative_to(base_dir)
    except ValueError:
        return path


def safe_resolve_path(path: Path) -> Path:
    """安全解析真实路径，失败时返回原路径。

    Args:
        path: 待解析路径。

    Returns:
        Path: 解析后的路径或原路径。
    """
    try:
        return path.resolve()
    except OSError:
        return path


def is_path_under_base(path: Path, base_dir: Path) -> bool:
    """判断路径是否位于给定目录下。

    Args:
        path: 待判断路径。
        base_dir: 基准目录。

    Returns:
        bool: 若路径位于基准目录下返回 True。
    """
    try:
        path.relative_to(base_dir)
    except ValueError:
        return False
    return True


def is_file_in_excluded_dirs(
    file_path: Path,
    target_dir: Path,
    excluded_dir_names: set[str],
) -> bool:
    """判断文件是否位于排除目录中。

    Args:
        file_path: 文件路径。
        target_dir: 扫描根目录。
        excluded_dir_names: 被排除目录名集合。

    Returns:
        bool: 位于排除目录返回 True。
    """
    if not excluded_dir_names:
        return False

    try:
        relative_parts = file_path.relative_to(target_dir).parts
    except ValueError:
        return False

    for part in relative_parts[:-1]:
        if part.lower() in excluded_dir_names:
            return True
    return False


def should_skip_walk_directory(
    directory_path: Path,
    backup_dir: Path,
    excluded_dir_names: set[str],
    visited_real_dirs: set[str],
) -> bool:
    """判断目录是否应在遍历时跳过。

    Args:
        directory_path: 目录路径。
        backup_dir: 备份目录。
        excluded_dir_names: 被排除目录名集合。
        visited_real_dirs: 已访问真实目录路径集合。

    Returns:
        bool: 应跳过返回 True。
    """
    if directory_path.name.lower() in excluded_dir_names:
        return True

    if is_path_under_base(directory_path, backup_dir):
        return True

    resolved_directory = safe_resolve_path(directory_path)
    if is_path_under_base(resolved_directory, backup_dir):
        return True

    return str(resolved_directory) in visited_real_dirs


def should_skip_media_file(
    file_path: Path,
    target_dir: Path,
    backup_dir: Path,
    excluded_dir_names: set[str],
) -> bool:
    """判断媒体文件是否应跳过。

    Args:
        file_path: 文件路径。
        target_dir: 扫描根目录。
        backup_dir: 备份目录。
        excluded_dir_names: 被排除目录名集合。

    Returns:
        bool: 应跳过返回 True。
    """
    if is_path_under_base(file_path, backup_dir):
        return True

    resolved_file = safe_resolve_path(file_path)
    if is_path_under_base(resolved_file, backup_dir):
        return True

    return is_file_in_excluded_dirs(file_path, target_dir, excluded_dir_names)


def detect_media_kind(file_path: Path) -> MediaKind | None:
    """根据扩展名识别媒体类型。

    Args:
        file_path: 文件路径。

    Returns:
        MediaKind | None: 图片/视频类型，或 None。
    """
    ext = file_path.suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    return None


def format_exif_time(file_path: Path) -> tuple[str, str]:
    """将文件最后修改时间格式化为元数据写入格式。

    Args:
        file_path: 文件路径。

    Returns:
        tuple[str, str]: (EXIF 时间, ISO 时间)。
    """
    modified_at = datetime.fromtimestamp(file_path.stat().st_mtime).astimezone()
    exif_time = modified_at.strftime("%Y:%m:%d %H:%M:%S")
    iso_time = modified_at.isoformat(timespec="seconds")
    return exif_time, iso_time
