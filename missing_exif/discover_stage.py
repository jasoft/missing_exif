"""预扫描阶段：递归发现媒体文件并输出 JSONL。"""

from __future__ import annotations

import os
from pathlib import Path

from .common import (
    DiscoverItem,
    detect_media_kind,
    log_info,
    safe_relative_path,
    safe_resolve_path,
    should_skip_media_file,
    should_skip_walk_directory,
)
from .jsonl_io import open_jsonl_writer, write_jsonl_payload


def discover_media_to_jsonl(
    target_dir: Path,
    backup_dir: Path,
    excluded_dir_names: set[str],
    output_path: str,
) -> tuple[int, int, int]:
    """执行预扫描并将结果写入 JSONL。

    Args:
        target_dir: 扫描根目录。
        backup_dir: 备份目录。
        excluded_dir_names: 被排除目录名集合。
        output_path: JSONL 输出路径，`-` 表示标准输出。

    Returns:
        tuple[int, int, int]: (目录数, 文件数, 媒体候选数)。
    """
    visited_real_dirs: set[str] = set()
    visited_dir_count = 0
    inspected_file_count = 0
    media_candidate_count = 0

    with open_jsonl_writer(output_path) as writer:
        for root_dir, dir_names, file_names in os.walk(target_dir, followlinks=True):
            root_path = Path(root_dir)
            visited_dir_count += 1

            root_real_path = safe_resolve_path(root_path)
            root_real_key = str(root_real_path)
            if root_real_key in visited_real_dirs:
                dir_names[:] = []
                continue
            visited_real_dirs.add(root_real_key)

            relative_dir = safe_relative_path(root_path, target_dir)
            log_info(
                "[预扫描进度] "
                f"目录: {visited_dir_count} | "
                f"文件: {inspected_file_count} | "
                f"媒体候选: {media_candidate_count} | "
                f"当前目录: {relative_dir}"
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

                relative_path = safe_relative_path(file_path, target_dir)
                item = DiscoverItem(
                    file_path=file_path,
                    relative_path=relative_path,
                    media_kind=media_kind,
                )
                write_jsonl_payload(writer, item.to_dict())
                media_candidate_count += 1

                if media_candidate_count % 200 == 0:
                    writer.flush()

        writer.flush()

    log_info(
        "[预扫描完成] "
        f"目录: {visited_dir_count} | "
        f"文件: {inspected_file_count} | "
        f"媒体候选: {media_candidate_count}"
    )
    return visited_dir_count, inspected_file_count, media_candidate_count
