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
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Sequence, cast

MediaKind = Literal["image", "video"]
PlanItem = tuple[Path, MediaKind, str, str, Path]
ScanResult = tuple[Path, PlanItem | None, str | None]

DEFAULT_SCAN_WORKERS = max(4, min(32, (os.cpu_count() or 4) * 2))
DEFAULT_PLAN_FLUSH_INTERVAL = 200
STATE_DIR_NAME = ".missing_exif_state"
DISCOVERY_FILE_PROGRESS_INTERVAL = 1000
DISCOVERY_DIR_PROGRESS_INTERVAL = 300

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
    parser.add_argument(
        "--scan-workers",
        type=int,
        default=DEFAULT_SCAN_WORKERS,
        help=f"扫描阶段并发线程数，默认 {DEFAULT_SCAN_WORKERS}。",
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


def resolve_state_plan_file(target_dir: Path, backup_dir: Path) -> Path:
    """生成自动管理的计划文件路径。

    Args:
        target_dir: 扫描根目录。
        backup_dir: 备份目录。

    Returns:
        Path: 计划文件路径。
    """
    target_hash = hashlib.sha1(
        str(target_dir).encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()[:16]
    state_dir = backup_dir / STATE_DIR_NAME
    return state_dir / f"scan_state_{target_hash}.json"


def normalize_excluded_dir_names(raw_values: Sequence[str]) -> set[str]:
    """规范化目录排除规则。

    Args:
        raw_values: 用户输入的目录名，支持重复和逗号分隔。

    Returns:
        set[str]: 去重且小写化后的目录名集合。
    """
    names: set[str] = set()
    for raw in raw_values:
        for item in raw.split(","):
            normalized = item.strip().lower()
            if normalized:
                names.add(normalized)
    return names


def now_iso_time() -> str:
    """返回当前 UTC 时间字符串。

    Returns:
        str: ISO 8601 格式时间字符串。
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_file_fingerprint(file_path: Path) -> tuple[int, int]:
    """获取文件指纹信息。

    Args:
        file_path: 文件路径。

    Returns:
        tuple[int, int]: (文件大小, mtime_ns)。
    """
    stat_result = file_path.stat()
    return stat_result.st_size, stat_result.st_mtime_ns


def serialize_plan_item(plan_item: PlanItem) -> dict[str, str]:
    """序列化计划项。

    Args:
        plan_item: 待序列化计划项。

    Returns:
        dict[str, str]: 可写入 JSON 的计划项字典。
    """
    file_path, media_kind, exif_time, iso_time, backup_path = plan_item
    return {
        "file_path": str(file_path),
        "media_kind": media_kind,
        "exif_time": exif_time,
        "iso_time": iso_time,
        "backup_path": str(backup_path),
    }


def deserialize_plan_item(payload: dict[str, str]) -> PlanItem:
    """反序列化计划项。

    Args:
        payload: JSON 中的计划项字典。

    Returns:
        PlanItem: 反序列化后的计划项。

    Raises:
        ValueError: 当 JSON 字段缺失时抛出。
    """
    required_keys = {
        "file_path",
        "media_kind",
        "exif_time",
        "iso_time",
        "backup_path",
    }
    if not required_keys.issubset(payload):
        missing_keys = required_keys.difference(payload)
        raise ValueError(f"计划项缺失字段: {sorted(missing_keys)}")

    media_kind = payload["media_kind"]
    if media_kind not in {"image", "video"}:
        raise ValueError(f"未知媒体类型: {media_kind}")
    checked_media_kind = cast(MediaKind, media_kind)

    return (
        Path(payload["file_path"]),
        checked_media_kind,
        payload["exif_time"],
        payload["iso_time"],
        Path(payload["backup_path"]),
    )


def serialize_fingerprint(size: int, mtime_ns: int) -> dict[str, int]:
    """序列化文件指纹。

    Args:
        size: 文件大小。
        mtime_ns: 修改时间纳秒值。

    Returns:
        dict[str, int]: 指纹字典。
    """
    return {"size": size, "mtime_ns": mtime_ns}


def fingerprint_matches(
    fingerprint_payload: dict[str, int], size: int, mtime_ns: int
) -> bool:
    """判断文件指纹是否匹配。

    Args:
        fingerprint_payload: 计划文件中的指纹字典。
        size: 当前文件大小。
        mtime_ns: 当前修改时间纳秒值。

    Returns:
        bool: 匹配返回 True。
    """
    return (
        fingerprint_payload.get("size") == size
        and fingerprint_payload.get("mtime_ns") == mtime_ns
    )


class ScanPlanStore:
    """扫描计划与进度存储器。"""

    def __init__(
        self,
        plan_file: Path,
        flush_interval: int,
        reset_plan: bool,
    ) -> None:
        """初始化存储器。

        Args:
            plan_file: 计划文件路径。
            flush_interval: 刷盘间隔。
            reset_plan: 是否重置已有计划。
        """
        self.plan_file = plan_file
        self.flush_interval = max(flush_interval, 1)
        self._dirty_count = 0
        self._items_by_path: dict[str, dict[str, str]] = {}

        self.payload: dict[str, object]
        if reset_plan:
            self.payload = self._new_payload()
        else:
            self.payload = self._load_or_create_payload()

        items = cast(list[dict[str, str]], self.payload["items"])
        for item_payload in items:
            file_path = item_payload["file_path"]
            self._items_by_path[file_path] = item_payload

        self.flush(force=True)

    def _new_payload(self) -> dict[str, object]:
        """创建空计划数据。

        Returns:
            dict[str, object]: 空计划数据。
        """
        now = now_iso_time()
        return {
            "version": 2,
            "status": "initialized",
            "created_at": now,
            "updated_at": now,
            "target_dir": "",
            "backup_dir": "",
            "excluded_dir_names": [],
            "scan_workers": 0,
            "total_media_files": 0,
            "scanned_media_files": 0,
            "fatal_error": "",
            "scan_errors": [],
            "items": [],
            "scanned_files": {},
        }

    def _load_or_create_payload(self) -> dict[str, object]:
        """加载已有计划，若不存在则新建。

        Returns:
            dict[str, object]: 计划数据。
        """
        if not self.plan_file.exists():
            return self._new_payload()

        content = self.plan_file.read_text(encoding="utf-8")
        if not content.strip():
            return self._new_payload()

        payload = json.loads(content)
        if not isinstance(payload, dict):
            raise ValueError(f"计划文件格式错误: {self.plan_file}")

        payload.setdefault("version", 2)
        payload.setdefault("status", "initialized")
        payload.setdefault("created_at", now_iso_time())
        payload.setdefault("updated_at", now_iso_time())
        payload.setdefault("target_dir", "")
        payload.setdefault("backup_dir", "")
        payload.setdefault("excluded_dir_names", [])
        payload.setdefault("scan_workers", 0)
        payload.setdefault("total_media_files", 0)
        payload.setdefault("scanned_media_files", 0)
        payload.setdefault("fatal_error", "")
        payload.setdefault("scan_errors", [])
        payload.setdefault("items", [])
        payload.setdefault("scanned_files", {})
        return payload

    def set_scan_context(
        self,
        target_dir: Path,
        backup_dir: Path,
        excluded_dir_names: set[str],
        scan_workers: int,
    ) -> None:
        """设置扫描上下文信息。

        Args:
            target_dir: 扫描根目录。
            backup_dir: 备份目录。
            excluded_dir_names: 排除目录名集合。
            scan_workers: 扫描线程数。
        """
        self.payload["target_dir"] = str(target_dir)
        self.payload["backup_dir"] = str(backup_dir)
        self.payload["excluded_dir_names"] = sorted(excluded_dir_names)
        self.payload["scan_workers"] = scan_workers
        self.payload["scan_errors"] = []
        self.payload["fatal_error"] = ""
        self.payload["scanned_media_files"] = 0
        self.touch()

    def set_status(self, status: str) -> None:
        """设置计划状态。

        Args:
            status: 状态字符串。
        """
        self.payload["status"] = status
        self.touch()

    def set_total_media_files(self, total_media_files: int) -> None:
        """设置媒体总数。

        Args:
            total_media_files: 媒体文件总数。
        """
        self.payload["total_media_files"] = total_media_files
        self.touch()

    def set_scanned_media_files(self, scanned_media_files: int) -> None:
        """设置已扫描数量。

        Args:
            scanned_media_files: 已扫描文件数量。
        """
        self.payload["scanned_media_files"] = scanned_media_files
        self.touch()
        self.maybe_flush()

    def record_scan_error(self, error_message: str) -> None:
        """记录扫描错误并持续写入。

        Args:
            error_message: 错误信息。
        """
        scan_errors = cast(list[str], self.payload["scan_errors"])
        scan_errors.append(error_message)
        self.touch()
        self.maybe_flush()

    def record_fatal_error(self, error_message: str) -> None:
        """记录致命错误。

        Args:
            error_message: 致命错误信息。
        """
        self.payload["fatal_error"] = error_message
        self.touch()
        self.flush(force=True)

    def add_or_update_plan_item(self, plan_item: PlanItem) -> None:
        """新增或更新计划项。

        Args:
            plan_item: 计划项。
        """
        item_payload = serialize_plan_item(plan_item)
        file_key = item_payload["file_path"]
        self._items_by_path[file_key] = item_payload
        self.touch()
        self.maybe_flush()

    def remove_plan_item(self, file_path: Path) -> None:
        """移除计划项。

        Args:
            file_path: 文件路径。
        """
        file_key = str(file_path)
        if file_key in self._items_by_path:
            self._items_by_path.pop(file_key)
            self.touch()
            self.maybe_flush()

    def has_plan_item(self, file_path: Path) -> bool:
        """判断计划项是否存在。

        Args:
            file_path: 文件路径。

        Returns:
            bool: 计划项存在返回 True。
        """
        return str(file_path) in self._items_by_path

    def set_scanned_file_record(
        self,
        file_path: Path,
        media_kind: MediaKind,
        size: int,
        mtime_ns: int,
        status: str,
    ) -> None:
        """写入单文件扫描记录。

        Args:
            file_path: 文件路径。
            media_kind: 媒体类型。
            size: 文件大小。
            mtime_ns: 文件修改时间纳秒值。
            status: 扫描状态。
        """
        scanned_files = cast(
            dict[str, dict[str, object]],
            self.payload["scanned_files"],
        )
        scanned_files[str(file_path)] = {
            "media_kind": media_kind,
            "status": status,
            "fingerprint": serialize_fingerprint(size, mtime_ns),
        }
        self.touch()
        self.maybe_flush()

    def get_cached_status(
        self,
        file_path: Path,
        media_kind: MediaKind,
        size: int,
        mtime_ns: int,
    ) -> str | None:
        """查询文件是否命中历史扫描缓存。

        Args:
            file_path: 文件路径。
            media_kind: 媒体类型。
            size: 当前文件大小。
            mtime_ns: 当前修改时间纳秒值。

        Returns:
            str | None: 命中则返回状态字符串，否则返回 None。
        """
        scanned_files = cast(
            dict[str, dict[str, object]],
            self.payload["scanned_files"],
        )
        cache_record = scanned_files.get(str(file_path))
        if not isinstance(cache_record, dict):
            return None

        if cache_record.get("media_kind") != media_kind:
            return None

        fingerprint_payload = cache_record.get("fingerprint")
        if not isinstance(fingerprint_payload, dict):
            return None

        if not fingerprint_matches(fingerprint_payload, size, mtime_ns):
            return None

        status = cache_record.get("status")
        if not isinstance(status, str):
            return None
        return status

    def get_plan_items(self) -> list[PlanItem]:
        """读取当前计划项列表。

        Returns:
            list[PlanItem]: 计划项列表。
        """
        plan_items: list[PlanItem] = []
        for item_payload in self._items_by_path.values():
            plan_items.append(deserialize_plan_item(item_payload))
        plan_items.sort(key=lambda item: str(item[0]))
        return plan_items

    def get_pending_count(self) -> int:
        """获取当前待处理数量。

        Returns:
            int: 待处理数量。
        """
        return len(self._items_by_path)

    def get_target_dir_from_plan(self) -> Path | None:
        """从计划文件读取目标目录。

        Returns:
            Path | None: 目标目录路径，缺失时返回 None。
        """
        target_dir = self.payload.get("target_dir")
        if isinstance(target_dir, str) and target_dir.strip():
            return Path(target_dir)
        return None

    def touch(self) -> None:
        """更新计划更新时间。"""
        self.payload["updated_at"] = now_iso_time()
        self._dirty_count += 1

    def maybe_flush(self) -> None:
        """按间隔刷盘。"""
        if self._dirty_count >= self.flush_interval:
            self.flush(force=True)

    def flush(self, force: bool = False) -> None:
        """将计划写入磁盘。

        Args:
            force: 是否强制刷盘。
        """
        if not force and self._dirty_count == 0:
            return

        self.payload["items"] = list(self._items_by_path.values())
        self.plan_file.parent.mkdir(parents=True, exist_ok=True)
        temp_file = self.plan_file.with_suffix(f"{self.plan_file.suffix}.tmp")
        temp_file.write_text(
            json.dumps(self.payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_file.replace(self.plan_file)
        self._dirty_count = 0


def is_file_in_excluded_dirs(
    file_path: Path, target_dir: Path, excluded_dir_names: set[str]
) -> bool:
    """判断文件是否位于被排除目录中。

    Args:
        file_path: 文件路径。
        target_dir: 扫描根目录。
        excluded_dir_names: 被排除目录名集合（小写）。

    Returns:
        bool: 若文件位于被排除目录中则返回 True。
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


def is_path_under_base(path: Path, base_dir: Path) -> bool:
    """判断路径是否位于给定目录下。

    Args:
        path: 待判断路径。
        base_dir: 基准目录。

    Returns:
        bool: 若路径位于基准目录下则返回 True。
    """
    try:
        path.relative_to(base_dir)
    except ValueError:
        return False
    return True


def safe_resolve_path(path: Path) -> Path:
    """安全解析路径，失败时返回原路径。

    Args:
        path: 待解析路径。

    Returns:
        Path: 解析后的路径或原路径。
    """
    try:
        return path.resolve()
    except OSError:
        return path


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
        visited_real_dirs: 已访问目录真实路径集合。

    Returns:
        bool: True 表示应跳过该目录。
    """
    if directory_path.name.lower() in excluded_dir_names:
        return True

    if is_path_under_base(directory_path, backup_dir):
        return True

    real_directory = safe_resolve_path(directory_path)
    if is_path_under_base(real_directory, backup_dir):
        return True

    return str(real_directory) in visited_real_dirs


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
        bool: True 表示应跳过该文件。
    """
    if is_path_under_base(file_path, backup_dir):
        return True

    resolved_file_path = safe_resolve_path(file_path)
    if is_path_under_base(resolved_file_path, backup_dir):
        return True

    return is_file_in_excluded_dirs(file_path, target_dir, excluded_dir_names)


def should_report_discovery_progress(
    visited_dir_count: int,
    inspected_file_count: int,
) -> bool:
    """判断是否应输出预扫描进度。

    Args:
        visited_dir_count: 已遍历目录数。
        inspected_file_count: 已检查文件数。

    Returns:
        bool: True 表示应输出进度。
    """
    if visited_dir_count == 1:
        return True

    if visited_dir_count % DISCOVERY_DIR_PROGRESS_INTERVAL == 0:
        return True

    if inspected_file_count % DISCOVERY_FILE_PROGRESS_INTERVAL == 0:
        return True

    return False


def print_discovery_progress(
    visited_dir_count: int,
    inspected_file_count: int,
    media_candidate_count: int,
    current_dir: Path,
    target_dir: Path,
) -> None:
    """输出预扫描进度。

    Args:
        visited_dir_count: 已遍历目录数。
        inspected_file_count: 已检查文件数。
        media_candidate_count: 已发现媒体候选数量。
        current_dir: 当前目录。
        target_dir: 扫描根目录。
    """
    relative_dir = safe_relative_path(current_dir, target_dir)
    print(
        "[预扫描进度] "
        f"目录: {visited_dir_count} | "
        f"文件: {inspected_file_count} | "
        f"媒体候选: {media_candidate_count} | "
        f"当前目录: {relative_dir}",
        flush=True,
    )


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


def iter_media_files(
    target_dir: Path,
    backup_dir: Path,
    excluded_dir_names: set[str],
) -> list[tuple[Path, MediaKind]]:
    """递归收集目标目录中的媒体文件。

    Args:
        target_dir: 扫描根目录。
        backup_dir: 备份目录（用于避免扫描备份文件）。
        excluded_dir_names: 被排除目录名集合。

    Returns:
        list[tuple[Path, MediaKind]]: 媒体文件与类型列表。
    """
    media_files: list[tuple[Path, MediaKind]] = []
    visited_real_dirs: set[str] = set()
    visited_dir_count = 0
    inspected_file_count = 0

    for root_dir, dir_names, file_names in os.walk(target_dir, followlinks=True):
        root_path = Path(root_dir)
        visited_dir_count += 1
        root_real_path = safe_resolve_path(root_path)
        root_real_key = str(root_real_path)
        if root_real_key in visited_real_dirs:
            dir_names[:] = []
            continue
        visited_real_dirs.add(root_real_key)

        if should_report_discovery_progress(
            visited_dir_count=visited_dir_count,
            inspected_file_count=inspected_file_count,
        ):
            print_discovery_progress(
                visited_dir_count=visited_dir_count,
                inspected_file_count=inspected_file_count,
                media_candidate_count=len(media_files),
                current_dir=root_path,
                target_dir=target_dir,
            )

        filtered_dir_names: list[str] = []
        for dir_name in dir_names:
            child_dir_path = root_path / dir_name
            if should_skip_walk_directory(
                directory_path=child_dir_path,
                backup_dir=backup_dir,
                excluded_dir_names=excluded_dir_names,
                visited_real_dirs=visited_real_dirs,
            ):
                continue
            filtered_dir_names.append(dir_name)
        dir_names[:] = filtered_dir_names

        for file_name in file_names:
            file_path = root_path / file_name
            inspected_file_count += 1
            if should_skip_media_file(
                file_path=file_path,
                target_dir=target_dir,
                backup_dir=backup_dir,
                excluded_dir_names=excluded_dir_names,
            ):
                continue

            media_kind = detect_media_kind(file_path)
            if media_kind is None:
                continue
            media_files.append((file_path, media_kind))

            if should_report_discovery_progress(
                visited_dir_count=visited_dir_count,
                inspected_file_count=inspected_file_count,
            ):
                print_discovery_progress(
                    visited_dir_count=visited_dir_count,
                    inspected_file_count=inspected_file_count,
                    media_candidate_count=len(media_files),
                    current_dir=root_path,
                    target_dir=target_dir,
                )

    print(
        "[预扫描完成] "
        f"目录: {visited_dir_count} | "
        f"文件: {inspected_file_count} | "
        f"媒体候选: {len(media_files)}",
        flush=True,
    )
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


def build_plan_item(
    file_path: Path,
    media_kind: MediaKind,
    target_dir: Path,
    backup_dir: Path,
) -> PlanItem:
    """构建单文件计划项。

    Args:
        file_path: 文件路径。
        media_kind: 媒体类型。
        target_dir: 扫描根目录。
        backup_dir: 备份目录。

    Returns:
        PlanItem: 计划项。
    """
    exif_time, iso_time = format_exif_time(file_path)
    relative_path = file_path.relative_to(target_dir)
    backup_path = backup_dir / relative_path
    return (file_path, media_kind, exif_time, iso_time, backup_path)


def inspect_media_for_plan(
    file_path: Path,
    media_kind: MediaKind,
    target_dir: Path,
    backup_dir: Path,
) -> ScanResult:
    """检查单个媒体文件是否需要写入元数据。

    Args:
        file_path: 文件路径。
        media_kind: 媒体类型。
        target_dir: 扫描根目录。
        backup_dir: 备份目录。

    Returns:
        ScanResult: (文件路径, 计划项或 None, 错误信息或 None)。
    """
    try:
        if has_existing_metadata(file_path, media_kind):
            return file_path, None, None
        plan_item = build_plan_item(file_path, media_kind, target_dir, backup_dir)
        return file_path, plan_item, None
    except Exception as exc:  # noqa: BLE001
        return file_path, None, f"{file_path}: {exc}"


def submit_scan_task(
    executor: ThreadPoolExecutor,
    future_map: dict[Future[ScanResult], tuple[Path, MediaKind, int, int]],
    file_path: Path,
    media_kind: MediaKind,
    target_dir: Path,
    backup_dir: Path,
    size: int,
    mtime_ns: int,
) -> None:
    """提交单个扫描任务。

    Args:
        executor: 线程池执行器。
        future_map: future 映射表。
        file_path: 文件路径。
        media_kind: 媒体类型。
        target_dir: 扫描根目录。
        backup_dir: 备份目录。
        size: 文件大小。
        mtime_ns: 文件修改时间纳秒值。
    """
    future = executor.submit(
        inspect_media_for_plan,
        file_path,
        media_kind,
        target_dir,
        backup_dir,
    )
    future_map[future] = (file_path, media_kind, size, mtime_ns)


def refill_scan_tasks(
    executor: ThreadPoolExecutor,
    future_map: dict[Future[ScanResult], tuple[Path, MediaKind, int, int]],
    pending_inputs: list[tuple[Path, MediaKind, int, int]],
    target_dir: Path,
    backup_dir: Path,
    inflight_limit: int,
) -> None:
    """补充并发扫描任务到指定上限。

    Args:
        executor: 线程池执行器。
        future_map: future 映射表。
        pending_inputs: 待提交扫描输入。
        target_dir: 扫描根目录。
        backup_dir: 备份目录。
        inflight_limit: 最大并发队列长度。
    """
    while pending_inputs and len(future_map) < inflight_limit:
        file_path, media_kind, size, mtime_ns = pending_inputs.pop()
        submit_scan_task(
            executor=executor,
            future_map=future_map,
            file_path=file_path,
            media_kind=media_kind,
            target_dir=target_dir,
            backup_dir=backup_dir,
            size=size,
            mtime_ns=mtime_ns,
        )


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
    target_dir: Path,
    backup_dir: Path,
    progress_interval: int,
    excluded_dir_names: set[str],
    scan_workers: int,
    plan_store: ScanPlanStore,
) -> tuple[list[PlanItem], list[str]]:
    """构建需要修改的文件清单。

    Args:
        target_dir: 扫描根目录。
        backup_dir: 备份目录。
        progress_interval: 扫描进度输出间隔（媒体文件数量）。
        excluded_dir_names: 被排除目录名集合。
        scan_workers: 扫描并发线程数。
        plan_store: 扫描计划存储器。

    Returns:
        tuple[list[PlanItem], list[str]]: 待处理清单与扫描错误列表。
    """
    print("跳过规则: 文件存在 EXIF/XMP/QuickTime/Keys/IPTC 元数据即跳过。")
    if excluded_dir_names:
        names_text = ", ".join(sorted(excluded_dir_names))
        print(f"目录排除: {names_text}")
    print(f"扫描线程数: {max(scan_workers, 1)}")

    plan_store.set_scan_context(
        target_dir=target_dir,
        backup_dir=backup_dir,
        excluded_dir_names=excluded_dir_names,
        scan_workers=max(scan_workers, 1),
    )
    plan_store.set_status("scanning")

    media_files = iter_media_files(target_dir, backup_dir, excluded_dir_names)
    total_files = len(media_files)
    print(f"扫描到媒体文件总数: {total_files}", flush=True)
    plan_store.set_total_media_files(total_files)

    scan_errors: list[str] = []
    effective_interval = max(progress_interval, 1)
    completed_count = 0
    pending_inputs: list[tuple[Path, MediaKind, int, int]] = []

    for file_path, media_kind in media_files:
        try:
            size, mtime_ns = get_file_fingerprint(file_path)
        except OSError as exc:
            completed_count += 1
            error_message = f"{file_path}: 读取文件属性失败: {exc}"
            scan_errors.append(error_message)
            plan_store.record_scan_error(error_message)
            plan_store.set_scanned_media_files(completed_count)
            if should_report_scan_progress(
                completed_count, total_files, effective_interval
            ):
                print_scan_progress(
                    index=completed_count,
                    total_files=total_files,
                    file_path=file_path,
                    target_dir=target_dir,
                    pending_count=plan_store.get_pending_count(),
                )
            continue

        cached_status = plan_store.get_cached_status(
            file_path=file_path,
            media_kind=media_kind,
            size=size,
            mtime_ns=mtime_ns,
        )
        if cached_status is not None:
            if cached_status == "needs_patch" and not plan_store.has_plan_item(
                file_path
            ):
                pending_inputs.append((file_path, media_kind, size, mtime_ns))
                continue

            completed_count += 1
            plan_store.set_scanned_media_files(completed_count)
            if cached_status != "needs_patch":
                plan_store.remove_plan_item(file_path)
            if should_report_scan_progress(
                completed_count, total_files, effective_interval
            ):
                print_scan_progress(
                    index=completed_count,
                    total_files=total_files,
                    file_path=file_path,
                    target_dir=target_dir,
                    pending_count=plan_store.get_pending_count(),
                )
            continue

        pending_inputs.append((file_path, media_kind, size, mtime_ns))

    pending_inputs.reverse()
    if total_files == 0:
        return [], scan_errors

    if not pending_inputs:
        plan_store.flush(force=True)
        return plan_store.get_plan_items(), scan_errors

    worker_count = max(scan_workers, 1)
    inflight_limit = max(worker_count * 4, 1)
    future_map: dict[Future[ScanResult], tuple[Path, MediaKind, int, int]] = {}

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        refill_scan_tasks(
            executor=executor,
            future_map=future_map,
            pending_inputs=pending_inputs,
            target_dir=target_dir,
            backup_dir=backup_dir,
            inflight_limit=inflight_limit,
        )

        while future_map:
            completed_futures, _ = wait(
                set(future_map),
                return_when=FIRST_COMPLETED,
            )
            for future in completed_futures:
                file_path, media_kind, size, mtime_ns = future_map.pop(future)
                try:
                    checked_file_path, plan_item, scan_error = future.result()
                except Exception as exc:  # noqa: BLE001
                    checked_file_path = file_path
                    plan_item = None
                    scan_error = f"{file_path}: 线程扫描异常: {exc}"

                completed_count += 1
                plan_store.set_scanned_media_files(completed_count)

                if scan_error:
                    scan_errors.append(scan_error)
                    plan_store.record_scan_error(scan_error)
                    plan_store.set_scanned_file_record(
                        file_path=checked_file_path,
                        media_kind=media_kind,
                        size=size,
                        mtime_ns=mtime_ns,
                        status="scan_error",
                    )
                elif plan_item is not None:
                    plan_store.add_or_update_plan_item(plan_item)
                    plan_store.set_scanned_file_record(
                        file_path=checked_file_path,
                        media_kind=media_kind,
                        size=size,
                        mtime_ns=mtime_ns,
                        status="needs_patch",
                    )
                else:
                    plan_store.remove_plan_item(checked_file_path)
                    plan_store.set_scanned_file_record(
                        file_path=checked_file_path,
                        media_kind=media_kind,
                        size=size,
                        mtime_ns=mtime_ns,
                        status="has_metadata",
                    )

                if should_report_scan_progress(
                    completed_count, total_files, effective_interval
                ):
                    print_scan_progress(
                        index=completed_count,
                        total_files=total_files,
                        file_path=checked_file_path,
                        target_dir=target_dir,
                        pending_count=plan_store.get_pending_count(),
                    )

            refill_scan_tasks(
                executor=executor,
                future_map=future_map,
                pending_inputs=pending_inputs,
                target_dir=target_dir,
                backup_dir=backup_dir,
                inflight_limit=inflight_limit,
            )

    plan_store.flush(force=True)
    return plan_store.get_plan_items(), scan_errors


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


def load_plan_items_from_file(
    plan_file: Path,
) -> tuple[list[PlanItem], Path | None]:
    """从计划 JSON 文件读取待处理项。

    Args:
        plan_file: 计划文件路径。

    Returns:
        tuple[list[PlanItem], Path | None]: 计划项列表和计划内目标目录。

    Raises:
        ValueError: 当计划文件格式无效时抛出。
    """
    if not plan_file.exists():
        raise ValueError(f"计划文件不存在: {plan_file}")

    content = plan_file.read_text(encoding="utf-8")
    if not content.strip():
        raise ValueError(f"计划文件为空: {plan_file}")

    payload = json.loads(content)
    if not isinstance(payload, dict):
        raise ValueError(f"计划文件格式错误: {plan_file}")

    items_payload = payload.get("items")
    if not isinstance(items_payload, list):
        raise ValueError("计划文件缺少 items 数组。")

    plan_items: list[PlanItem] = []
    for item in items_payload:
        if not isinstance(item, dict):
            raise ValueError("计划项格式错误。")
        plan_items.append(deserialize_plan_item(item))

    target_dir_value = payload.get("target_dir")
    if isinstance(target_dir_value, str) and target_dir_value.strip():
        return plan_items, Path(target_dir_value)
    return plan_items, None


def print_error_summary(
    title: str,
    errors: Sequence[str],
    preview_count: int = 20,
) -> None:
    """输出错误摘要。

    Args:
        title: 摘要标题。
        errors: 错误信息列表。
        preview_count: 最多展示条目数。
    """
    if not errors:
        return

    print(f"{title}: {len(errors)}")
    for message in errors[:preview_count]:
        print(f"- {message}")
    remaining_count = len(errors) - preview_count
    if remaining_count > 0:
        print(f"... 其余 {remaining_count} 条已省略。")


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
    excluded_dir_names = normalize_excluded_dir_names(args.exclude_dir)
    plan_file = resolve_state_plan_file(target_dir, backup_dir)

    if not target_dir.exists() or not target_dir.is_dir():
        print(f"目标目录不存在或不是目录: {target_dir}", file=sys.stderr)
        return 2

    try:
        ensure_exiftool_available()
    except Exception as exc:  # noqa: BLE001
        print(f"初始化失败: {exc}", file=sys.stderr)
        return 2

    try:
        plan_store = ScanPlanStore(
            plan_file=plan_file,
            flush_interval=DEFAULT_PLAN_FLUSH_INTERVAL,
            reset_plan=False,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"初始化扫描状态失败: {exc}", file=sys.stderr)
        return 2

    print(f"开始扫描目录: {target_dir}", flush=True)
    print("已启用自动断点续扫：已扫描且未变化的文件将被跳过。")

    try:
        plan, scan_errors = build_modification_plan(
            target_dir=target_dir,
            backup_dir=backup_dir,
            progress_interval=args.progress_interval,
            excluded_dir_names=excluded_dir_names,
            scan_workers=args.scan_workers,
            plan_store=plan_store,
        )
        plan_store.set_status("scan_completed")
        plan_store.flush(force=True)
    except KeyboardInterrupt:
        plan_store.set_status("scan_interrupted")
        plan_store.flush(force=True)
        print("扫描被中断，进度已保存。")
        plan = plan_store.get_plan_items()
        scan_errors = []
    except Exception as exc:  # noqa: BLE001
        plan_store.record_fatal_error(str(exc))
        plan_store.set_status("scan_failed")
        plan_store.flush(force=True)
        print(f"扫描阶段发生未处理异常: {exc}", file=sys.stderr)
        plan = plan_store.get_plan_items()
        scan_errors = [f"扫描阶段发生未处理异常: {exc}"]

    print_error_summary("扫描阶段错误（已跳过）", scan_errors)

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
