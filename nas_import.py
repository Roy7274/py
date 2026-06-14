#!/usr/bin/env python3
"""Import annotated NAS/MinIO image URLs into Cobra-friendly records.

The script is intentionally standalone. By default it performs a dry run:
it traverses a NAS-like source, pairs images with VOC XML files, parses labels,
and prints a report. It does not upload files to MinIO.
"""

from __future__ import annotations

import argparse
import atexit
import email.utils
import html.parser
import json
import os
import posixpath
import re
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp"}
XML_SUFFIX = ".xml"
IGNORED_PAIR_DIRS = {"jpg", "jpeg", "image", "images", "img", "xml", "annotation", "annotations", "label", "labels"}
MAX_DATASET_ADDRESS_LENGTH = 200
MAX_TASK_NAME_LENGTH = 16
MAX_DATASET_NAME_LENGTH = 16
MAX_TASK_DESCRIPTION_LENGTH = 500
IMPORT_TASK_PREFIX = "数据导入-"
SCAN_PROGRESS_EVERY = 500
XML_PROGRESS_EVERY = 50
CHECKPOINT_VERSION = 1
DEFAULT_CHECKPOINT_FILE = "nas_import_checkpoint.json"
INSERT_COMMIT_EVERY = 500
DUPLICATE_CHECK_PROGRESS_EVERY = 20
HTTP_TIMEOUT_SECONDS = 60
HTTP_RETRY_COUNT = 3
HTTP_RETRY_DELAY_SECONDS = 2.0
HTTP_SKIPPABLE_STATUS_CODES = {401, 403, 404, 410}

def format_failure_message(exc: Exception) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    cause = exc.__cause__
    if cause is not None:
        cause_text = str(cause).strip() or cause.__class__.__name__
        if cause_text and cause_text not in message:
            message = f"{message} | 原因: {cause_text}"
    return message


class AllPairsAlreadyImported(Exception):
    def __init__(self, skipped_duplicate_count: int) -> None:
        self.skipped_duplicate_count = skipped_duplicate_count
        super().__init__(f"本批次 {skipped_duplicate_count} 张图片均已存在于 raw_dataset_detail，无需重复写入")

_ACTIVE_CHECKPOINT_FILE: Path | None = None
_GUARD_CHECKPOINT: tuple[str, Any] | None = None
_last_checkpoint_save_at = 0.0
CHECKPOINT_MIN_SAVE_INTERVAL_SECONDS = 1.0


def add_vendor_path() -> None:
    vendor_dir = Path(__file__).resolve().parent / "vendor"
    if vendor_dir.exists():
        vendor_path = str(vendor_dir)
        if vendor_path not in sys.path:
            sys.path.insert(0, vendor_path)


add_vendor_path()


@dataclass(frozen=True)
class SourceFile:
    name: str
    url: str
    local_path: str | None = None

    def to_dict(self) -> dict:
        return {"name": self.name, "url": self.url, "local_path": self.local_path}


@dataclass(frozen=True)
class ImportOptions:
    dataset_name: str | None
    business_domain: str
    business_scenario: str
    created_by: int
    executor: int
    task_name: str | None
    task_description: str | None
    modality: str | None
    max_import_count: int | None = None
    allow_duplicates: bool = False


@dataclass(frozen=True)
class DbOptions:
    host: str
    port: int
    database: str
    user: str
    password: str
    charset: str = "utf8mb4"
    write_delay_every: int = 100
    write_delay_seconds: float = 0.0
    progress_every: int = 500
    failure_status: str = "reject"


@dataclass(frozen=True)
class HttpFileDirectory:
    relative_dir: str
    url: str
    file_count: int


@dataclass(frozen=True)
class SplitDirPair:
    image_dir: HttpFileDirectory
    xml_dir: HttpFileDirectory


@dataclass(frozen=True)
class SplitDirBatch:
    index: int
    dir_pairs: list[SplitDirPair]
    estimated_image_count: int


@dataclass
class HttpDirListing:
    subdirs: dict[str, str]
    image_count: int
    xml_count: int


@dataclass
class SyncDfsState:
    image_root_url: str
    xml_root_url: str
    scanned_dirs: int = 0
    paired_dirs: int = 0
    unmatched_image_dirs: int = 0
    unmatched_xml_dirs: int = 0
    skipped_dirs: int = 0


def split_dir_pair_to_dict(pair: SplitDirPair) -> dict:
    return {
        "imageDir": {
            "relativeDir": pair.image_dir.relative_dir,
            "url": pair.image_dir.url,
            "fileCount": pair.image_dir.file_count,
        },
        "xmlDir": {
            "relativeDir": pair.xml_dir.relative_dir,
            "url": pair.xml_dir.url,
            "fileCount": pair.xml_dir.file_count,
        },
    }


def split_dir_pair_from_dict(data: dict) -> SplitDirPair:
    image_dir = data.get("imageDir") or {}
    xml_dir = data.get("xmlDir") or {}
    return SplitDirPair(
        image_dir=HttpFileDirectory(
            relative_dir=str(image_dir.get("relativeDir") or ""),
            url=str(image_dir.get("url") or ""),
            file_count=int(image_dir.get("fileCount") or 0),
        ),
        xml_dir=HttpFileDirectory(
            relative_dir=str(xml_dir.get("relativeDir") or ""),
            url=str(xml_dir.get("url") or ""),
            file_count=int(xml_dir.get("fileCount") or 0),
        ),
    )


def configure_http_client(timeout_seconds: int, retry_count: int, retry_delay_seconds: float) -> None:
    global HTTP_TIMEOUT_SECONDS, HTTP_RETRY_COUNT, HTTP_RETRY_DELAY_SECONDS
    HTTP_TIMEOUT_SECONDS = timeout_seconds
    HTTP_RETRY_COUNT = retry_count
    HTTP_RETRY_DELAY_SECONDS = retry_delay_seconds


def default_checkpoint_path() -> Path:
    return Path(__file__).resolve().parent / DEFAULT_CHECKPOINT_FILE


def resolve_checkpoint_path(checkpoint_path: str | None) -> Path:
    if checkpoint_path:
        return Path(checkpoint_path).expanduser().resolve()
    return default_checkpoint_path()


def batch_files_sidecar(checkpoint_file: Path, batch_index: int) -> Path:
    return checkpoint_file.parent / f"{checkpoint_file.stem}.batch_{batch_index:04d}.files.jsonl"


def parsed_cache_sidecar(checkpoint_file: Path, batch_index: int) -> Path:
    return checkpoint_file.parent / f"{checkpoint_file.stem}.batch_{batch_index:04d}.parsed.jsonl"


def append_jsonl_lines(path: Path, payloads: Iterable[dict]) -> int:
    items = list(payloads)
    if not items:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        for payload in items:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return len(items)


def append_jsonl_line(path: Path, payload: dict) -> None:
    append_jsonl_lines(path, [payload])


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    items: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped:
            items.append(json.loads(stripped))
    return items


def remove_checkpoint_artifacts(checkpoint_file: Path) -> None:
    checkpoint_file.unlink(missing_ok=True)
    checkpoint_file.with_suffix(checkpoint_file.suffix + ".tmp").unlink(missing_ok=True)
    for path in checkpoint_file.parent.glob(f"{checkpoint_file.stem}.batch_*.files.jsonl"):
        path.unlink(missing_ok=True)
    for path in checkpoint_file.parent.glob(f"{checkpoint_file.stem}.batch_*.parsed.jsonl"):
        path.unlink(missing_ok=True)


def load_batch_files_from_sidecar(checkpoint_file: Path, batch_index: int) -> list[SourceFile]:
    return [
        SourceFile(
            name=str(item.get("name") or ""),
            url=str(item.get("url") or ""),
            local_path=item.get("local_path"),
        )
        for item in read_jsonl(batch_files_sidecar(checkpoint_file, batch_index))
    ]


def load_parsed_xml_cache(checkpoint_file: Path, batch_index: int) -> dict[str, dict]:
    cache: dict[str, dict] = {}
    for item in read_jsonl(parsed_cache_sidecar(checkpoint_file, batch_index)):
        xml_url = str(item.get("url") or "")
        if xml_url:
            cache[xml_url] = item
    return cache


def load_checkpoint_sidecars(checkpoint_file: Path, checkpoint: ImportCheckpoint) -> None:
    if checkpoint.active_batch_index is None:
        return
    if not checkpoint.active_batch_files:
        checkpoint.active_batch_files = read_jsonl(
            batch_files_sidecar(checkpoint_file, checkpoint.active_batch_index)
        )
    if not checkpoint.parsed_xml_urls:
        legacy_parsed = checkpoint.legacy_parsed_xml_by_url
        if legacy_parsed:
            checkpoint.parsed_xml_urls = set(legacy_parsed.keys())
        else:
            checkpoint.parsed_xml_urls = {
                str(item.get("url") or "")
                for item in read_jsonl(parsed_cache_sidecar(checkpoint_file, checkpoint.active_batch_index))
                if item.get("url")
            }


def print_checkpoint_artifacts(checkpoint_path: str | None) -> None:
    checkpoint_file = resolve_checkpoint_path(checkpoint_path)
    if checkpoint_file.exists():
        print_progress(f"检查点已更新：{checkpoint_file} ({checkpoint_file.stat().st_size} 字节)")
    else:
        print_progress(f"检查点尚未生成：{checkpoint_file}")
        return
    for sidecar in sorted(checkpoint_file.parent.glob(f"{checkpoint_file.stem}.batch_*")):
        if sidecar.is_file():
            print_progress(f"  侧车文件：{sidecar} ({sidecar.stat().st_size} 字节)")


def cleanup_batch_sidecars(checkpoint_file: Path, batch_index: int) -> None:
    batch_files_sidecar(checkpoint_file, batch_index).unlink(missing_ok=True)
    parsed_cache_sidecar(checkpoint_file, batch_index).unlink(missing_ok=True)


def install_checkpoint_guard(checkpoint_path: str, checkpoint: ImportCheckpoint) -> None:
    global _GUARD_CHECKPOINT, _ACTIVE_CHECKPOINT_FILE
    resolved = resolve_checkpoint_path(checkpoint_path)
    _ACTIVE_CHECKPOINT_FILE = resolved
    _GUARD_CHECKPOINT = (str(resolved), checkpoint)
    atexit.register(_flush_checkpoint_guard)
    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _checkpoint_signal_handler)
        except (AttributeError, ValueError, OSError):
            continue


def clear_checkpoint_guard() -> None:
    global _GUARD_CHECKPOINT, _ACTIVE_CHECKPOINT_FILE
    _GUARD_CHECKPOINT = None
    _ACTIVE_CHECKPOINT_FILE = None


def _flush_checkpoint_guard() -> None:
    if _GUARD_CHECKPOINT is None:
        return
    path, checkpoint = _GUARD_CHECKPOINT
    try:
        save_import_checkpoint(path, checkpoint, force=True)
    except OSError as exc:
        print_progress(f"退出时检查点写入失败：{path} ({exc})")


def _checkpoint_signal_handler(signum: int, frame: Any) -> None:
    _flush_checkpoint_guard()
    print_progress(f"收到中断信号，检查点已保存：{signum}")
    raise SystemExit(128 + signum if signum < 128 else 1)


def load_import_checkpoint(
    checkpoint_path: str | None,
    image_url: str,
    xml_url: str,
    batch_size: int,
    reset: bool = False,
) -> ImportCheckpoint:
    checkpoint_file = resolve_checkpoint_path(checkpoint_path)
    if reset:
        remove_checkpoint_artifacts(checkpoint_file)
    if checkpoint_file.exists():
        payload = json.loads(checkpoint_file.read_text(encoding="utf-8"))
        checkpoint = ImportCheckpoint.from_dict(payload)
        if (
            checkpoint.image_url != image_url
            or checkpoint.xml_url != xml_url
            or checkpoint.batch_size != batch_size
        ):
            raise ValueError("检查点与当前 --image-url/--xml-url/--batch-size 不一致，请更换 --checkpoint-file 或添加 --reset-checkpoint")
        load_checkpoint_sidecars(checkpoint_file, checkpoint)
        print_progress(
            f"从检查点恢复：已访问目录 {len(checkpoint.visited_relative_paths)}，"
            f"待扫描栈 {len(checkpoint.dfs_stack)}，已完成批次 {checkpoint.completed_batch_indexes}"
        )
        if checkpoint.active_batch_index is not None:
            print_progress(
                f"检查点批次进度：批次 {checkpoint.active_batch_index}，"
                f"阶段 {checkpoint.phase or 'unknown'}，"
                f"文件扫描完成 {checkpoint.file_scan_completed}，"
                f"已扫目录 {len(checkpoint.active_batch_scanned_dirs)}，"
                f"已发现文件 {checkpoint.active_batch_file_count or len(checkpoint.active_batch_files)}，"
                f"已解析 XML {len(checkpoint.parsed_xml_urls)}"
            )
        print_checkpoint_artifacts(str(checkpoint_file))
        if checkpoint.last_error:
            print_progress(f"检查点上次错误：{checkpoint.last_error}")
        return checkpoint
    return ImportCheckpoint.new(image_url, xml_url, batch_size)


def save_import_checkpoint(
    checkpoint_path: str | None,
    checkpoint: ImportCheckpoint,
    *,
    force: bool = False,
) -> None:
    global _last_checkpoint_save_at
    checkpoint_file = resolve_checkpoint_path(checkpoint_path)
    now = time.monotonic()
    if not force and (now - _last_checkpoint_save_at) < CHECKPOINT_MIN_SAVE_INTERVAL_SECONDS:
        return
    checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
    temp_path = checkpoint_file.with_suffix(checkpoint_file.suffix + ".tmp")
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(checkpoint.to_dict(), ensure_ascii=False, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(checkpoint_file)
        _last_checkpoint_save_at = now
    except OSError as exc:
        print_progress(f"检查点写入失败：{checkpoint_file} ({exc})")
        raise


@dataclass
class ImportCheckpoint:
    image_url: str
    xml_url: str
    batch_size: int
    visited_relative_paths: set[str]
    skipped_relative_paths: set[str]
    dfs_stack: list[tuple[str, str, str]]
    completed_batch_indexes: list[int]
    batch_index: int
    pending_pairs: list[SplitDirPair]
    pending_image_count: int
    scanned_dirs: int
    paired_dirs: int
    unmatched_image_dirs: int
    unmatched_xml_dirs: int
    active_batch_index: int | None
    active_batch_scanned_dirs: set[str]
    active_batch_files: list[dict]
    parsed_xml_urls: set[str]
    phase: str = "dfs"
    file_scan_completed: bool = False
    active_batch_file_count: int = 0
    legacy_parsed_xml_by_url: dict[str, dict] | None = None
    db_write_task_id: int | None = None
    db_write_dataset_id: int | None = None
    db_write_inserted_count: int = 0
    parsed_xml_pending: list[dict] = field(default_factory=list, repr=False)
    last_error: str | None = None

    @classmethod
    def new(cls, image_url: str, xml_url: str, batch_size: int) -> ImportCheckpoint:
        return cls(
            image_url=image_url,
            xml_url=xml_url,
            batch_size=batch_size,
            visited_relative_paths=set(),
            skipped_relative_paths=set(),
            dfs_stack=[],
            completed_batch_indexes=[],
            batch_index=0,
            pending_pairs=[],
            pending_image_count=0,
            scanned_dirs=0,
            paired_dirs=0,
            unmatched_image_dirs=0,
            unmatched_xml_dirs=0,
            active_batch_index=None,
            active_batch_scanned_dirs=set(),
            active_batch_files=[],
            parsed_xml_urls=set(),
        )

    def suggest_start_batch(self) -> int:
        if not self.completed_batch_indexes:
            return 1
        return max(self.completed_batch_indexes) + 1

    def mark_dir_visited(self, relative_path: str) -> None:
        self.visited_relative_paths.add(relative_path)

    def mark_dir_skipped(self, relative_path: str) -> None:
        self.mark_dir_visited(relative_path)
        self.skipped_relative_paths.add(relative_path)

    def is_dir_visited(self, relative_path: str) -> bool:
        return relative_path in self.visited_relative_paths

    def mark_batch_completed(self, batch_index: int) -> None:
        if batch_index not in self.completed_batch_indexes:
            self.completed_batch_indexes.append(batch_index)
        self.flush_parsed_xml_sidecar()
        if _ACTIVE_CHECKPOINT_FILE is not None:
            cleanup_batch_sidecars(_ACTIVE_CHECKPOINT_FILE, batch_index)
        self.active_batch_index = None
        self.active_batch_scanned_dirs = set()
        self.active_batch_files = []
        self.parsed_xml_urls = set()
        self.parsed_xml_pending = []
        self.phase = "dfs"
        self.file_scan_completed = False
        self.active_batch_file_count = 0
        self.db_write_task_id = None
        self.db_write_dataset_id = None
        self.db_write_inserted_count = 0
        self.clear_db_write()
        self.last_error = None

    def begin_active_batch(self, batch_index: int) -> None:
        if self.active_batch_index == batch_index:
            return
        if _ACTIVE_CHECKPOINT_FILE is not None:
            cleanup_batch_sidecars(_ACTIVE_CHECKPOINT_FILE, batch_index)
        self.active_batch_index = batch_index
        self.active_batch_scanned_dirs = set()
        self.active_batch_files = []
        self.parsed_xml_urls = set()
        self.parsed_xml_pending = []
        self.phase = "batch_files"
        self.file_scan_completed = False
        self.active_batch_file_count = 0
        self.db_write_task_id = None
        self.db_write_dataset_id = None
        self.db_write_inserted_count = 0

    def mark_batch_file_scan_completed(self, file_count: int) -> None:
        self.phase = "xml_parse"
        self.file_scan_completed = True
        self.active_batch_file_count = file_count

    def begin_db_write(self, task_id: int, dataset_id: int) -> None:
        self.phase = "db_write"
        self.db_write_task_id = task_id
        self.db_write_dataset_id = dataset_id
        self.db_write_inserted_count = 0

    def update_db_write_progress(self, inserted_count: int) -> None:
        self.db_write_inserted_count = inserted_count

    def clear_db_write(self) -> None:
        self.db_write_task_id = None
        self.db_write_dataset_id = None
        self.db_write_inserted_count = 0

    def mark_active_batch_dir_scanned(self, relative_dir: str, files: list[SourceFile]) -> None:
        self.active_batch_scanned_dirs.add(relative_dir)
        file_dicts = [file.to_dict() for file in files]
        self.active_batch_files.extend(file_dicts)
        if _ACTIVE_CHECKPOINT_FILE is not None and self.active_batch_index is not None and file_dicts:
            append_jsonl_lines(
                batch_files_sidecar(_ACTIVE_CHECKPOINT_FILE, self.active_batch_index),
                file_dicts,
            )

    def is_active_batch_dir_scanned(self, relative_dir: str) -> bool:
        return relative_dir in self.active_batch_scanned_dirs

    def has_parsed_xml(self, xml_url: str) -> bool:
        return xml_url in self.parsed_xml_urls

    def mark_parsed_xml(self, xml_url: str, parsed_xml: dict) -> None:
        self.parsed_xml_urls.add(xml_url)
        if _ACTIVE_CHECKPOINT_FILE is not None and self.active_batch_index is not None:
            self.parsed_xml_pending.append(parsed_xml)

    def flush_parsed_xml_sidecar(self) -> int:
        if _ACTIVE_CHECKPOINT_FILE is None or self.active_batch_index is None or not self.parsed_xml_pending:
            return 0
        written = append_jsonl_lines(
            parsed_cache_sidecar(_ACTIVE_CHECKPOINT_FILE, self.active_batch_index),
            self.parsed_xml_pending,
        )
        self.parsed_xml_pending.clear()
        return written

    def record_error(self, error: Exception | str) -> None:
        self.last_error = str(error)

    def to_dict(self) -> dict:
        return {
            "version": CHECKPOINT_VERSION,
            "imageUrl": self.image_url,
            "xmlUrl": self.xml_url,
            "batchSize": self.batch_size,
            "visitedRelativePaths": sorted(self.visited_relative_paths),
            "skippedRelativePaths": sorted(self.skipped_relative_paths),
            "dfsStack": [
                {"imageDirUrl": image_url, "xmlDirUrl": xml_url, "relativePath": relative_path}
                for image_url, xml_url, relative_path in self.dfs_stack
            ],
            "completedBatchIndexes": sorted(self.completed_batch_indexes),
            "accumulator": {
                "batchIndex": self.batch_index,
                "pendingPairs": [split_dir_pair_to_dict(pair) for pair in self.pending_pairs],
                "pendingImageCount": self.pending_image_count,
            },
            "stats": {
                "scannedDirs": self.scanned_dirs,
                "pairedDirs": self.paired_dirs,
                "unmatchedImageDirs": self.unmatched_image_dirs,
                "unmatchedXmlDirs": self.unmatched_xml_dirs,
                "skippedDirs": len(self.skipped_relative_paths),
            },
            "activeBatch": {
                "batchIndex": self.active_batch_index,
                "phase": self.phase,
                "fileScanCompleted": self.file_scan_completed,
                "scannedDirRelativePaths": sorted(self.active_batch_scanned_dirs),
                "fileCount": self.active_batch_file_count or len(self.active_batch_files),
                "parsedXmlCount": len(self.parsed_xml_urls),
                "dbWrite": {
                    "taskId": self.db_write_task_id,
                    "datasetId": self.db_write_dataset_id,
                    "insertedCount": self.db_write_inserted_count,
                },
            },
            "lastError": self.last_error,
            "updatedAt": format_db_timestamp(),
        }

    @classmethod
    def from_dict(cls, payload: dict) -> ImportCheckpoint:
        accumulator = payload.get("accumulator") or {}
        stats = payload.get("stats") or {}
        active_batch = payload.get("activeBatch") or {}
        dfs_stack = [
            (frame["imageDirUrl"], frame["xmlDirUrl"], frame.get("relativePath", ""))
            for frame in payload.get("dfsStack") or []
        ]
        legacy_parsed = dict(active_batch.get("parsedXmlByUrl") or {})
        parsed_xml_urls = {
            str(url)
            for url in (active_batch.get("parsedXmlUrls") or [])
            if url
        }
        if legacy_parsed:
            parsed_xml_urls.update(legacy_parsed.keys())
        db_write = active_batch.get("dbWrite") or {}
        return cls(
            image_url=str(payload.get("imageUrl") or ""),
            xml_url=str(payload.get("xmlUrl") or ""),
            batch_size=int(payload.get("batchSize") or 0),
            visited_relative_paths=set(payload.get("visitedRelativePaths") or []),
            skipped_relative_paths=set(payload.get("skippedRelativePaths") or []),
            dfs_stack=dfs_stack,
            completed_batch_indexes=[int(value) for value in payload.get("completedBatchIndexes") or []],
            batch_index=int(accumulator.get("batchIndex") or 0),
            pending_pairs=[split_dir_pair_from_dict(item) for item in accumulator.get("pendingPairs") or []],
            pending_image_count=int(accumulator.get("pendingImageCount") or 0),
            scanned_dirs=int(stats.get("scannedDirs") or 0),
            paired_dirs=int(stats.get("pairedDirs") or 0),
            unmatched_image_dirs=int(stats.get("unmatchedImageDirs") or 0),
            unmatched_xml_dirs=int(stats.get("unmatchedXmlDirs") or 0),
            active_batch_index=active_batch.get("batchIndex"),
            active_batch_scanned_dirs=set(active_batch.get("scannedDirRelativePaths") or []),
            active_batch_files=list(active_batch.get("files") or []),
            parsed_xml_urls=parsed_xml_urls,
            phase=str(active_batch.get("phase") or "dfs"),
            file_scan_completed=bool(active_batch.get("fileScanCompleted")),
            active_batch_file_count=int(active_batch.get("fileCount") or 0),
            legacy_parsed_xml_by_url=legacy_parsed or None,
            db_write_task_id=db_write.get("taskId"),
            db_write_dataset_id=db_write.get("datasetId"),
            db_write_inserted_count=int(db_write.get("insertedCount") or 0),
            last_error=payload.get("lastError"),
        )


class DirectoryLinkParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for key, value in attrs:
            if key.lower() == "href" and value:
                self.links.append(value)


def print_progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def extract_http_status(exc: BaseException | None) -> int | None:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, urllib.error.HTTPError):
            return int(current.code)
        current = current.__cause__
    return None


def format_directory_scan_error(exc: Exception | str) -> str:
    if isinstance(exc, str):
        return exc
    status = extract_http_status(exc)
    if status is not None:
        return f"HTTP {status}"
    message = str(exc).strip()
    return message or exc.__class__.__name__


def is_skippable_directory_error(exc: Exception) -> bool:
    status = extract_http_status(exc)
    if status is not None and status in HTTP_SKIPPABLE_STATUS_CODES:
        return True
    message = str(exc).lower()
    for token in ("403", "401", "404", "forbidden", "unauthorized", "not found"):
        if token in message:
            return True
    return False


def handle_skippable_directory_scan_failure(
    relative_path: str,
    exc: Exception,
    *,
    phase: str,
) -> None:
    current_path = relative_path or "."
    print_progress(
        f"{phase}跳过不可访问目录：{current_path}（{format_directory_scan_error(exc)}，"
        f"已重试 {HTTP_RETRY_COUNT} 次）"
    )


def normalize_pair_key(name: str) -> str:
    """Build a tolerant pairing key for image/XML names.

    The field team data has small differences between image and XML names:
    separators differ, a photographer/name segment may differ, and tower/date
    separators may be inconsistent. The key keeps stable business parts and
    removes the noisy person segment between timestamp and `None` when present.
    """

    basename = posixpath.basename(name.replace("\\", "/"))
    stem = re.sub(r"\.[^.]+$", "", basename, flags=re.IGNORECASE)
    stem = urllib.parse.unquote(stem)
    stem = re.sub(r"\s+", "_", stem.strip())
    stem = re.sub(r"_+", "_", stem)

    # Normalize `#184_2022...` and `#1842022...` to the same shape.
    stem = re.sub(r"(#\d+)_+(20\d{2}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})", r"\1\2", stem)

    # Remove the noisy person/operator segment after timestamp. In exported
    # files it may be before `None`, or simply before the next business label.
    timestamp = r"20\d{2}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}"
    stem = re.sub(rf"({timestamp})[_\s][^_]+(?=_None_)", r"\1", stem)
    stem = re.sub(rf"({timestamp})[_\s][^_]+(?=_)", r"\1", stem)

    # Ignore common image/xml split directory names if they leaked into the key.
    parts = [part for part in re.split(r"[/\\]+", stem) if part.lower() not in IGNORED_PAIR_DIRS]
    stem = "_".join(parts)

    return re.sub(r"[\s_\-]+", "", stem).lower()


def parse_voc_xml(xml_text: str) -> dict:
    xml_payload = re.sub(r"^\s*<\?xml[^>]*\?>", "", xml_text, count=1)
    root = ET.fromstring(xml_payload)

    def text(path: str) -> str:
        node = root.find(path)
        return (node.text or "").strip() if node is not None else ""

    def int_text(path: str) -> int | None:
        value = text(path)
        if value == "":
            return None
        try:
            return int(float(value))
        except ValueError:
            return None

    objects = []
    for obj in root.findall("object"):
        name = (obj.findtext("name") or "").strip()
        bbox = obj.find("bndbox")
        if not name or bbox is None:
            continue
        parsed_bbox = {
            "xmin": _safe_int(bbox.findtext("xmin")),
            "ymin": _safe_int(bbox.findtext("ymin")),
            "xmax": _safe_int(bbox.findtext("xmax")),
            "ymax": _safe_int(bbox.findtext("ymax")),
        }
        if None in parsed_bbox.values():
            continue
        objects.append({"name": name, "bbox": parsed_bbox})

    return {
        "filename": text("filename"),
        "width": int_text("size/width"),
        "height": int_text("size/height"),
        "objects": objects,
    }


def _safe_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value.strip()))
    except (ValueError, AttributeError):
        return None


def pair_images_and_xml(images: list[dict], xml_files: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    images_by_basename: dict[tuple[str, str], dict] = {}
    images_by_key: dict[tuple[str, str], dict] = {}
    for image in images:
        scope = pair_scope_key(image["name"])
        basename = posixpath.basename(image["name"].replace("\\", "/"))
        images_by_basename[(scope, basename.lower())] = image
        images_by_key.setdefault((scope, normalize_pair_key(basename)), image)

    used_image_urls: set[str] = set()
    used_xml_urls: set[str] = set()
    pairs: list[dict] = []

    for xml_file in xml_files:
        image = None
        scope = pair_scope_key(xml_file["name"])
        xml_filename = posixpath.basename((xml_file.get("filename") or "").strip().replace("\\", "/")).lower()
        if xml_filename:
            image = images_by_basename.get((scope, xml_filename))
        if image is None:
            xml_basename = posixpath.basename(xml_file["name"].replace("\\", "/"))
            image = images_by_key.get((scope, normalize_pair_key(xml_basename)))
        if image is None:
            continue
        if image["url"] in used_image_urls:
            continue
        pairs.append({"image": image, "xml": xml_file})
        used_image_urls.add(image["url"])
        used_xml_urls.add(xml_file["url"])

    unmatched_images = [image for image in images if image["url"] not in used_image_urls]
    unmatched_xml = [xml_file for xml_file in xml_files if xml_file["url"] not in used_xml_urls]
    return pairs, unmatched_images, unmatched_xml


def pair_scope_key(name: str) -> str:
    path = urllib.parse.unquote(name.replace("\\", "/")).strip("/")
    parts = [part for part in path.split("/") if part]
    if len(parts) <= 1:
        return ""
    scope_parts = parts[:-1]
    scope_parts = [part for part in scope_parts if part.lower() not in IGNORED_PAIR_DIRS]
    return "/".join(scope_parts).lower()


def build_classification_count(pairs: list[dict]) -> list[dict]:
    counter: Counter[str] = Counter()
    for pair in pairs:
        for obj in pair["xml"].get("objects", []):
            label = obj.get("name")
            if label:
                counter[label] += 1

    items = [
        {"attributeValueId": str(index + 1), "attributeValue": label, "count": count}
        for index, (label, count) in enumerate(sorted(counter.items()))
    ]
    return [{"observationName": "缺陷标签", "attributeGroups": [{"attributeName": "缺陷类型", "items": items}]}]


def label_id_map(classification_count: list[dict]) -> dict[str, str]:
    result: dict[str, str] = {}
    for observation in classification_count:
        for group in observation.get("attributeGroups", []):
            for item in group.get("items", []):
                value = item.get("attributeValue")
                value_id = item.get("attributeValueId")
                if value and value_id:
                    result[value] = str(value_id)
    return result


def build_image_classification(xml_file: dict, ids_by_label: dict[str, str] | None = None) -> list[dict]:
    labels = sorted({obj["name"] for obj in xml_file.get("objects", []) if obj.get("name")})
    return [
        {
            "观测对象": "缺陷标签",
            "属性": "缺陷类型",
            "属性值": label,
            "attributeValueId": (ids_by_label or {}).get(label, label),
        }
        for label in labels
    ]


def normalize_classification(classification: list[dict], classification_count: list[dict]) -> list[dict]:
    ids_by_label = label_id_map(classification_count)
    normalized = []
    for item in classification:
        value = item.get("属性值") or item.get("attributeValue")
        if not value:
            continue
        normalized.append(
            {
                "观测对象": item.get("观测对象") or item.get("observationObject") or item.get("observationName") or "缺陷标签",
                "属性": item.get("属性") or item.get("attribute") or item.get("attributeName") or "缺陷类型",
                "属性值": value,
                "attributeValueId": str(item.get("attributeValueId") or ids_by_label.get(value) or value),
            }
        )
    return normalized


def build_classification_count_from_report_pairs(pairs: list[dict]) -> list[dict]:
    counter: Counter[str] = Counter()
    for pair in pairs:
        for item in pair.get("classification") or []:
            value = item.get("属性值") or item.get("attributeValue")
            if value:
                counter[value] += 1
    items = [
        {"attributeValueId": str(index + 1), "attributeValue": label, "count": count}
        for index, (label, count) in enumerate(sorted(counter.items()))
    ]
    return [{"observationName": "缺陷标签", "attributeGroups": [{"attributeName": "缺陷类型", "items": items}]}]


def build_db_payload(
    report: dict,
    options: ImportOptions,
    existing_names: set[str] | None = None,
    existing_addresses: set[str] | None = None,
) -> dict:
    if not options.dataset_name:
        raise ValueError("--dataset-name 缺失且无法自动生成")
    raw_pairs = report.get("pairs") or []
    pairs, skipped_duplicate_count = filter_duplicate_pairs(
        raw_pairs,
        allow_duplicates=options.allow_duplicates,
        existing_names=existing_names or set(),
        existing_addresses=existing_addresses or set(),
    )
    if not pairs:
        if raw_pairs and skipped_duplicate_count >= len(raw_pairs):
            raise AllPairsAlreadyImported(skipped_duplicate_count)
        raise ValueError("没有可入库的新图片：报告为空或图片均已存在")
    if options.max_import_count is not None:
        if options.max_import_count <= 0:
            raise ValueError("--max-import-count 必须大于 0")
        pairs = pairs[: options.max_import_count]

    now = format_db_timestamp()
    source = report.get("source")
    dataset_address = format_dataset_address(source)
    source_description = format_source_for_db(source)
    description = truncate_db_text(options.task_description or f"NAS URL 导入：{source_description}", MAX_TASK_DESCRIPTION_LENGTH)
    task_base_name = options.task_name or options.dataset_name
    task_name = task_base_name if task_base_name.startswith(IMPORT_TASK_PREFIX) else f"{IMPORT_TASK_PREFIX}{task_base_name}"
    classification_count = build_classification_count_from_report_pairs(pairs)

    return {
        "task": {
            "name": task_name,
            "status": "doing",
            "type": "input",
            "description": description,
            "executor": options.executor,
            "created_by": options.created_by,
            "created_time": now,
            "last_updated_time": now,
            "project_task": 0,
        },
        "dataset": {
            "name": options.dataset_name,
            "business_domain": options.business_domain,
            "business_scenario": options.business_scenario,
            "type": "original",
            "source_ids": "[]",
            "address": dataset_address,
            "total": len(pairs),
            "modality": options.modality,
            "mark": 1,
        },
        "rawDetails": [
            {
                "name": pair["name"],
                "address": pair["address"],
                "thumbnail": pair.get("thumbnail") or pair["address"],
            }
            for pair in pairs
        ],
        "classificationcount": classification_count,
        "skippedDuplicateCount": skipped_duplicate_count,
    }


def format_dataset_address(source: Any) -> str:
    if isinstance(source, dict):
        image_url = str(source.get("imageUrl") or "")
        xml_url = str(source.get("xmlUrl") or "")
        common_prefix = os.path.commonprefix([image_url, xml_url]) if image_url and xml_url else image_url or xml_url
        common_prefix = common_prefix.rsplit("/", 1)[0] + "/" if "/" in common_prefix else common_prefix
        return truncate_db_text(common_prefix or "NAS split image/xml dirs", MAX_DATASET_ADDRESS_LENGTH)
    return truncate_db_text(format_source_for_db(source), MAX_DATASET_ADDRESS_LENGTH)


def format_source_for_db(source: Any) -> str:
    if source is None:
        return ""
    if isinstance(source, str):
        return source
    return json.dumps(source, ensure_ascii=False)


def truncate_db_text(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    if max_length <= 3:
        return value[:max_length]
    return value[: max_length - 3] + "..."


def filter_duplicate_pairs(
    pairs: list[dict],
    allow_duplicates: bool,
    existing_names: set[str],
    existing_addresses: set[str],
) -> tuple[list[dict], int]:
    if allow_duplicates:
        return pairs, 0

    seen_names: set[str] = set()
    seen_addresses: set[str] = set()
    unique_pairs: list[dict] = []
    skipped = 0
    for pair in pairs:
        name = pair.get("name") or ""
        address = pair.get("address") or ""
        if name in seen_names or address in seen_addresses or name in existing_names or address in existing_addresses:
            skipped += 1
            continue
        seen_names.add(name)
        seen_addresses.add(address)
        unique_pairs.append(pair)
    return unique_pairs, skipped


def resolve_import_names(cursor: Any, report: dict, options: ImportOptions) -> ImportOptions:
    if options.dataset_name and options.task_name:
        return options

    auto_base = extract_auto_name_base(report)
    existing_task_names, existing_dataset_names = load_existing_import_names(cursor, auto_base)
    dataset_name = options.dataset_name
    task_name = options.task_name

    if dataset_name is None and task_name is None:
        auto_name = choose_unique_auto_name(auto_base, existing_task_names, existing_dataset_names)
        dataset_name = auto_name
        task_name = auto_name
    elif dataset_name is None:
        dataset_name = choose_unique_dataset_name(auto_base, existing_dataset_names)
    elif task_name is None:
        task_name = choose_unique_task_base_name(auto_base, existing_task_names)

    return replace(options, dataset_name=dataset_name, task_name=task_name)


def extract_auto_name_base(report: dict) -> str:
    source = report.get("source")
    source_text = format_source_for_db(source)
    decoded = urllib.parse.unquote(source_text).replace("\\", "/")
    matches = re.findall(r"(?:^|/)(20\d{2})/(\d{4})(?:/|$)", decoded)
    if matches:
        year, month_day = matches[-1]
        return f"{year[-2:]}{month_day}"

    modified_date = get_first_image_modified_date(report)
    if modified_date:
        return modified_date.strftime("%y%m%d")

    raise ValueError("未提供 --dataset-name，且无法从源路径或第一张图片修改时间中识别日期")


def get_first_image_modified_date(report: dict) -> datetime | None:
    for pair in report.get("pairs") or []:
        address = pair.get("address")
        if not address:
            continue
        try:
            return get_image_modified_date(str(address))
        except (OSError, ValueError, urllib.error.URLError):
            continue
    return None


def get_image_modified_date(url: str) -> datetime:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme in {"http", "https"}:
        return get_http_image_modified_date(url)
    if parsed.scheme == "file":
        path = urllib.request.url2pathname(parsed.path)
        return datetime.fromtimestamp(os.path.getmtime(path))
    raise ValueError(f"不支持读取该图片地址的修改时间: {url}")


def get_http_image_modified_date(url: str) -> datetime:
    last_error: Exception | None = None
    for method in ("HEAD", "GET"):
        request = urllib.request.Request(url, headers={"User-Agent": "cobra-nas-import/1.0"}, method=method)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                last_modified = response.headers.get("Last-Modified")
        except urllib.error.HTTPError as exc:
            last_error = exc
            if method == "HEAD":
                continue
            raise
        if last_modified:
            parsed_date = email.utils.parsedate_to_datetime(last_modified)
            if parsed_date.tzinfo is not None:
                parsed_date = parsed_date.astimezone().replace(tzinfo=None)
            return parsed_date
    if last_error:
        raise ValueError(f"读取图片修改时间失败: {last_error}") from last_error
    raise ValueError(f"图片响应头缺少 Last-Modified: {url}")


def load_existing_import_names(cursor: Any, auto_base: str) -> tuple[set[str], set[str]]:
    cursor.execute("SELECT name FROM task_entity WHERE name LIKE %s", (f"{IMPORT_TASK_PREFIX}{auto_base}%",))
    task_names = {str(row[0]) for row in cursor.fetchall() if row and row[0]}

    cursor.execute("SELECT name FROM dataset WHERE name LIKE %s", (f"{auto_base}%",))
    dataset_names = {str(row[0]) for row in cursor.fetchall() if row and row[0]}
    return task_names, dataset_names


def choose_unique_auto_name(auto_base: str, existing_task_names: set[str], existing_dataset_names: set[str]) -> str:
    for candidate in auto_name_candidates(auto_base):
        task_name = f"{IMPORT_TASK_PREFIX}{candidate}"
        if task_name in existing_task_names or candidate in existing_dataset_names:
            continue
        return candidate
    raise ValueError(f"无法为日期 {auto_base} 生成不重复且不超过长度限制的任务名/数据集名")


def choose_unique_task_base_name(auto_base: str, existing_task_names: set[str]) -> str:
    for candidate in auto_name_candidates(auto_base):
        task_name = f"{IMPORT_TASK_PREFIX}{candidate}"
        if task_name not in existing_task_names:
            return candidate
    raise ValueError(f"无法为日期 {auto_base} 生成不重复且不超过长度限制的任务名")


def choose_unique_dataset_name(auto_base: str, existing_dataset_names: set[str]) -> str:
    for candidate in auto_name_candidates(auto_base):
        if candidate not in existing_dataset_names:
            return candidate
    raise ValueError(f"无法为日期 {auto_base} 生成不重复且不超过长度限制的数据集名")


def auto_name_candidates(auto_base: str) -> Iterable[str]:
    max_base_length = min(MAX_DATASET_NAME_LENGTH, MAX_TASK_NAME_LENGTH - len(IMPORT_TASK_PREFIX))
    for suffix in name_suffixes():
        if len(suffix) > max_base_length:
            break
        base = auto_base[: max_base_length - len(suffix)] if suffix else auto_base[:max_base_length]
        candidate = f"{base}{suffix}"
        if candidate:
            yield candidate


def name_suffixes() -> Iterable[str]:
    yield ""
    for code in range(ord("A"), ord("Z") + 1):
        yield chr(code)

    numeric_suffix = 1
    while True:
        for code in range(ord("A"), ord("Z") + 1):
            yield f"{chr(code)}{numeric_suffix}"
        numeric_suffix += 1


def batch_name_suffix(batch_index: int) -> str:
    if batch_index <= 0:
        raise ValueError("--start-batch 必须大于 0")
    for index, suffix in enumerate(name_suffixes(), start=1):
        if index == batch_index:
            return suffix
    raise RuntimeError("无法生成批次名称后缀")


def with_batch_import_names(options: ImportOptions, batch_index: int) -> ImportOptions:
    suffix = batch_name_suffix(batch_index)
    return replace(
        options,
        dataset_name=append_name_suffix(options.dataset_name, suffix, MAX_DATASET_NAME_LENGTH),
        task_name=append_task_name_suffix(options.task_name, suffix),
        max_import_count=None,
    )


def append_task_name_suffix(task_name: str | None, suffix: str) -> str | None:
    if task_name is None:
        return None
    if task_name.startswith(IMPORT_TASK_PREFIX):
        return append_name_suffix(task_name, suffix, MAX_TASK_NAME_LENGTH)
    return append_name_suffix(task_name, suffix, MAX_TASK_NAME_LENGTH - len(IMPORT_TASK_PREFIX))


def append_name_suffix(name: str | None, suffix: str, max_length: int) -> str | None:
    if name is None:
        return None
    if not suffix:
        return name[:max_length]
    if len(suffix) >= max_length:
        return suffix[-max_length:]
    return f"{name[: max_length - len(suffix)]}{suffix}"


def write_database(
    report: dict,
    import_options: ImportOptions,
    db_options: DbOptions,
    checkpoint: ImportCheckpoint | None = None,
    checkpoint_path: str | None = None,
) -> dict:
    connection = open_mysql_connection(db_options)
    task_id: int | None = None
    dataset_id: int | None = None
    payload: dict | None = None
    try:
        with connection.cursor() as cursor:
            existing_names: set[str] = set()
            existing_addresses: set[str] = set()
            if not import_options.allow_duplicates:
                pair_count = len(report.get("pairs") or [])
                print_progress(f"正在检查库中重复记录：待比对 {pair_count} 张图片的 name/address")
                existing_names, existing_addresses = load_existing_raw_details(
                    cursor,
                    report.get("pairs") or [],
                )
                print_progress(
                    f"重复检查完成：库中已存在 name {len(existing_names)} 条、address {len(existing_addresses)} 条"
                )
            resolved_import_options = resolve_import_names(cursor, report, import_options)
            try:
                payload = build_db_payload(report, resolved_import_options, existing_names, existing_addresses)
            except AllPairsAlreadyImported as exc:
                print_progress(str(exc))
                return {
                    "taskId": checkpoint.db_write_task_id if checkpoint else None,
                    "datasetId": checkpoint.db_write_dataset_id if checkpoint else None,
                    "rawDetailCount": 0,
                    "skippedDuplicateCount": exc.skipped_duplicate_count,
                    "alreadyImported": True,
                }

            total_count = len(payload["rawDetails"])
            batch_index = checkpoint.active_batch_index if checkpoint is not None else None
            print_db_write_progress(
                0,
                total_count,
                db_options,
                batch_index=batch_index,
                force=True,
                stage="准备写入",
            )
            print_progress(
                f"开始写入数据库：待写入 {total_count} 张图片，跳过重复 {payload.get('skippedDuplicateCount', 0)} 张"
            )

            resume_write = (
                checkpoint is not None
                and checkpoint.db_write_task_id is not None
                and checkpoint.db_write_dataset_id is not None
                and checkpoint.phase == "db_write"
            )
            if resume_write and checkpoint is not None:
                task_id = int(checkpoint.db_write_task_id)
                dataset_id = int(checkpoint.db_write_dataset_id)
                print_progress(
                    f"从检查点恢复写库：复用 taskId={task_id}, datasetId={dataset_id}，"
                    f"待写入 {total_count} 张（已自动跳过库中重复记录 {payload.get('skippedDuplicateCount', 0)} 张）"
                )
            else:
                task_id = insert_task(cursor, payload["task"])
                dataset_id = insert_dataset(cursor, payload["dataset"], task_id)
                connection.commit()
                if checkpoint is not None:
                    checkpoint.begin_db_write(task_id, dataset_id)
                    save_import_checkpoint(checkpoint_path, checkpoint, force=True)
                print_progress(f"导入任务已创建：taskId={task_id}, 状态=doing")

            inserted = insert_details(
                cursor,
                payload,
                dataset_id,
                db_options,
                connection=connection,
                checkpoint=checkpoint,
                checkpoint_path=checkpoint_path,
                batch_index=batch_index,
            )
            update_dataset_total(cursor, dataset_id, count_dataset_details(cursor, dataset_id))
            update_task_status(cursor, task_id, "done")
        connection.commit()
        if checkpoint is not None:
            checkpoint.clear_db_write()
        print_progress(
            f"数据库写入完成：taskId={task_id}, datasetId={dataset_id}, 状态=done, "
            f"本次写入={inserted['rawDetailCount']}, 跳过重复={payload.get('skippedDuplicateCount', 0)}"
        )
        return {
            "taskId": task_id,
            "datasetId": dataset_id,
            "skippedDuplicateCount": payload.get("skippedDuplicateCount", 0),
            **inserted,
        }
    except Exception:
        connection.rollback()
        if task_id is not None:
            mark_task_failed(connection, task_id, db_options.failure_status)
        raise
    finally:
        connection.close()


def count_dataset_details(cursor: Any, dataset_id: int) -> int:
    cursor.execute("SELECT COUNT(*) FROM raw_dataset_detail WHERE dataset_id = %s", (dataset_id,))
    row = cursor.fetchone()
    return int(row[0] if row else 0)


def load_existing_raw_details(cursor: Any, pairs: list[dict]) -> tuple[set[str], set[str]]:
    names = sorted({pair.get("name") for pair in pairs if pair.get("name")})
    addresses = sorted({pair.get("address") for pair in pairs if pair.get("address")})
    existing_names: set[str] = set()
    existing_addresses: set[str] = set()

    name_chunks = list(chunked(names, 500))
    for chunk_index, chunk in enumerate(name_chunks, start=1):
        placeholders = ", ".join(["%s"] * len(chunk))
        cursor.execute(f"SELECT name, address FROM raw_dataset_detail WHERE name IN ({placeholders})", chunk)
        for name, address in cursor.fetchall():
            if name:
                existing_names.add(name)
            if address:
                existing_addresses.add(address)
        maybe_print_duplicate_check_progress("name", chunk_index, len(name_chunks), len(existing_names))

    address_chunks = list(chunked(addresses, 500))
    for chunk_index, chunk in enumerate(address_chunks, start=1):
        placeholders = ", ".join(["%s"] * len(chunk))
        cursor.execute(f"SELECT name, address FROM raw_dataset_detail WHERE address IN ({placeholders})", chunk)
        for name, address in cursor.fetchall():
            if name:
                existing_names.add(name)
            if address:
                existing_addresses.add(address)
        maybe_print_duplicate_check_progress("address", chunk_index, len(address_chunks), len(existing_addresses))

    return existing_names, existing_addresses


def maybe_print_duplicate_check_progress(field: str, chunk_index: int, total_chunks: int, hit_count: int) -> None:
    if total_chunks <= 0:
        return
    if chunk_index != 1 and chunk_index % DUPLICATE_CHECK_PROGRESS_EVERY != 0 and chunk_index != total_chunks:
        return
    print_progress(
        f"重复检查进度（{field}）：批次 {chunk_index}/{total_chunks}，累计命中 {hit_count} 条"
    )


def chunked(values: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def open_mysql_connection(options: DbOptions):
    try:
        import pymysql  # type: ignore

        return pymysql.connect(
            host=options.host,
            port=options.port,
            user=options.user,
            password=options.password,
            database=options.database,
            charset=options.charset,
            autocommit=False,
        )
    except ImportError:
        pass

    try:
        import mysql.connector  # type: ignore

        return mysql.connector.connect(
            host=options.host,
            port=options.port,
            user=options.user,
            password=options.password,
            database=options.database,
            charset=options.charset,
            autocommit=False,
        )
    except ImportError as exc:
        raise RuntimeError("缺少 MySQL Python 驱动，请先安装 pymysql 或 mysql-connector-python") from exc


def allocate_seq_ids(cursor: Any, seq_table: str, count: int) -> list[int]:
    """Allocate primary keys the same way Hibernate TABLE generators do."""
    if count <= 0:
        return []
    cursor.execute(f"SELECT next_val FROM {seq_table} FOR UPDATE")
    row = cursor.fetchone()
    if not row or row[0] is None:
        raise RuntimeError(f"序列表 {seq_table} 没有可用的 next_val")
    start = int(row[0])
    cursor.execute(f"UPDATE {seq_table} SET next_val = next_val + %s", (count,))
    return list(range(start, start + count))


def insert_task(cursor: Any, task: dict) -> int:
    task_id = allocate_seq_ids(cursor, "task_entity_seq", 1)[0]
    params = {**task, "id": task_id}
    cursor.execute(
        """
        INSERT INTO task_entity
          (id, name, status, type, description, executor, created_by, created_time, last_updated_time, project_task)
        VALUES
          (%(id)s, %(name)s, %(status)s, %(type)s, %(description)s, %(executor)s, %(created_by)s, %(created_time)s, %(last_updated_time)s, %(project_task)s)
        """,
        params,
    )
    return task_id


def insert_dataset(cursor: Any, dataset: dict, task_id: int) -> int:
    params = {**dataset, "task_id": task_id, "total": 0}
    cursor.execute(
        """
        INSERT INTO dataset
          (name, business_domain, business_scenario, type, task_id, source_ids, address, total, modality, mark)
        VALUES
          (%(name)s, %(business_domain)s, %(business_scenario)s, %(type)s, %(task_id)s, %(source_ids)s, %(address)s, %(total)s, %(modality)s, %(mark)s)
        """,
        params,
    )
    return int(cursor.lastrowid)


def update_dataset_total(cursor: Any, dataset_id: int, total: int) -> None:
    cursor.execute("UPDATE dataset SET total = %s WHERE id = %s", (total, dataset_id))


def update_task_status(cursor: Any, task_id: int, status: str) -> None:
    cursor.execute(
        "UPDATE task_entity SET status = %s, last_updated_time = %s WHERE id = %s",
        (status, format_db_timestamp(), task_id),
    )


def mark_task_failed(connection: Any, task_id: int, failure_status: str) -> None:
    try:
        with connection.cursor() as cursor:
            update_task_status(cursor, task_id, failure_status)
        connection.commit()
        print(f"数据库写入失败：taskId={task_id}, 状态={failure_status}", file=sys.stderr, flush=True)
    except Exception:
        connection.rollback()


def insert_details(
    cursor: Any,
    payload: dict,
    dataset_id: int,
    db_options: DbOptions,
    connection: Any,
    checkpoint: ImportCheckpoint | None = None,
    checkpoint_path: str | None = None,
    batch_index: int | None = None,
) -> dict:
    raw_details = payload["rawDetails"]
    raw_count = 0
    skipped_duplicates = 0
    total_count = len(raw_details)
    if total_count <= 0:
        print_progress("没有需要插入的图片记录")
        return {"rawDetailCount": 0}
    print_db_write_progress(0, total_count, db_options, batch_index=batch_index, force=True, stage="开始插入")
    total_chunks = (total_count + INSERT_COMMIT_EVERY - 1) // INSERT_COMMIT_EVERY
    for chunk_index, chunk_start in enumerate(range(0, total_count, INSERT_COMMIT_EVERY), start=1):
        chunk = raw_details[chunk_start : chunk_start + INSERT_COMMIT_EVERY]
        raw_ids = allocate_seq_ids(cursor, "raw_dataset_detail_seq", len(chunk))
        for raw_id, raw in zip(raw_ids, chunk):
            raw_params = {**raw, "id": raw_id, "dataset_id": dataset_id}
            try:
                cursor.execute(
                    """
                    INSERT INTO raw_dataset_detail
                      (id, name, address, dataset_id, thumbnail)
                    VALUES
                      (%(id)s, %(name)s, %(address)s, %(dataset_id)s, %(thumbnail)s)
                    """,
                    raw_params,
                )
            except Exception as exc:
                if is_mysql_duplicate_error(exc):
                    skipped_duplicates += 1
                    continue
                raise
            raw_count += 1
            print_db_write_progress(raw_count, total_count, db_options, batch_index=batch_index)
            maybe_throttle_writes(raw_count, total_count, db_options)
        connection.commit()
        print_progress(
            f"{'批次 ' + str(batch_index) + '：' if batch_index is not None else ''}"
            f"数据库提交进度：第 {chunk_index}/{total_chunks} 批已落库，累计 {raw_count}/{total_count} 张"
        )
        if checkpoint is not None:
            checkpoint.update_db_write_progress(raw_count)
            save_import_checkpoint(checkpoint_path, checkpoint, force=True)
    print_db_write_progress(raw_count, total_count, db_options, batch_index=batch_index, force=True, stage="写入完成")
    if skipped_duplicates:
        print_progress(f"写库时跳过 {skipped_duplicates} 条数据库重复记录")
    return {"rawDetailCount": raw_count}


def is_mysql_duplicate_error(exc: Exception) -> bool:
    args = getattr(exc, "args", ())
    if args and args[0] in {1062, 1169, 1586}:
        return True
    message = str(exc).lower()
    return "duplicate" in message


def format_db_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")


def print_db_write_progress(
    current_count: int,
    total_count: int,
    db_options: DbOptions,
    *,
    batch_index: int | None = None,
    force: bool = False,
    stage: str | None = None,
) -> None:
    if total_count <= 0 and stage is None:
        return
    prefix = f"批次 {batch_index}：" if batch_index is not None else ""
    if stage is not None:
        if total_count > 0:
            print_progress(f"{prefix}数据库{stage}：目标 {total_count} 张")
        else:
            print_progress(f"{prefix}数据库{stage}")
        return
    if db_options.progress_every <= 0 and not force:
        return
    should_print = force or current_count == 1 or current_count >= total_count
    if not should_print and db_options.progress_every > 0:
        should_print = current_count % db_options.progress_every == 0
    if not should_print:
        return
    percent = current_count * 100 / total_count
    print_progress(f"{prefix}数据库写入进度：{current_count}/{total_count} ({percent:.1f}%)")


def maybe_print_progress(current_count: int, total_count: int, db_options: DbOptions) -> None:
    print_db_write_progress(current_count, total_count, db_options)


def maybe_throttle_writes(current_count: int, total_count: int, db_options: DbOptions) -> None:
    if db_options.write_delay_seconds <= 0:
        return
    if db_options.write_delay_every <= 0:
        return
    if current_count >= total_count:
        return
    if current_count % db_options.write_delay_every == 0:
        time.sleep(db_options.write_delay_seconds)


def build_fusion_detail_id(raw_detail_id: int, task_id: int) -> int:
    # Match the existing Java fusion creation convention: raw detail id + task id.
    candidate = int(f"{raw_detail_id}{task_id}")
    if candidate <= 9_223_372_036_854_775_807:
        return candidate
    return raw_detail_id * 1_000_000 + (task_id % 1_000_000)


def list_source_files(
    source_url: str | None,
    local_root: str | None,
    public_base_url: str | None,
    image_url: str | None = None,
    xml_url: str | None = None,
) -> list[SourceFile]:
    if image_url or xml_url:
        if not image_url or not xml_url:
            raise ValueError("--image-url 和 --xml-url 必须同时提供")
        return list_split_http_dirs(image_url, xml_url)
    if local_root:
        return list_local_files(local_root, public_base_url)
    if source_url:
        return list_http_or_minio_files(source_url, recursive_http=True)
    raise ValueError("Either --source-url, --local-root, or --image-url/--xml-url is required")


def list_local_files(local_root: str, public_base_url: str | None) -> list[SourceFile]:
    root = Path(local_root)
    if not root.exists():
        raise FileNotFoundError(f"Local root does not exist: {local_root}")
    files: list[SourceFile] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix not in IMAGE_SUFFIXES and suffix != XML_SUFFIX:
            continue
        relative = path.relative_to(root).as_posix()
        url = to_public_url(public_base_url, relative) if public_base_url else path.as_uri()
        files.append(SourceFile(name=relative, url=url, local_path=str(path)))
        if len(files) % SCAN_PROGRESS_EVERY == 0:
            print_progress(f"扫描进度：已发现 {len(files)} 个图片/XML 文件")
    return files


def list_split_http_dirs(image_url: str, xml_url: str) -> list[SourceFile]:
    return list_http_or_minio_files(image_url, recursive_http=True) + list_http_or_minio_files(xml_url, recursive_http=True)


def list_http_or_minio_files(source_url: str, recursive_http: bool = False) -> list[SourceFile]:
    try:
        files = list_minio_public_prefix(source_url)
        if files:
            return files
    except Exception as exc:
        minio_error = exc
    else:
        minio_error = None

    try:
        files = list_http_index(source_url, recursive=recursive_http)
        if files:
            return files
    except Exception as exc:
        index_error = exc
    else:
        index_error = None

    hint = (
        "无法从该 URL 枚举文件。MinIO 的对象 URL 通常只能访问单个对象，"
        "列目录需要 bucket 开启匿名 ListBucket，或改用本地挂载路径/提供 MinIO 凭证模式。"
    )
    details = []
    if minio_error:
        details.append(f"MinIO ListObjects 失败: {minio_error}")
    if index_error:
        details.append(f"HTTP 目录索引失败: {index_error}")
    raise RuntimeError(hint + (" " + "；".join(details) if details else ""))


def list_minio_public_prefix(source_url: str) -> list[SourceFile]:
    parsed = urllib.parse.urlparse(source_url)
    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) < 2:
        return []
    bucket = urllib.parse.unquote(path_parts[0])
    prefix = "/".join(urllib.parse.unquote(part) for part in path_parts[1:])
    if prefix and not prefix.endswith("/"):
        prefix = prefix + "/"

    query = urllib.parse.urlencode({"list-type": "2", "prefix": prefix})
    list_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, f"/{urllib.parse.quote(bucket)}", "", query, ""))
    xml_text = read_url_text(list_url)
    root = ET.fromstring(xml_text)
    namespace = _xml_namespace(root.tag)

    files: list[SourceFile] = []
    for contents in root.findall(f"{namespace}Contents"):
        key = contents.findtext(f"{namespace}Key")
        if not key:
            continue
        suffix = Path(key).suffix.lower()
        if suffix not in IMAGE_SUFFIXES and suffix != XML_SUFFIX:
            continue
        file_url = urllib.parse.urlunparse(
            (parsed.scheme, parsed.netloc, f"/{urllib.parse.quote(bucket)}/{urllib.parse.quote(key, safe='/')}", "", "", "")
        )
        files.append(SourceFile(name=key, url=file_url))
    return files


def _xml_namespace(tag: str) -> str:
    if tag.startswith("{"):
        return tag[: tag.index("}") + 1]
    return ""


def list_http_index(source_url: str, recursive: bool = False) -> list[SourceFile]:
    if recursive:
        return list_http_index_recursive(source_url)

    html = read_url_text(source_url)
    parser = DirectoryLinkParser()
    parser.feed(html)
    files: list[SourceFile] = []
    for link in parser.links:
        full_url = urllib.parse.urljoin(source_url.rstrip("/") + "/", link)
        parsed = urllib.parse.urlparse(full_url)
        suffix = Path(urllib.parse.unquote(parsed.path)).suffix.lower()
        if suffix not in IMAGE_SUFFIXES and suffix != XML_SUFFIX:
            continue
        files.append(SourceFile(name=urllib.parse.unquote(parsed.path.lstrip("/")), url=full_url))
    return files


def list_http_index_recursive(source_url: str) -> list[SourceFile]:
    source_dir_url = canonical_http_dir_url(source_url)
    queue = [source_dir_url]
    visited_dirs: set[str] = set()
    files: list[SourceFile] = []

    while queue:
        dir_url = queue.pop(0)
        if dir_url in visited_dirs:
            continue
        print_http_scan_progress(dir_url, source_dir_url, len(visited_dirs), len(queue) + 1, len(files))
        visited_dirs.add(dir_url)

        html = read_url_text(dir_url)
        parser = DirectoryLinkParser()
        parser.feed(html)

        for link in parser.links:
            if is_parent_directory_link(link):
                continue

            full_url = canonical_http_url(urllib.parse.urljoin(dir_url, link))
            if not is_under_source_url(full_url, source_dir_url):
                continue

            parsed = urllib.parse.urlparse(full_url)
            suffix = Path(urllib.parse.unquote(parsed.path)).suffix.lower()
            if suffix in IMAGE_SUFFIXES or suffix == XML_SUFFIX:
                files.append(SourceFile(name=relative_http_name(source_dir_url, full_url), url=full_url))
                continue
            if suffix:
                continue

            directory_url = canonical_http_dir_url(full_url)
            if directory_url not in visited_dirs:
                queue.append(directory_url)

    return files


def plan_split_dir_batches(image_url: str, xml_url: str, batch_size: int = 50_000) -> list[SplitDirBatch]:
    return list(iter_split_dir_batches_dfs(image_url, xml_url, batch_size))


class _SplitDirBatchAccumulator:
    def __init__(self, batch_size: int) -> None:
        self.batch_size = batch_size
        self.current_pairs: list[SplitDirPair] = []
        self.current_count = 0
        self.batch_index = 0
        self._ready_batches: list[SplitDirBatch] = []

    def add(self, pair: SplitDirPair) -> None:
        if self.current_pairs and self.current_count >= self.batch_size:
            self._ready_batches.append(self._flush_current())
        self.current_pairs.append(pair)
        self.current_count += pair.image_dir.file_count
        if self.current_pairs and self.current_count >= self.batch_size:
            self._ready_batches.append(self._flush_current())

    def flush_ready(self) -> Iterable[SplitDirBatch]:
        while self._ready_batches:
            yield self._ready_batches.pop(0)

    def finalize(self) -> SplitDirBatch | None:
        if not self.current_pairs:
            return None
        return self._flush_current()

    def planned_batch_count(self) -> int:
        pending = 1 if self.current_pairs else 0
        return self.batch_index + len(self._ready_batches) + pending

    def restore_from_checkpoint(self, checkpoint: ImportCheckpoint) -> None:
        self.batch_index = checkpoint.batch_index
        self.current_pairs = list(checkpoint.pending_pairs)
        self.current_count = checkpoint.pending_image_count

    def sync_to_checkpoint(self, checkpoint: ImportCheckpoint) -> None:
        checkpoint.batch_index = self.batch_index
        checkpoint.pending_pairs = list(self.current_pairs)
        checkpoint.pending_image_count = self.current_count

    def _flush_current(self) -> SplitDirBatch:
        self.batch_index += 1
        batch = SplitDirBatch(
            index=self.batch_index,
            dir_pairs=list(self.current_pairs),
            estimated_image_count=self.current_count,
        )
        self.current_pairs = []
        self.current_count = 0
        return batch


def sync_checkpoint_from_dfs(
    checkpoint: ImportCheckpoint,
    state: SyncDfsState,
    accumulator: _SplitDirBatchAccumulator,
) -> None:
    checkpoint.scanned_dirs = state.scanned_dirs
    checkpoint.paired_dirs = state.paired_dirs
    checkpoint.unmatched_image_dirs = state.unmatched_image_dirs
    checkpoint.unmatched_xml_dirs = state.unmatched_xml_dirs
    accumulator.sync_to_checkpoint(checkpoint)


def iter_split_dir_batches_dfs(
    image_url: str,
    xml_url: str,
    batch_size: int = 50_000,
    checkpoint: ImportCheckpoint | None = None,
    checkpoint_path: str | None = None,
) -> Iterable[SplitDirBatch]:
    if batch_size <= 0:
        raise ValueError("--batch-size 必须大于 0")

    print_progress("开始同步深度扫描 NAS 目录并规划批次...")
    image_root_url = canonical_http_dir_url(image_url)
    xml_root_url = canonical_http_dir_url(xml_url)
    if checkpoint is None:
        checkpoint = ImportCheckpoint.new(image_url, xml_url, batch_size)
    if not checkpoint.dfs_stack:
        checkpoint.dfs_stack = [(image_root_url, xml_root_url, "")]
    state = SyncDfsState(
        image_root_url=image_root_url,
        xml_root_url=xml_root_url,
        scanned_dirs=checkpoint.scanned_dirs,
        paired_dirs=checkpoint.paired_dirs,
        unmatched_image_dirs=checkpoint.unmatched_image_dirs,
        unmatched_xml_dirs=checkpoint.unmatched_xml_dirs,
        skipped_dirs=len(checkpoint.skipped_relative_paths),
    )
    accumulator = _SplitDirBatchAccumulator(batch_size)
    accumulator.restore_from_checkpoint(checkpoint)
    stack = list(checkpoint.dfs_stack)
    while stack:
        image_dir_url, xml_dir_url, relative_path = stack.pop()
        if checkpoint.is_dir_visited(relative_path):
            print_progress(f"跳过已扫描目录：{relative_path or '.'}")
            continue

        state.scanned_dirs += 1
        print_sync_dfs_progress(relative_path, state)
        checkpoint.dfs_stack = stack + [(image_dir_url, xml_dir_url, relative_path)]
        sync_checkpoint_from_dfs(checkpoint, state, accumulator)
        save_import_checkpoint(checkpoint_path, checkpoint, force=True)
        try:
            image_listing = list_http_directory_listing(image_dir_url, state.image_root_url)
            xml_listing = list_http_directory_listing(xml_dir_url, state.xml_root_url)

            if image_listing.image_count > 0 and xml_listing.xml_count > 0:
                pair = SplitDirPair(
                    image_dir=HttpFileDirectory(
                        relative_dir=relative_http_name(state.image_root_url, image_dir_url).strip("/"),
                        url=canonical_http_dir_url(image_dir_url),
                        file_count=image_listing.image_count,
                    ),
                    xml_dir=HttpFileDirectory(
                        relative_dir=relative_http_name(state.xml_root_url, xml_dir_url).strip("/"),
                        url=canonical_http_dir_url(xml_dir_url),
                        file_count=xml_listing.xml_count,
                    ),
                )
                state.paired_dirs += 1
                accumulator.add(pair)
                yield from accumulator.flush_ready()

            elif image_listing.image_count > 0:
                state.unmatched_image_dirs += 1
            elif xml_listing.xml_count > 0:
                state.unmatched_xml_dirs += 1

            child_matches = match_sync_subdirs(image_listing.subdirs, xml_listing.subdirs, relative_path)
            for image_child_url, xml_child_url, child_relative_path in reversed(child_matches):
                stack.append((image_child_url, xml_child_url, child_relative_path))
        except Exception as exc:
            if is_skippable_directory_error(exc):
                handle_skippable_directory_scan_failure(relative_path, exc, phase="同步深扫：")
                state.skipped_dirs += 1
                checkpoint.mark_dir_skipped(relative_path)
                checkpoint.dfs_stack = list(stack)
                sync_checkpoint_from_dfs(checkpoint, state, accumulator)
                save_import_checkpoint(checkpoint_path, checkpoint, force=True)
                continue
            checkpoint.record_error(exc)
            checkpoint.dfs_stack = stack + [(image_dir_url, xml_dir_url, relative_path)]
            sync_checkpoint_from_dfs(checkpoint, state, accumulator)
            save_import_checkpoint(checkpoint_path, checkpoint, force=True)
            raise RuntimeError(f"目录扫描失败：{relative_path or '.'}") from exc

        checkpoint.mark_dir_visited(relative_path)
        checkpoint.dfs_stack = list(stack)
        sync_checkpoint_from_dfs(checkpoint, state, accumulator)
        save_import_checkpoint(checkpoint_path, checkpoint, force=True)

    if state.unmatched_image_dirs:
        print_progress(f"目录扫描告警：{state.unmatched_image_dirs} 个图片目录未匹配到 XML 目录")
    if state.unmatched_xml_dirs:
        print_progress(f"目录扫描告警：{state.unmatched_xml_dirs} 个 XML 目录未匹配到图片目录")
    if state.skipped_dirs:
        print_progress(f"目录扫描告警：跳过 {state.skipped_dirs} 个不可访问目录（如权限不足或不存在）")
    print_progress(
        f"目录扫描完成：已扫描目录 {state.scanned_dirs}，匹配目录 {state.paired_dirs}，"
        f"跳过目录 {state.skipped_dirs}，计划批次 {accumulator.planned_batch_count()}"
    )
    yield from accumulator.flush_ready()
    final_batch = accumulator.finalize()
    if final_batch is not None:
        yield final_batch
    checkpoint.dfs_stack = []
    sync_checkpoint_from_dfs(checkpoint, state, accumulator)
    save_import_checkpoint(checkpoint_path, checkpoint, force=True)


def list_http_directory_listing(dir_url: str, source_root_url: str) -> HttpDirListing:
    html = read_url_text(dir_url)
    parser = DirectoryLinkParser()
    parser.feed(html)
    subdirs: dict[str, str] = {}
    image_count = 0
    xml_count = 0
    for link in parser.links:
        if is_parent_directory_link(link):
            continue

        full_url = canonical_http_url(urllib.parse.urljoin(dir_url, link))
        if not is_under_source_url(full_url, source_root_url):
            continue

        parsed = urllib.parse.urlparse(full_url)
        suffix = Path(urllib.parse.unquote(parsed.path)).suffix.lower()
        if suffix in IMAGE_SUFFIXES:
            image_count += 1
            continue
        if suffix == XML_SUFFIX:
            xml_count += 1
            continue
        if suffix:
            continue

        directory_url = canonical_http_dir_url(full_url)
        directory_name = posixpath.basename(urllib.parse.unquote(parsed.path).rstrip("/"))
        if directory_name:
            subdirs[directory_name] = directory_url

    return HttpDirListing(subdirs=subdirs, image_count=image_count, xml_count=xml_count)


def match_sync_subdirs(
    image_subdirs: dict[str, str],
    xml_subdirs: dict[str, str],
    parent_relative_path: str,
) -> list[tuple[str, str, str]]:
    matches: list[tuple[str, str, str]] = []
    used_xml_names: set[str] = set()

    for image_name in sorted(image_subdirs.keys(), key=str.lower):
        image_child_url = image_subdirs[image_name]
        image_child_rel = join_relative_path(parent_relative_path, image_name)
        xml_name = image_name
        if xml_name in xml_subdirs and xml_name not in used_xml_names:
            matches.append((image_child_url, xml_subdirs[xml_name], image_child_rel))
            used_xml_names.add(xml_name)
            continue

        image_norm = normalize_split_dir_key(image_child_rel)
        matched_xml_name = None
        for candidate_name in sorted(xml_subdirs.keys(), key=str.lower):
            if candidate_name in used_xml_names:
                continue
            xml_child_rel = join_relative_path(parent_relative_path, candidate_name)
            if normalize_split_dir_key(xml_child_rel) == image_norm:
                matched_xml_name = candidate_name
                break
        if matched_xml_name is not None:
            matches.append((image_child_url, xml_subdirs[matched_xml_name], image_child_rel))
            used_xml_names.add(matched_xml_name)

    for xml_name in sorted(xml_subdirs.keys(), key=str.lower):
        if xml_name in used_xml_names:
            continue
        if xml_name in image_subdirs:
            continue
        xml_child_rel = join_relative_path(parent_relative_path, xml_name)
        xml_norm = normalize_split_dir_key(xml_child_rel)
        for image_name in sorted(image_subdirs.keys(), key=str.lower):
            image_child_rel = join_relative_path(parent_relative_path, image_name)
            if normalize_split_dir_key(image_child_rel) == xml_norm:
                matches.append((image_subdirs[image_name], xml_subdirs[xml_name], image_child_rel))
                used_xml_names.add(xml_name)
                break

    return matches


def join_relative_path(parent_relative_path: str, child_name: str) -> str:
    if parent_relative_path:
        return posixpath.join(parent_relative_path, child_name)
    return child_name


def normalize_split_dir_key(relative_dir: str) -> str:
    parts = [part for part in relative_dir.replace("\\", "/").split("/") if part]
    parts = [part for part in parts if part.lower() not in IGNORED_PAIR_DIRS]
    return "/".join(parts).lower()


def list_http_files_in_directory(directory: HttpFileDirectory, suffixes: set[str]) -> list[SourceFile]:
    html = read_url_text(directory.url)
    parser = DirectoryLinkParser()
    parser.feed(html)
    files: list[SourceFile] = []
    for link in parser.links:
        if is_parent_directory_link(link):
            continue
        full_url = canonical_http_url(urllib.parse.urljoin(directory.url, link))
        parsed = urllib.parse.urlparse(full_url)
        suffix = Path(urllib.parse.unquote(parsed.path)).suffix.lower()
        if suffix not in suffixes:
            continue
        basename = posixpath.basename(urllib.parse.unquote(parsed.path))
        name = posixpath.join(directory.relative_dir, basename) if directory.relative_dir else basename
        files.append(SourceFile(name=name, url=full_url))
    return files


def print_http_scan_progress(
    dir_url: str,
    source_dir_url: str,
    scanned_dir_count: int,
    pending_dir_count: int,
    file_count: int,
) -> None:
    current_dir = relative_http_name(source_dir_url, dir_url).rstrip("/") or "."
    print_progress(
        f"扫描进度：已扫描目录 {scanned_dir_count}，待扫描目录 {pending_dir_count}，已发现文件 {file_count}，当前目录 {current_dir}"
    )


def print_sync_dfs_progress(relative_path: str, state: SyncDfsState) -> None:
    current_dir = relative_path or "."
    print_progress(
        f"同步深扫进度：已扫描目录 {state.scanned_dirs}，已匹配含文件目录 {state.paired_dirs}，当前目录 {current_dir}"
    )


def canonical_http_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))


def canonical_http_dir_url(url: str) -> str:
    parsed = urllib.parse.urlparse(canonical_http_url(url))
    path = parsed.path or "/"
    if not path.endswith("/"):
        path += "/"
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def is_parent_directory_link(link: str) -> bool:
    parsed = urllib.parse.urlparse(link)
    path = urllib.parse.unquote(parsed.path or link).strip().strip("/")
    return path in {"", ".", "..", "..."}


def is_under_source_url(candidate_url: str, source_dir_url: str) -> bool:
    candidate = urllib.parse.urlparse(candidate_url)
    source = urllib.parse.urlparse(source_dir_url)
    if candidate.scheme != source.scheme or candidate.netloc != source.netloc:
        return False
    candidate_path = urllib.parse.unquote(candidate.path)
    source_path = urllib.parse.unquote(source.path)
    if not source_path.endswith("/"):
        source_path += "/"
    return candidate_path.startswith(source_path)


def relative_http_name(source_dir_url: str, file_url: str) -> str:
    source = urllib.parse.urlparse(source_dir_url)
    file = urllib.parse.urlparse(file_url)
    source_path = urllib.parse.unquote(source.path)
    file_path = urllib.parse.unquote(file.path)
    if not source_path.endswith("/"):
        source_path += "/"
    if file_path.startswith(source_path):
        return file_path[len(source_path) :].lstrip("/")
    return file_path.lstrip("/")


def read_file_text(file_info: dict) -> str:
    if file_info.get("local_path"):
        return Path(file_info["local_path"]).read_text(encoding="utf-8")
    return read_url_text(file_info["url"])


def read_url_text(url: str, timeout: int | None = None, retries: int | None = None) -> str:
    timeout_seconds = HTTP_TIMEOUT_SECONDS if timeout is None else timeout
    retry_count = HTTP_RETRY_COUNT if retries is None else retries
    last_error: Exception | None = None
    for attempt in range(1, retry_count + 1):
        request = urllib.request.Request(url, headers={"User-Agent": "cobra-nas-import/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                data = response.read()
            for encoding in ("utf-8", "gbk"):
                try:
                    return data.decode(encoding)
                except UnicodeDecodeError:
                    continue
            return data.decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt >= retry_count:
                break
            print_progress(f"HTTP 请求失败，{attempt}/{retry_count} 次重试：{url} ({exc})")
            time.sleep(HTTP_RETRY_DELAY_SECONDS)
    raise RuntimeError(f"HTTP 请求失败：{url}") from last_error


def to_public_url(public_base_url: str, relative_path: str) -> str:
    encoded = urllib.parse.quote(relative_path.replace("\\", "/"), safe="/")
    return public_base_url.rstrip("/") + "/" + encoded


def analyze(
    source_url: str | None,
    local_root: str | None,
    public_base_url: str | None,
    image_url: str | None = None,
    xml_url: str | None = None,
) -> dict:
    print_progress("开始扫描数据源...")
    files = list_source_files(source_url, local_root, public_base_url, image_url, xml_url)
    print_progress(f"扫描完成：发现 {len(files)} 个图片/XML 文件")
    return analyze_files(files, source_url or local_root or {"imageUrl": image_url, "xmlUrl": xml_url})


def build_split_dir_batch_report(
    batch: SplitDirBatch,
    image_url: str,
    xml_url: str,
    checkpoint: ImportCheckpoint | None = None,
    checkpoint_path: str | None = None,
) -> dict:
    total_dirs = len(batch.dir_pairs)
    print_progress(
        f"开始扫描批次 {batch.index}：目录 {total_dirs} 个，预计图片 {batch.estimated_image_count} 张"
    )
    if checkpoint is not None:
        checkpoint.begin_active_batch(batch.index)
        save_import_checkpoint(checkpoint_path, checkpoint, force=True)
    files: list[SourceFile] = []
    if checkpoint is not None and checkpoint.active_batch_index == batch.index and checkpoint.active_batch_files:
        files = [
            SourceFile(
                name=str(item.get("name") or ""),
                url=str(item.get("url") or ""),
                local_path=item.get("local_path"),
            )
            for item in checkpoint.active_batch_files
        ]
        print_progress(f"批次 {batch.index} 从检查点恢复：已跳过 {len(checkpoint.active_batch_scanned_dirs)} 个目录，累计 {len(files)} 个文件")
    for dir_index, pair in enumerate(batch.dir_pairs, start=1):
        scan_key = pair.image_dir.relative_dir
        if checkpoint is not None and checkpoint.is_active_batch_dir_scanned(scan_key):
            print_progress(f"批次 {batch.index} 跳过已扫描目录：{scan_key or '.'}")
            continue
        print_batch_file_scan_progress(batch.index, dir_index, total_dirs, pair.image_dir.relative_dir, len(files))
        before_count = len(files)
        dir_scan_started = time.monotonic()
        try:
            dir_files = list_http_files_in_directory(pair.image_dir, IMAGE_SUFFIXES)
            dir_files.extend(list_http_files_in_directory(pair.xml_dir, {XML_SUFFIX}))
            files.extend(dir_files)
        except Exception as exc:
            if is_skippable_directory_error(exc):
                handle_skippable_directory_scan_failure(
                    scan_key,
                    exc,
                    phase=f"批次 {batch.index} 文件扫描：",
                )
                if checkpoint is not None:
                    checkpoint.mark_active_batch_dir_scanned(scan_key, [])
                    save_import_checkpoint(checkpoint_path, checkpoint, force=True)
                continue
            if checkpoint is not None:
                checkpoint.record_error(exc)
                save_import_checkpoint(checkpoint_path, checkpoint, force=True)
            raise RuntimeError(f"批次 {batch.index} 文件扫描失败：{scan_key or '.'}") from exc
        if checkpoint is not None:
            checkpoint.mark_active_batch_dir_scanned(scan_key, dir_files)
            save_import_checkpoint(checkpoint_path, checkpoint, force=True)
        added_count = len(files) - before_count
        dir_scan_elapsed = time.monotonic() - dir_scan_started
        if added_count:
            print_progress(
                f"批次 {batch.index} 文件扫描：当前目录新增 {added_count} 个文件，"
                f"累计 {len(files)} 个，本目录用时 {dir_scan_elapsed:.1f}s"
            )
        if len(files) % SCAN_PROGRESS_EVERY == 0 and len(files) > before_count:
            print_progress(f"批次 {batch.index} 文件扫描：累计已发现 {len(files)} 个图片/XML 文件")
    print_progress(f"批次 {batch.index} 扫描完成：发现 {len(files)} 个图片/XML 文件")
    if checkpoint is not None:
        checkpoint.mark_batch_file_scan_completed(len(files))
        save_import_checkpoint(checkpoint_path, checkpoint, force=True)
        print_checkpoint_artifacts(checkpoint_path)
        if _ACTIVE_CHECKPOINT_FILE is not None:
            files = load_batch_files_from_sidecar(_ACTIVE_CHECKPOINT_FILE, batch.index)
            checkpoint.active_batch_files.clear()
    return analyze_files(
        files,
        {
            "imageUrl": image_url,
            "xmlUrl": xml_url,
            "batchIndex": batch.index,
            "directories": [
                {"imageDir": pair.image_dir.relative_dir, "xmlDir": pair.xml_dir.relative_dir}
                for pair in batch.dir_pairs
            ],
        },
        batch_index=batch.index,
        checkpoint=checkpoint,
        checkpoint_path=checkpoint_path,
    )


def print_batch_file_scan_progress(
    batch_index: int,
    dir_index: int,
    total_dirs: int,
    current_dir: str,
    file_count: int,
) -> None:
    current_path = current_dir or "."
    print_progress(
        f"批次 {batch_index} 文件扫描：目录 {dir_index}/{total_dirs}，"
        f"已发现文件 {file_count}，当前目录 {current_path}"
    )


def analyze_files(
    files: list[SourceFile],
    source: Any,
    batch_index: int | None = None,
    checkpoint: ImportCheckpoint | None = None,
    checkpoint_path: str | None = None,
) -> dict:
    image_files = [file.to_dict() for file in files if Path(file.name).suffix.lower() in IMAGE_SUFFIXES]
    raw_xml_files = [file.to_dict() for file in files if Path(file.name).suffix.lower() == XML_SUFFIX]

    parsed_xml_files = []
    xml_errors = []
    progress_prefix = f"批次 {batch_index}：" if batch_index is not None else ""
    parsed_xml_cache: dict[str, dict] = {}
    if checkpoint is not None and batch_index is not None:
        checkpoint.phase = "xml_parse"
        cache_file = resolve_checkpoint_path(checkpoint_path)
        parsed_xml_cache = load_parsed_xml_cache(cache_file, batch_index)
        if checkpoint.legacy_parsed_xml_by_url:
            parsed_xml_cache.update(checkpoint.legacy_parsed_xml_by_url)
            checkpoint.legacy_parsed_xml_by_url = None
        if parsed_xml_cache:
            print_progress(f"{progress_prefix}从检查点恢复：已解析 XML {len(parsed_xml_cache)} 个")
        save_import_checkpoint(checkpoint_path, checkpoint, force=True)
        print_checkpoint_artifacts(checkpoint_path)
    print_progress(f"{progress_prefix}开始解析 XML：待解析 {len(raw_xml_files)} 个")
    for xml_file in raw_xml_files:
        xml_url = str(xml_file.get("url") or "")
        cached = parsed_xml_cache.get(xml_url) if xml_url else None
        if cached is not None:
            parsed_xml_files.append(cached)
            parsed_count = len(parsed_xml_files) + len(xml_errors)
            if parsed_count == len(raw_xml_files) or parsed_count % XML_PROGRESS_EVERY == 0:
                print_progress(f"{progress_prefix}XML 解析进度：{parsed_count}/{len(raw_xml_files)}")
            continue
        try:
            parsed = parse_voc_xml(read_file_text(xml_file))
            merged = {**xml_file, **parsed}
            parsed_xml_files.append(merged)
            parsed_xml_cache[xml_url] = merged
            if checkpoint is not None and xml_url:
                checkpoint.mark_parsed_xml(xml_url, merged)
                parsed_count = len(parsed_xml_files) + len(xml_errors)
                if parsed_count % XML_PROGRESS_EVERY == 0:
                    flushed = checkpoint.flush_parsed_xml_sidecar()
                    if flushed:
                        print_progress(f"{progress_prefix}检查点侧车已落盘 XML {flushed} 条")
                    save_import_checkpoint(checkpoint_path, checkpoint, force=True)
        except RuntimeError as exc:
            if "HTTP 请求失败" in str(exc):
                if checkpoint is not None:
                    checkpoint.record_error(exc)
                    checkpoint.flush_parsed_xml_sidecar()
                    save_import_checkpoint(checkpoint_path, checkpoint, force=True)
                raise RuntimeError(f"{progress_prefix}XML 读取失败：{xml_file['name']}") from exc
            xml_errors.append({"name": xml_file["name"], "url": xml_file["url"], "error": str(exc)})
        except (ET.ParseError, UnicodeDecodeError, urllib.error.URLError, OSError) as exc:
            xml_errors.append({"name": xml_file["name"], "url": xml_file["url"], "error": str(exc)})
        parsed_count = len(parsed_xml_files) + len(xml_errors)
        if parsed_count == len(raw_xml_files) or parsed_count % XML_PROGRESS_EVERY == 0:
            print_progress(f"{progress_prefix}XML 解析进度：{parsed_count}/{len(raw_xml_files)}")
    if checkpoint is not None:
        flushed = checkpoint.flush_parsed_xml_sidecar()
        if flushed:
            print_progress(f"{progress_prefix}检查点侧车已落盘 XML {flushed} 条")
        save_import_checkpoint(checkpoint_path, checkpoint, force=True)

    print_progress(f"{progress_prefix}开始配对图片和 XML...")
    pairs, unmatched_images, unmatched_xml = pair_images_and_xml(image_files, parsed_xml_files)
    annotated_pairs = [pair for pair in pairs if pair["xml"].get("objects")]
    empty_annotation_pairs = [pair for pair in pairs if not pair["xml"].get("objects")]
    classification_count = build_classification_count(annotated_pairs)
    ids_by_label = label_id_map(classification_count)
    print_progress(
        f"{progress_prefix}预检完成：图片 {len(image_files)}，XML {len(raw_xml_files)}，"
        f"配对 {len(pairs)}，有标注 {len(annotated_pairs)}，空标注 {len(empty_annotation_pairs)}，"
        f"可导入 {len(pairs)}，XML 错误 {len(xml_errors)}"
    )

    return {
        "source": source,
        "totalFiles": len(files),
        "imageCount": len(image_files),
        "xmlCount": len(raw_xml_files),
        "parsedXmlCount": len(parsed_xml_files),
        "pairedCount": len(pairs),
        "emptyAnnotationPairCount": len(empty_annotation_pairs),
        "annotatedPairCount": len(annotated_pairs),
        "unmatchedImageCount": len(unmatched_images),
        "unmatchedXmlCount": len(unmatched_xml),
        "xmlErrorCount": len(xml_errors),
        "labels": classification_count,
        "pairs": [
            {
                "name": pair["image"]["name"],
                "address": pair["image"]["url"],
                "thumbnail": pair["image"]["url"],
                "xml": pair["xml"]["url"],
                "width": pair["xml"].get("width"),
                "height": pair["xml"].get("height"),
                "classification": build_image_classification(pair["xml"], ids_by_label),
            }
            for pair in pairs
        ],
        "unmatchedImages": unmatched_images,
        "unmatchedXml": unmatched_xml,
        "xmlErrors": xml_errors[:50],
    }


def write_report(report: dict, output_path: str | None) -> None:
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if output_path:
        Path(output_path).write_text(text, encoding="utf-8")
    else:
        print(text)


def import_split_dirs_in_batches(
    image_url: str,
    xml_url: str,
    import_options: ImportOptions,
    db_options: DbOptions,
    batch_size: int = 50_000,
    start_batch: int = 1,
    stop_after_batches: int | None = None,
    batch_report_dir: str | None = None,
    checkpoint_path: str | None = None,
    reset_checkpoint: bool = False,
    auto_resume: bool = True,
) -> dict:
    if start_batch <= 0:
        raise ValueError("--start-batch 必须大于 0")
    if stop_after_batches is not None and stop_after_batches <= 0:
        raise ValueError("--stop-after-batches 必须大于 0")

    resolved_checkpoint = resolve_checkpoint_path(checkpoint_path)
    checkpoint = load_import_checkpoint(str(resolved_checkpoint), image_url, xml_url, batch_size, reset=reset_checkpoint)
    checkpoint_path = str(resolved_checkpoint)
    print_progress(f"检查点文件：{resolved_checkpoint}")
    save_import_checkpoint(checkpoint_path, checkpoint, force=True)
    print_checkpoint_artifacts(checkpoint_path)
    install_checkpoint_guard(checkpoint_path, checkpoint)
    if auto_resume and start_batch == 1 and checkpoint.completed_batch_indexes:
        start_batch = checkpoint.suggest_start_batch()
        print_progress(f"根据检查点自动从批次 {start_batch} 继续")

    report_dir = Path(batch_report_dir) if batch_report_dir else None
    if report_dir:
        report_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "batchCount": 0,
        "plannedBatchCount": 0,
        "rawDetailCount": 0,
        "skippedDuplicateCount": 0,
        "batches": [],
        "checkpointFile": checkpoint_path,
    }
    processed_batch_count = 0
    try:
        for batch in iter_split_dir_batches_dfs(
            image_url,
            xml_url,
            batch_size,
            checkpoint=checkpoint,
            checkpoint_path=checkpoint_path,
        ):
            summary["plannedBatchCount"] = max(summary["plannedBatchCount"], batch.index)
            if batch.index in checkpoint.completed_batch_indexes:
                print_progress(f"跳过批次 {batch.index}：检查点记录为已完成")
                continue
            if batch.index < start_batch:
                print_progress(f"跳过批次 {batch.index}：早于 --start-batch {start_batch}")
                continue
            if stop_after_batches is not None and processed_batch_count >= stop_after_batches:
                break

            report = build_split_dir_batch_report(
                batch,
                image_url,
                xml_url,
                checkpoint=checkpoint,
                checkpoint_path=checkpoint_path,
            )
            if report_dir:
                write_report(report, str(report_dir / f"nas_import_batch_{batch.index:04d}.json"))
            if not report.get("pairs"):
                print_progress(f"跳过批次 {batch.index}：没有可入库图片")
                checkpoint.mark_batch_completed(batch.index)
                save_import_checkpoint(checkpoint_path, checkpoint, force=True)
                summary["batches"].append({"batchIndex": batch.index, "status": "skipped", "rawDetailCount": 0})
                processed_batch_count += 1
                continue

            batch_options = with_batch_import_names(import_options, batch.index)
            checkpoint.phase = "db_write"
            print_progress(
                f"批次 {batch.index} 开始写入数据库：报告配对 {len(report.get('pairs') or [])} 张"
                f"（写库时将自动跳过 raw_dataset_detail 中已存在的 name/address）"
            )
            save_import_checkpoint(checkpoint_path, checkpoint, force=True)
            try:
                result = write_database(
                    report,
                    batch_options,
                    db_options,
                    checkpoint=checkpoint,
                    checkpoint_path=checkpoint_path,
                )
            except Exception as exc:
                checkpoint.record_error(exc)
                save_import_checkpoint(checkpoint_path, checkpoint, force=True)
                raise RuntimeError(f"批次 {batch.index} 写入失败：{format_failure_message(exc)}") from exc

            if result.get("alreadyImported"):
                print_progress(f"批次 {batch.index} 已全部存在于数据库，标记为完成")

            checkpoint.mark_batch_completed(batch.index)
            save_import_checkpoint(checkpoint_path, checkpoint, force=True)
            summary["batchCount"] += 1
            summary["rawDetailCount"] += int(result.get("rawDetailCount") or 0)
            summary["skippedDuplicateCount"] += int(result.get("skippedDuplicateCount") or 0)
            summary["batches"].append({"batchIndex": batch.index, "status": "done", **result})
            processed_batch_count += 1
    except Exception as exc:
        checkpoint.record_error(exc)
        save_import_checkpoint(checkpoint_path, checkpoint, force=True)
        print_progress(
            f"导入中断，进度已写入检查点：{resolved_checkpoint}。"
            f"详情：{format_failure_message(exc)}。"
            f"修复后使用相同命令重试即可继续。"
        )
        raise
    finally:
        clear_checkpoint_guard()

    print_progress(
        f"分批导入完成：完成批次 {summary['batchCount']}/{summary['plannedBatchCount']}，"
        f"写入图片 {summary['rawDetailCount']}，跳过重复 {summary['skippedDuplicateCount']}"
    )
    return summary


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import annotated NAS/MinIO image URLs into Cobra tables.")
    parser.add_argument("--source-url", help="HTTP/MinIO prefix URL, for example http://localhost:9000/cobra/prefix")
    parser.add_argument("--image-url", help="HTTP directory URL containing images, used together with --xml-url")
    parser.add_argument("--xml-url", help="HTTP directory URL containing VOC XML files, used together with --image-url")
    parser.add_argument("--local-root", help="Mounted NAS local root path")
    parser.add_argument("--public-base-url", help="Public URL base used with --local-root")
    parser.add_argument("--report-input", help="Optional: reuse an existing dry-run report JSON instead of scanning source")
    parser.add_argument("--output", help="Write scan/import report JSON to this path")
    parser.add_argument("--write-db", action="store_true", help="Actually insert task/dataset/detail rows into MySQL")
    parser.add_argument("--dataset-name", help="Dataset name used when --write-db is set; default: auto from source date")
    parser.add_argument("--business-domain", help="dataset.business_domain used when --write-db is set")
    parser.add_argument("--business-scenario", help="dataset.business_scenario / tag_scene id used when --write-db is set")
    parser.add_argument("--created-by", type=int, default=1, help="task_entity.created_by, default: 1")
    parser.add_argument("--executor", type=int, default=1, help="task_entity.executor, default: 1")
    parser.add_argument("--task-name", help="task_entity.name, default: 知识融合-{dataset-name}")
    parser.add_argument("--task-description", help="task_entity.description")
    parser.add_argument("--modality", default="image", help="dataset.modality, default: image")
    parser.add_argument("--max-import-count", type=int, help="Limit how many annotated image/XML pairs are inserted this run")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50_000,
        help="When --image-url/--xml-url and --write-db are set, import roughly N images per DB batch. Default: 50000",
    )
    parser.add_argument(
        "--start-batch",
        type=int,
        default=1,
        help="When batch importing split image/xml dirs, start from this 1-based batch index. Default: 1",
    )
    parser.add_argument(
        "--stop-after-batches",
        type=int,
        help="When batch importing split image/xml dirs, stop after N processed batches",
    )
    parser.add_argument(
        "--batch-report-dir",
        help="When batch importing split image/xml dirs, write one dry-run report JSON per batch to this directory",
    )
    parser.add_argument(
        "--checkpoint-file",
        help="Checkpoint JSON used to resume split-dir batch import after timeout/failure. "
        f"Default: {default_checkpoint_path()}",
    )
    parser.add_argument(
        "--reset-checkpoint",
        action="store_true",
        help="Delete the checkpoint file before starting split-dir batch import",
    )
    parser.add_argument(
        "--no-auto-resume",
        action="store_true",
        help="Do not auto continue from the next unfinished batch recorded in checkpoint",
    )
    parser.add_argument(
        "--http-timeout",
        type=int,
        default=60,
        help="HTTP request timeout in seconds for NAS directory/file access. Default: 60",
    )
    parser.add_argument(
        "--http-retries",
        type=int,
        default=3,
        help="Retry count for failed HTTP directory/file requests. Default: 3",
    )
    parser.add_argument(
        "--http-retry-delay",
        type=float,
        default=HTTP_RETRY_DELAY_SECONDS,
        help="Seconds to wait between HTTP retries. Default: 2",
    )
    parser.add_argument(
        "--allow-duplicates",
        action="store_true",
        help="Allow inserting duplicate raw_dataset_detail name/address. Default: skip duplicates",
    )
    parser.add_argument(
        "--write-delay-every",
        type=int,
        default=100,
        help="When writing DB, sleep after every N inserted images. Default: 100",
    )
    parser.add_argument(
        "--write-delay-seconds",
        type=float,
        help="Sleep seconds for write throttling. Default: 0.1 when unlimited import, otherwise 0",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=500,
        help="Print DB write progress after every N inserted images. Default: 500. Use 0 to disable row progress",
    )
    parser.add_argument("--db-host", default=os.getenv("COBRA_DB_HOST", "127.0.0.1"))
    parser.add_argument("--db-port", type=int, default=int(os.getenv("COBRA_DB_PORT", "3306")))
    parser.add_argument("--db-name", default=os.getenv("COBRA_DB_NAME", "cobra"))
    parser.add_argument("--db-user", default=os.getenv("COBRA_DB_USER", "root"))
    parser.add_argument("--db-password", default=os.getenv("COBRA_DB_PASSWORD", ""))
    parser.add_argument(
        "--failure-status",
        default="reject",
        help="Task status written when DB import fails. Default: reject",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        if args.write_db:
            missing = [
                name
                for name, value in {
                    "--business-domain": args.business_domain,
                    "--business-scenario": args.business_scenario,
                }.items()
                if not value
            ]
            if missing:
                raise ValueError(f"--write-db 缺少必要参数: {', '.join(missing)}")
            import_options = ImportOptions(
                dataset_name=args.dataset_name,
                business_domain=args.business_domain,
                business_scenario=args.business_scenario,
                created_by=args.created_by,
                executor=args.executor,
                task_name=args.task_name,
                task_description=args.task_description,
                modality=args.modality,
                max_import_count=args.max_import_count,
                allow_duplicates=args.allow_duplicates,
            )
            db_options = DbOptions(
                host=args.db_host,
                port=args.db_port,
                database=args.db_name,
                user=args.db_user,
                password=args.db_password,
                write_delay_every=args.write_delay_every,
                write_delay_seconds=(
                    args.write_delay_seconds
                    if args.write_delay_seconds is not None
                    else (0.1 if args.max_import_count is None else 0.0)
                ),
                progress_every=args.progress_every,
                failure_status=args.failure_status,
            )
            if not args.report_input and args.image_url and args.xml_url:
                configure_http_client(args.http_timeout, args.http_retries, args.http_retry_delay)
                result = import_split_dirs_in_batches(
                    args.image_url,
                    args.xml_url,
                    import_options,
                    db_options,
                    batch_size=args.batch_size,
                    start_batch=args.start_batch,
                    stop_after_batches=args.stop_after_batches,
                    batch_report_dir=args.batch_report_dir,
                    checkpoint_path=args.checkpoint_file,
                    reset_checkpoint=args.reset_checkpoint,
                    auto_resume=not args.no_auto_resume,
                )
                if args.output:
                    write_report(result, args.output)
                print(json.dumps({"dbWrite": result}, ensure_ascii=False, indent=2))
                return 0

        if args.report_input:
            report = json.loads(Path(args.report_input).read_text(encoding="utf-8"))
        else:
            report = analyze(args.source_url, args.local_root, args.public_base_url, args.image_url, args.xml_url)
        if args.output or not args.write_db:
            write_report(report, args.output)
        if args.write_db:
            result = write_database(
                report,
                import_options,
                db_options,
            )
            print(json.dumps({"dbWrite": result}, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(f"导入预检失败: {format_failure_message(exc)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
