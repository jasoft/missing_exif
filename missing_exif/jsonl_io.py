"""JSONL 输入输出辅助函数。"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
import json
from pathlib import Path
import sys
from typing import Generator, Iterator, Mapping, TextIO


@contextmanager
def open_jsonl_reader(path_value: str) -> Generator[TextIO, None, None]:
    """以 JSONL 只读方式打开输入源。

    Args:
        path_value: 输入路径，`-` 代表标准输入。

    Yields:
        TextIO: 文本输入流。
    """
    if path_value == "-":
        with nullcontext(sys.stdin) as handle:
            yield handle
        return

    path = Path(path_value)
    with path.open("r", encoding="utf-8") as handle:
        yield handle


@contextmanager
def open_jsonl_writer(path_value: str) -> Generator[TextIO, None, None]:
    """以 JSONL 写入方式打开输出目标。

    Args:
        path_value: 输出路径，`-` 代表标准输出。

    Yields:
        TextIO: 文本输出流。
    """
    if path_value == "-":
        with nullcontext(sys.stdout) as handle:
            yield handle
        return

    path = Path(path_value)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        yield handle


def iter_jsonl_payloads(reader: TextIO) -> Iterator[tuple[int, dict[str, object]]]:
    """迭代读取 JSONL 数据。

    Args:
        reader: JSONL 输入流。

    Yields:
        Iterator[tuple[int, dict[str, object]]]: (行号, 字典)。
    """
    for line_no, raw_line in enumerate(reader, start=1):
        text = raw_line.strip()
        if not text:
            continue

        payload = json.loads(text)
        if not isinstance(payload, dict):
            continue
        yield line_no, payload


def write_jsonl_payload(writer: TextIO, payload: Mapping[str, object]) -> None:
    """向输出流写入单条 JSONL。

    Args:
        writer: 输出流。
        payload: 可序列化字典。
    """
    writer.write(json.dumps(payload, ensure_ascii=False))
    writer.write("\n")
