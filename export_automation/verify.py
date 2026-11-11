"""下载文件首行元数据核对与异常隔离。

门户下载的 CSV 首行是一段 JSON，含 ``job_id``、``filter_digest``、
``schema_version``。文件名恒为“结算资料.csv”且不唯一，因此**禁止按文件名认领**，
只能按首行元数据与产生它的任务/筛选摘要绑定。
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .models import SCHEMA_VERSION


class MetadataError(ValueError):
    """首行元数据无法解析或字段类型错误。"""


@dataclass(frozen=True)
class FileMetadata:
    job_id: str
    filter_digest: str
    schema_version: int
    raw_first_line: str

    @property
    def digest(self) -> str:
        return self.filter_digest


def read_metadata(path: Path) -> FileMetadata:
    """只读取并解析首行元数据，不假设后续行的业务结构。"""
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            first_line = handle.readline().rstrip("\r\n")
    except OSError as exc:
        raise MetadataError(f"无法读取下载文件: {exc}") from exc
    if not first_line:
        raise MetadataError("下载文件为空，缺少元数据首行")
    try:
        payload: Any = json.loads(first_line)
    except json.JSONDecodeError as exc:
        raise MetadataError(f"首行不是合法 JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise MetadataError("首行元数据必须是 JSON 对象")
    try:
        job_id = payload["job_id"]
        filter_digest = payload["filter_digest"]
        schema_version = payload["schema_version"]
    except KeyError as exc:
        raise MetadataError(f"首行元数据缺少字段: {exc.args[0]}") from exc
    if not isinstance(job_id, str) or not job_id:
        raise MetadataError("job_id 必须是非空字符串")
    if not isinstance(filter_digest, str) or not filter_digest:
        raise MetadataError("filter_digest 必须是非空字符串")
    # bool 是 int 的子类，显式排除，防止 true/false 混过版本号校验。
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise MetadataError("schema_version 必须是整数")
    return FileMetadata(job_id, filter_digest, schema_version, first_line)


def check_metadata(
    path: Path,
    *,
    expected_job_id: str,
    expected_digest: str,
) -> Optional[str]:
    """核对元数据，返回 ``None`` 表示通过，否则返回人类可读的隔离原因。"""
    try:
        meta = read_metadata(path)
    except MetadataError as exc:
        return f"元数据解析失败: {exc}"
    problems: list[str] = []
    if meta.job_id != expected_job_id:
        problems.append(
            f"job_id 不符: 文件={meta.job_id!r} 期望={expected_job_id!r}"
        )
    if meta.filter_digest != expected_digest:
        problems.append(
            f"filter_digest 不符: 文件={meta.filter_digest!r} 期望={expected_digest!r}"
        )
    if meta.schema_version != SCHEMA_VERSION:
        problems.append(
            f"schema_version 不符: 文件={meta.schema_version} 期望={SCHEMA_VERSION}"
        )
    return "；".join(problems) if problems else None


def quarantine(path: Path, quarantine_dir: Path, reason: str) -> Path:
    """把异常文件移入隔离目录并写入同名原因说明，原路径不再保留。

    隔离文件名带任务编号与内容哈希前缀，避免不同异常任务互相覆盖；
    正常输出目录永远不会收到这个文件。
    """
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    digest = _short_hash(path)
    target = quarantine_dir / f"{path.stem}.{digest}{path.suffix}"
    counter = 1
    while target.exists():
        target = quarantine_dir / f"{path.stem}.{digest}.{counter}{path.suffix}"
        counter += 1
    shutil.move(str(path), str(target))
    reason_path = target.with_suffix(target.suffix + ".reason.txt")
    reason_path.write_text(reason + "\n", encoding="utf-8")
    return target


def accept(path: Path, output_dir: Path, request_id: str, job_id: str) -> Path:
    """校验通过后才允许进入正常输出目录，文件名绑定输入条件与任务编号。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{request_id}__{job_id}{path.suffix}"
    if target.exists():
        # 恢复运行时同一任务已落过盘：内容应一致，覆盖前先确认不是另一个文件。
        target.unlink()
    shutil.move(str(path), str(target))
    return target


def _short_hash(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:12]
