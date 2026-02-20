"""筛选阶段：在预扫描结果中找出缺失元数据的文件。"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import json
from pathlib import Path
from typing import Sequence

from .common import (
    DiscoverItem,
    IMAGE_METADATA_TAGS,
    MediaKind,
    PlanItem,
    VIDEO_METADATA_TAGS,
    format_exif_time,
    log_info,
)
from .jsonl_io import open_jsonl_reader, open_jsonl_writer, write_jsonl_payload
from .shell import exiftool_base_command, run_command

FilterResult = tuple[DiscoverItem, PlanItem | None, str | None]


def read_selected_tags(file_path: Path, tags: Sequence[str]) -> dict[str, object]:
    """读取文件中的指定元数据标签。

    Args:
        file_path: 待读取文件路径。
        tags: 需要读取的标签列表。

    Returns:
        dict[str, object]: 标签与值映射。

    Raises:
        RuntimeError: exiftool 返回异常或 JSON 解析失败时抛出。
    """
    command = exiftool_base_command()
    command.extend(["-j", "-n", "-G1"])
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
    if not isinstance(row, dict):
        return {}
    return row


def has_existing_metadata(file_path: Path, media_kind: MediaKind) -> bool:
    """判断文件是否已存在媒体元数据。

    Args:
        file_path: 文件路径。
        media_kind: 媒体类型。

    Returns:
        bool: 若存在任意 EXIF/XMP/QuickTime/Keys/IPTC 标签则返回 True。
    """
    tags = IMAGE_METADATA_TAGS if media_kind == "image" else VIDEO_METADATA_TAGS
    metadata = read_selected_tags(file_path, tags)

    for key, value in metadata.items():
        if key == "SourceFile":
            continue
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return True

    return False


def inspect_discover_item(
    item: DiscoverItem,
    backup_dir: Path,
) -> FilterResult:
    """检查单个媒体文件是否需要写入元数据。

    Args:
        item: 预扫描阶段输出的记录。
        backup_dir: 备份目录。

    Returns:
        FilterResult: (输入项, 计划项或 None, 错误信息或 None)。
    """
    try:
        if has_existing_metadata(item.file_path, item.media_kind):
            return item, None, None

        exif_time, iso_time = format_exif_time(item.file_path)
        plan_item = PlanItem(
            file_path=item.file_path,
            relative_path=item.relative_path,
            media_kind=item.media_kind,
            exif_time=exif_time,
            iso_time=iso_time,
            backup_path=backup_dir / item.relative_path,
        )
        return item, plan_item, None
    except Exception as exc:  # noqa: BLE001
        return item, None, f"{item.file_path}: {exc}"


def load_discover_items(input_path: str) -> tuple[list[DiscoverItem], list[str]]:
    """从 JSONL 读取预扫描结果。

    Args:
        input_path: 输入路径，`-` 代表标准输入。

    Returns:
        tuple[list[DiscoverItem], list[str]]: 有效记录与解析错误列表。
    """
    items: list[DiscoverItem] = []
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
                items.append(DiscoverItem.from_dict(payload))
            except ValueError as exc:
                errors.append(f"第 {line_no} 行记录无效: {exc}")

    return items, errors


def should_report_progress(index: int, total: int, interval: int) -> bool:
    """判断是否应输出筛选进度。

    Args:
        index: 当前处理序号（从 1 开始）。
        total: 总数。
        interval: 进度间隔。

    Returns:
        bool: 需要输出返回 True。
    """
    if total <= 0:
        return False
    return index == 1 or index == total or index % interval == 0


def refill_tasks(
    executor: ThreadPoolExecutor,
    future_map: dict[Future[FilterResult], DiscoverItem],
    pending_items: list[DiscoverItem],
    backup_dir: Path,
    inflight_limit: int,
) -> None:
    """补充并发筛选任务到指定上限。

    Args:
        executor: 线程池执行器。
        future_map: future 映射表。
        pending_items: 待处理项。
        backup_dir: 备份目录。
        inflight_limit: 最大并发队列长度。
    """
    while pending_items and len(future_map) < inflight_limit:
        item = pending_items.pop()
        future = executor.submit(inspect_discover_item, item, backup_dir)
        future_map[future] = item


def filter_discover_to_plan_jsonl(
    input_path: str,
    output_path: str,
    target_dir: Path,
    backup_dir: Path,
    worker_count: int,
    progress_interval: int,
) -> tuple[int, int, list[str]]:
    """执行筛选阶段，并输出待写回计划。

    Args:
        input_path: 预扫描结果 JSONL 输入路径。
        output_path: 待写回计划 JSONL 输出路径。
        target_dir: 扫描根目录。
        backup_dir: 备份目录。
        worker_count: 并发线程数。
        progress_interval: 进度输出间隔。

    Returns:
        tuple[int, int, list[str]]: (已处理总数, 待写回数量, 错误列表)。
    """
    items, parse_errors = load_discover_items(input_path)
    total_items = len(items)

    log_info(f"筛选输入记录: {total_items}")
    if parse_errors:
        log_info(f"筛选输入解析错误: {len(parse_errors)}")

    if total_items == 0:
        with open_jsonl_writer(output_path) as writer:
            writer.flush()
        return 0, 0, parse_errors

    pending_items = list(items)
    pending_items.reverse()
    effective_workers = max(worker_count, 1)
    inflight_limit = max(effective_workers * 4, 1)
    effective_interval = max(progress_interval, 1)

    processed_count = 0
    pending_count = 0
    errors = list(parse_errors)
    future_map: dict[Future[FilterResult], DiscoverItem] = {}

    with open_jsonl_writer(output_path) as writer:
        with ThreadPoolExecutor(max_workers=effective_workers) as executor:
            refill_tasks(
                executor=executor,
                future_map=future_map,
                pending_items=pending_items,
                backup_dir=backup_dir,
                inflight_limit=inflight_limit,
            )

            while future_map:
                completed_futures, _ = wait(
                    set(future_map),
                    return_when=FIRST_COMPLETED,
                )

                for future in completed_futures:
                    fallback_item = future_map.pop(future)
                    try:
                        checked_item, plan_item, error_message = future.result()
                    except Exception as exc:  # noqa: BLE001
                        checked_item = fallback_item
                        plan_item = None
                        error_message = f"{fallback_item.file_path}: 线程筛选异常: {exc}"

                    processed_count += 1
                    if error_message:
                        errors.append(error_message)
                    if plan_item is not None:
                        write_jsonl_payload(writer, plan_item.to_dict())
                        pending_count += 1

                    if processed_count % 200 == 0:
                        writer.flush()

                    if should_report_progress(
                        processed_count,
                        total_items,
                        effective_interval,
                    ):
                        percent = (processed_count / total_items) * 100
                        log_info(
                            "[筛选进度] "
                            f"{processed_count}/{total_items} ({percent:.1f}%) | "
                            f"当前: {checked_item.relative_path} | "
                            f"待写回: {pending_count}"
                        )

                refill_tasks(
                    executor=executor,
                    future_map=future_map,
                    pending_items=pending_items,
                    backup_dir=backup_dir,
                    inflight_limit=inflight_limit,
                )

        writer.flush()

    log_info(
        "[筛选完成] "
        f"已处理: {processed_count} | "
        f"待写回: {pending_count} | "
        f"错误: {len(errors)}"
    )
    return processed_count, pending_count, errors
