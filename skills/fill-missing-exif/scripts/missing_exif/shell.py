"""外部命令执行与编码处理。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Sequence


@dataclass(frozen=True)
class CommandResult:
    """命令执行结果。"""

    stdout: str
    stderr: str


def decode_output(payload: bytes) -> str:
    """将字节流解码为字符串。

    Args:
        payload: 子进程输出字节流。

    Returns:
        str: 解码后的字符串。
    """
    for encoding in ("utf-8", "utf-8-sig", "gbk"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", errors="replace")


def run_command(command: Sequence[str]) -> CommandResult:
    """执行外部命令并在失败时抛出异常。

    Args:
        command: 待执行命令及参数。

    Returns:
        CommandResult: 命令输出。

    Raises:
        RuntimeError: 当命令返回非零退出码时抛出。
    """
    result = subprocess.run(
        command,
        capture_output=True,
        text=False,
        check=False,
    )
    stdout_text = decode_output(result.stdout)
    stderr_text = decode_output(result.stderr)

    if result.returncode != 0:
        command_text = " ".join(command)
        error_text = stderr_text.strip() or stdout_text.strip() or "<无错误输出>"
        raise RuntimeError(f"命令失败: {command_text}\n{error_text}")

    return CommandResult(stdout=stdout_text, stderr=stderr_text)


def exiftool_base_command() -> list[str]:
    """返回 exiftool 命令基础参数。

    Returns:
        list[str]: exiftool 基础命令列表。
    """
    return [
        "exiftool",
        "-charset",
        "filename=utf8",
        "-charset",
        "exiftool=utf8",
    ]


def ensure_exiftool_available() -> None:
    """检查 exiftool 是否可用。

    Raises:
        RuntimeError: 当 exiftool 不可用时抛出。
    """
    command = exiftool_base_command()
    command.append("-ver")
    run_command(command)


def ensure_file_exists(file_path: Path) -> None:
    """检查文件是否存在。

    Args:
        file_path: 文件路径。

    Raises:
        RuntimeError: 文件不存在时抛出。
    """
    if not file_path.exists():
        raise RuntimeError(f"文件不存在: {file_path}")
