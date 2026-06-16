#!/usr/bin/env python3
"""Backfill VOC XML annotations into the label table for existing raw_dataset_detail rows."""

from __future__ import annotations

import argparse
import atexit
import json
import os
import posixpath
import re
import signal
import sys
import time
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import nas_import
from nas_import import (
    DbOptions,
    HttpFileDirectory,
    XML_SUFFIX,
    append_jsonl_lines,
    canonical_http_dir_url,
    canonical_http_url,
    configure_http_client,
    format_db_timestamp,
    format_failure_message,
    is_skippable_directory_error,
    list_http_files_in_directory,
    normalize_pair_key,
    open_mysql_connection,
    parse_voc_xml,
    print_progress,
    read_jsonl,
    relative_http_name,
)

CHECKPOINT_VERSION = 1
DEFAULT_CHECKPOINT_FILE = "label_backfill_checkpoint.json"
SIDEcar_SUCCESS_STATUSES = frozenset({"done", "skipped_existing", "no_objects"})
SIDEcar_RETRY_STATUSES = frozenset({"xml_not_found", "http_error"})
_GUARD_CHECKPOINT: tuple[str, LabelBackfillCheckpoint] | None = None
_GUARD_FLUSH: Any = None


def register_interrupt_flush(callback: Any) -> None:
    global _GUARD_FLUSH
    _GUARD_FLUSH = callback


def clear_interrupt_flush() -> None:
    global _GUARD_FLUSH
    _GUARD_FLUSH = None


class XmlResolveError(Exception):
    def __init__(self, status: str, message: str, *, xml_url: str | None = None) -> None:
        self.status = status
        self.xml_url = xml_url
        super().__init__(message)


@dataclass
class LabelBackfillStats:
    images_processed: int = 0
    images_skipped_existing: int = 0
    images_no_objects: int = 0
    images_xml_not_found: int = 0
    images_http_error: int = 0
    labels_inserted: int = 0

    def merge_sidecar(self, status: str, label_count: int = 0) -> None:
        self.images_processed += 1
        if status == "done":
            self.labels_inserted += label_count
        elif status == "skipped_existing":
            self.images_skipped_existing += 1
        elif status == "no_objects":
            self.images_no_objects += 1
        elif status == "xml_not_found":
            self.images_xml_not_found += 1
        elif status == "http_error":
            self.images_http_error += 1


@dataclass
class LabelBackfillCheckpoint:
    image_url: str
    xml_url: str
    dataset_ids: list[int]
    completed_dataset_ids: list[int] = field(default_factory=list)
    active_dataset_id: int | None = None
    stats: LabelBackfillStats = field(default_factory=LabelBackfillStats)
    last_error: str | None = None

    def to_dict(self) -> dict:
        return {
            "version": CHECKPOINT_VERSION,
            "imageUrl": self.image_url,
            "xmlUrl": self.xml_url,
            "datasetIds": self.dataset_ids,
            "completedDatasetIds": sorted(self.completed_dataset_ids),
            "activeDatasetId": self.active_dataset_id,
            "stats": {
                "imagesProcessed": self.stats.images_processed,
                "imagesSkippedExisting": self.stats.images_skipped_existing,
                "imagesNoObjects": self.stats.images_no_objects,
                "imagesXmlNotFound": self.stats.images_xml_not_found,
                "imagesHttpError": self.stats.images_http_error,
                "labelsInserted": self.stats.labels_inserted,
            },
            "lastError": self.last_error,
            "updatedAt": format_db_timestamp(),
        }

    @classmethod
    def from_dict(cls, payload: dict) -> LabelBackfillCheckpoint:
        stats_payload = payload.get("stats") or {}
        return cls(
            image_url=str(payload.get("imageUrl") or ""),
            xml_url=str(payload.get("xmlUrl") or ""),
            dataset_ids=[int(value) for value in payload.get("datasetIds") or []],
            completed_dataset_ids=[int(value) for value in payload.get("completedDatasetIds") or []],
            active_dataset_id=payload.get("activeDatasetId"),
            stats=LabelBackfillStats(
                images_processed=int(stats_payload.get("imagesProcessed") or 0),
                images_skipped_existing=int(stats_payload.get("imagesSkippedExisting") or 0),
                images_no_objects=int(stats_payload.get("imagesNoObjects") or 0),
                images_xml_not_found=int(stats_payload.get("imagesXmlNotFound") or 0),
                images_http_error=int(stats_payload.get("imagesHttpError") or 0),
                labels_inserted=int(stats_payload.get("labelsInserted") or 0),
            ),
            last_error=payload.get("lastError"),
        )


def default_checkpoint_path() -> Path:
    return Path(__file__).resolve().parent / DEFAULT_CHECKPOINT_FILE


def resolve_checkpoint_path(checkpoint_path: str | None) -> Path:
    if checkpoint_path:
        return Path(checkpoint_path).expanduser().resolve()
    return default_checkpoint_path()


def dataset_sidecar_path(checkpoint_file: Path, dataset_id: int) -> Path:
    return checkpoint_file.parent / f"{checkpoint_file.stem}.dataset_{dataset_id}.jsonl"


def save_checkpoint(checkpoint_file: Path, checkpoint: LabelBackfillCheckpoint) -> None:
    checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
    temp_path = checkpoint_file.with_suffix(checkpoint_file.suffix + ".tmp")
    with open(temp_path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(checkpoint.to_dict(), ensure_ascii=False, indent=2))
        handle.flush()
        os.fsync(handle.fileno())
    temp_path.replace(checkpoint_file)


def load_checkpoint(
    checkpoint_file: Path,
    image_url: str,
    xml_url: str,
    dataset_ids: list[int],
    *,
    reset: bool,
) -> LabelBackfillCheckpoint:
    if reset and checkpoint_file.exists():
        checkpoint_file.unlink()
        for path in checkpoint_file.parent.glob(f"{checkpoint_file.stem}.dataset_*.jsonl"):
            path.unlink()
    if checkpoint_file.exists():
        payload = json.loads(checkpoint_file.read_text(encoding="utf-8"))
        checkpoint = LabelBackfillCheckpoint.from_dict(payload)
        if checkpoint.image_url != image_url or checkpoint.xml_url != xml_url:
            raise ValueError("检查点与当前 --image-url/--xml-url 不一致，请更换 --checkpoint-file 或添加 --reset-checkpoint")
        if checkpoint.dataset_ids != dataset_ids:
            raise ValueError("检查点与当前 --dataset-ids 不一致，请更换 --checkpoint-file 或添加 --reset-checkpoint")
        print_progress(
            f"从检查点恢复：已完成 dataset {len(checkpoint.completed_dataset_ids)}/{len(checkpoint.dataset_ids)}，"
            f"累计插入 label {checkpoint.stats.labels_inserted} 条"
        )
        return checkpoint
    return LabelBackfillCheckpoint(image_url=image_url, xml_url=xml_url, dataset_ids=dataset_ids)


def install_checkpoint_guard(checkpoint_file: Path, checkpoint: LabelBackfillCheckpoint) -> None:
    global _GUARD
    _GUARD = (str(checkpoint_file), checkpoint)
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
    global _GUARD
    _GUARD = None


def _flush_checkpoint_guard() -> None:
    if _GUARD is None:
        return
    path, checkpoint = _GUARD
    try:
        save_checkpoint(Path(path), checkpoint)
    except OSError as exc:
        print_progress(f"退出时检查点写入失败：{path} ({exc})")


def _checkpoint_signal_handler(signum: int, frame: Any) -> None:
    _flush_checkpoint_guard()
    print_progress(f"收到中断信号，检查点已保存：{signum}")
    raise SystemExit(128 + signum if signum < 128 else 1)


def parse_dataset_ids(value: str) -> list[int]:
    ids: list[int] = []
    for part in value.split(","):
        token = part.strip()
        if not token:
            continue
        range_match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", token)
        if range_match:
            start = int(range_match.group(1))
            end = int(range_match.group(2))
            if end < start:
                raise ValueError(f"无效的 dataset 范围：{token}")
            ids.extend(range(start, end + 1))
            continue
        ids.append(int(token))
    if not ids:
        raise ValueError("--dataset-ids 不能为空")
    return ids


def normalize_relative_name(name: str, address: str, image_root_url: str) -> str:
    cleaned = name.replace("\\", "/").strip("/")
    if cleaned:
        return cleaned
    return relative_http_name(image_root_url, address).strip("/")


def join_http_url(base_dir_url: str, relative_path: str) -> str:
    """Join a relative path onto an HTTP directory URL with per-segment encoding."""
    parsed = urllib.parse.urlparse(canonical_http_dir_url(base_dir_url))
    parts = [part for part in relative_path.replace("\\", "/").split("/") if part]
    encoded_parts = [urllib.parse.quote(urllib.parse.unquote(part), safe="") for part in parts]
    path = parsed.path.rstrip("/")
    if encoded_parts:
        path = f"{path}/{'/'.join(encoded_parts)}"
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def build_direct_xml_url(xml_root_url: str, relative_image_name: str) -> str:
    rel = relative_image_name.replace("\\", "/").strip("/")
    stem, _ = posixpath.splitext(rel)
    xml_rel = f"{stem}{XML_SUFFIX}" if stem else rel
    return join_http_url(xml_root_url, xml_rel)


def image_address_to_xml_url(image_address: str, image_root_url: str, xml_root_url: str) -> str:
    """Derive XML URL from the stored image address, matching nas_import scan URLs."""
    relative_path = relative_http_name(canonical_http_dir_url(image_root_url), image_address)
    stem, _ = posixpath.splitext(relative_path)
    xml_relative = f"{stem}{XML_SUFFIX}" if stem else relative_path
    return join_http_url(xml_root_url, xml_relative)


def xml_directory_for_image(
    image_address: str,
    image_root_url: str,
    xml_root_url: str,
) -> tuple[str, str]:
    xml_file_url = image_address_to_xml_url(image_address, image_root_url, xml_root_url)
    parsed = urllib.parse.urlparse(xml_file_url)
    parent_path = posixpath.dirname(parsed.path)
    if not parent_path.endswith("/"):
        parent_path += "/"
    directory_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parent_path, "", "", ""))
    directory_rel = relative_http_name(canonical_http_dir_url(xml_root_url), directory_url).strip("/")
    return directory_url, directory_rel


class XmlDirectoryCache:
    def __init__(self, xml_root_url: str) -> None:
        self.xml_root_url = canonical_http_dir_url(xml_root_url)
        self._files_by_dir: dict[str, list[tuple[str, str]]] = {}

    def list_xml_files(self, directory_url: str, directory_rel: str) -> list[tuple[str, str]]:
        cache_key = directory_url
        if cache_key not in self._files_by_dir:
            listing = list_http_files_in_directory(
                HttpFileDirectory(relative_dir=directory_rel, url=directory_url, file_count=0),
                {XML_SUFFIX},
            )
            self._files_by_dir[cache_key] = [
                (posixpath.basename(item.name.replace("\\", "/")), item.url) for item in listing
            ]
        return self._files_by_dir[cache_key]

    def find_by_pair_key(self, directory_url: str, directory_rel: str, image_basename: str) -> str | None:
        target_key = normalize_pair_key(image_basename)
        for basename, url in self.list_xml_files(directory_url, directory_rel):
            if normalize_pair_key(basename) == target_key:
                return url
        return None


def classify_fetch_error(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, XmlResolveError):
        return exc.status, str(exc)
    if isinstance(exc, ET.ParseError):
        return "xml_parse_error", f"XML 解析失败：{exc}"
    if is_skippable_directory_error(exc):
        return "xml_not_found", format_failure_message(exc)
    if isinstance(exc, UnicodeEncodeError):
        return "http_error", f"URL 编码失败（路径含非 ASCII 字符但未正确转义）：{exc}"
    message = format_failure_message(exc)
    status = nas_import.extract_http_status(exc)
    if status is not None:
        return ("xml_not_found" if status in nas_import.HTTP_SKIPPABLE_STATUS_CODES else "http_error", message)
    if "HTTP 请求失败" in message:
        return "xml_not_found" if is_skippable_directory_error(exc) else "http_error", message
    return "http_error", message


def fetch_xml_text(url: str) -> str:
    try:
        return nas_import.read_url_text(url)
    except Exception as exc:
        status, message = classify_fetch_error(exc)
        raise XmlResolveError(status, message, xml_url=url) from exc


def load_paired_xml(
    image_address: str,
    relative_image_name: str,
    image_root_url: str,
    xml_root_url: str,
    cache: XmlDirectoryCache,
) -> tuple[str, dict]:
    candidates: list[str] = [image_address_to_xml_url(image_address, image_root_url, xml_root_url)]
    name_based = build_direct_xml_url(xml_root_url, relative_image_name)
    if name_based not in candidates:
        candidates.append(name_based)

    last_error: XmlResolveError | None = None
    for xml_url in candidates:
        try:
            parsed = parse_voc_xml(fetch_xml_text(xml_url))
            return xml_url, parsed
        except XmlResolveError as exc:
            last_error = exc
            if exc.status == "http_error":
                continue
        except ET.ParseError as exc:
            raise XmlResolveError("xml_parse_error", f"XML 解析失败：{exc}", xml_url=xml_url) from exc

    image_basename = posixpath.basename(relative_image_name.replace("\\", "/"))
    directory_url, directory_rel = xml_directory_for_image(image_address, image_root_url, xml_root_url)
    matched = cache.find_by_pair_key(directory_url, directory_rel, image_basename)
    if matched is not None and matched not in candidates:
        try:
            parsed = parse_voc_xml(fetch_xml_text(matched))
            return matched, parsed
        except XmlResolveError as exc:
            last_error = exc
        except ET.ParseError as exc:
            raise XmlResolveError("xml_parse_error", f"XML 解析失败：{exc}", xml_url=matched) from exc

    if last_error is not None:
        raise last_error
    raise XmlResolveError(
        "xml_not_found",
        f"未找到可配对的 XML：{relative_image_name}",
        xml_url=candidates[0],
    )


def build_label_rows(
    image_id: int,
    image_url: str,
    objects: list[dict],
    create_time: str,
) -> list[dict]:
    label_map: dict[str, int] = {}
    next_label_id = -1
    rows: list[dict] = []
    for obj in objects:
        name = str(obj.get("name") or "").strip()
        bbox = obj.get("bbox") or {}
        xmin = bbox.get("xmin")
        ymin = bbox.get("ymin")
        xmax = bbox.get("xmax")
        ymax = bbox.get("ymax")
        if not name or None in (xmin, ymin, xmax, ymax):
            continue
        if name not in label_map:
            label_map[name] = next_label_id
            next_label_id -= 1
        rows.append(
            {
                "point_x": int(xmin),
                "point_y": int(ymin),
                "width": float(xmax) - float(xmin),
                "height": float(ymax) - float(ymin),
                "image_id": image_id,
                "label": label_map[name],
                "score": None,
                "create_time": create_time,
                "is_delete": 0,
                "label_name": name,
                "image_url": image_url,
            }
        )
    return rows


def insert_label_rows(cursor: Any, rows: list[dict]) -> int:
    if not rows:
        return 0
    for row in rows:
        cursor.execute(
            """
            INSERT INTO label
              (point_x, point_y, width, height, image_id, label, score, create_time, is_delete, label_name, image_url)
            VALUES
              (%(point_x)s, %(point_y)s, %(width)s, %(height)s, %(image_id)s, %(label)s, %(score)s,
               %(create_time)s, %(is_delete)s, %(label_name)s, %(image_url)s)
            """,
            row,
        )
    return len(rows)


def image_has_labels(cursor: Any, image_id: int) -> bool:
    cursor.execute(
        """
        SELECT 1
        FROM label
        WHERE image_id = %s AND (is_delete = 0 OR is_delete IS NULL)
        LIMIT 1
        """,
        (image_id,),
    )
    return cursor.fetchone() is not None


def load_pending_raw_details(cursor: Any, dataset_id: int) -> list[dict]:
    """Load all pending rows upfront so write queries cannot truncate a streaming SELECT."""
    cursor.execute(
        """
        SELECT r.id, r.name, r.address
        FROM raw_dataset_detail r
        WHERE r.dataset_id = %s
          AND NOT EXISTS (
            SELECT 1
            FROM label l
            WHERE l.image_id = r.id
              AND (l.is_delete = 0 OR l.is_delete IS NULL)
          )
        ORDER BY r.id
        """,
        (dataset_id,),
    )
    return [
        {"id": int(row[0]), "name": str(row[1] or ""), "address": str(row[2] or "")}
        for row in cursor.fetchall()
    ]


def count_raw_details(cursor: Any, dataset_id: int) -> int:
    cursor.execute("SELECT COUNT(*) FROM raw_dataset_detail WHERE dataset_id = %s", (dataset_id,))
    row = cursor.fetchone()
    return int(row[0] if row else 0)


def load_sidecar_state(sidecar_path: Path) -> dict[int, str]:
    state: dict[int, str] = {}
    for item in read_jsonl(sidecar_path):
        raw_id = item.get("rawId")
        status = item.get("status")
        if raw_id is None or not status:
            continue
        state[int(raw_id)] = str(status)
    return state


def should_skip_sidecar(status: str, *, retry_failed: bool) -> bool:
    if retry_failed and status in SIDEcar_RETRY_STATUSES:
        return False
    return status in SIDEcar_SUCCESS_STATUSES or status in SIDEcar_RETRY_STATUSES


def sidecar_has_retryable(sidecar_path: Path) -> bool:
    return any(status in SIDEcar_RETRY_STATUSES for status in load_sidecar_state(sidecar_path).values())


def process_image_row(
    row: dict,
    *,
    image_root_url: str,
    xml_root_url: str,
    cache: XmlDirectoryCache,
    cursor: Any,
    create_time: str,
) -> tuple[str, int, str | None, str | None]:
    raw_id = int(row["id"])
    if image_has_labels(cursor, raw_id):
        return "skipped_existing", 0, None, None

    relative_name = normalize_relative_name(row["name"], row["address"], image_root_url)
    try:
        xml_url, parsed = load_paired_xml(
            row["address"],
            relative_name,
            image_root_url,
            xml_root_url,
            cache,
        )
    except XmlResolveError as exc:
        return exc.status, 0, exc.xml_url, str(exc)

    objects = parsed.get("objects") or []
    label_rows = build_label_rows(raw_id, row["address"], objects, create_time)
    if not label_rows:
        return "no_objects", 0, xml_url, None
    inserted = insert_label_rows(cursor, label_rows)
    return "done", inserted, xml_url, None


def backfill_dataset(
    connection: Any,
    dataset_id: int,
    *,
    image_root_url: str,
    xml_root_url: str,
    checkpoint_file: Path,
    checkpoint: LabelBackfillCheckpoint,
    commit_every: int,
    progress_every: int,
    retry_failed: bool,
) -> None:
    sidecar_path = dataset_sidecar_path(checkpoint_file, dataset_id)
    sidecar_state = load_sidecar_state(sidecar_path)
    cache = XmlDirectoryCache(xml_root_url)
    image_root = canonical_http_dir_url(image_root_url)
    xml_root = canonical_http_dir_url(xml_root_url)

    checkpoint.active_dataset_id = dataset_id
    save_checkpoint(checkpoint_file, checkpoint)
    print_progress(f"开始补录 dataset {dataset_id}，侧车：{sidecar_path}")

    with connection.cursor() as read_cursor:
        total_images = count_raw_details(read_cursor, dataset_id)
        pending_rows = load_pending_raw_details(read_cursor, dataset_id)
    pending_total = len(pending_rows)
    sidecar_skip_count = sum(
        1
        for row in pending_rows
        if sidecar_state.get(int(row["id"]))
        and should_skip_sidecar(sidecar_state[int(row["id"])], retry_failed=retry_failed)
    )
    print_progress(
        f"dataset {dataset_id} 统计：库中图片 {total_images} 张，待补录 {pending_total} 张，"
        f"侧车将跳过 {sidecar_skip_count} 张"
    )

    dataset_labels = 0
    dataset_processed = 0
    chunk_sidecar: list[dict] = []
    chunk_images = 0
    logged_http_errors = 0
    create_time = format_db_timestamp()

    with connection.cursor() as write_cursor:
        for row in pending_rows:
            raw_id = int(row["id"])
            prior_status = sidecar_state.get(raw_id)
            if prior_status and should_skip_sidecar(prior_status, retry_failed=retry_failed):
                continue

            started = time.monotonic()
            status, label_count, xml_url, error = process_image_row(
                row,
                image_root_url=image_root,
                xml_root_url=xml_root,
                cache=cache,
                cursor=write_cursor,
                create_time=create_time,
            )
            sidecar_item = {
                "rawId": raw_id,
                "status": status,
                "labelCount": label_count,
                "xmlUrl": xml_url,
                "name": row["name"],
                "error": error,
                "elapsedSeconds": round(time.monotonic() - started, 3),
            }
            chunk_sidecar.append(sidecar_item)
            checkpoint.stats.merge_sidecar(status, label_count)
            dataset_processed += 1
            dataset_labels += label_count
            chunk_images += 1

            if status == "http_error" and error and logged_http_errors < 5:
                logged_http_errors += 1
                print_progress(
                    f"dataset {dataset_id} HTTP 错误样例 #{logged_http_errors}："
                    f"rawId={raw_id}, name={row['name']}, xmlUrl={xml_url}, error={error}"
                )

            if progress_every > 0 and dataset_processed % progress_every == 0:
                print_progress(
                    f"dataset {dataset_id} 进度：已处理 {dataset_processed} 张，"
                    f"本集新增 label {dataset_labels} 条"
                )

            if chunk_images >= commit_every:
                connection.commit()
                append_jsonl_lines(sidecar_path, chunk_sidecar)
                chunk_sidecar.clear()
                chunk_images = 0
                save_checkpoint(checkpoint_file, checkpoint)

        if chunk_images > 0:
            connection.commit()
            append_jsonl_lines(sidecar_path, chunk_sidecar)

    if dataset_id not in checkpoint.completed_dataset_ids:
        checkpoint.completed_dataset_ids.append(dataset_id)
    checkpoint.active_dataset_id = None
    checkpoint.last_error = None
    save_checkpoint(checkpoint_file, checkpoint)
    print_progress(
        f"dataset {dataset_id} 补录完成：处理 {dataset_processed} 张，新增 label {dataset_labels} 条"
    )


def backfill_labels(
    image_url: str,
    xml_url: str,
    dataset_ids: list[int],
    db_options: DbOptions,
    *,
    checkpoint_path: str | None = None,
    reset_checkpoint: bool = False,
    commit_every: int = 500,
    progress_every: int = 1000,
    retry_failed: bool = False,
) -> dict:
    checkpoint_file = resolve_checkpoint_path(checkpoint_path)
    image_root = canonical_http_dir_url(image_url)
    xml_root = canonical_http_dir_url(xml_url)
    checkpoint = load_checkpoint(
        checkpoint_file,
        image_root,
        xml_root,
        dataset_ids,
        reset=reset_checkpoint,
    )
    install_checkpoint_guard(checkpoint_file, checkpoint)
    print_progress(f"检查点文件：{checkpoint_file}")

    connection = open_mysql_connection(db_options)
    try:
        for dataset_id in dataset_ids:
            sidecar_path = dataset_sidecar_path(checkpoint_file, dataset_id)
            if dataset_id in checkpoint.completed_dataset_ids:
                if retry_failed and sidecar_has_retryable(sidecar_path):
                    checkpoint.completed_dataset_ids.remove(dataset_id)
                    print_progress(f"dataset {dataset_id} 存在失败记录，按 --retry-failed 重新处理")
                else:
                    print_progress(f"跳过已完成 dataset：{dataset_id}")
                    continue
            try:
                backfill_dataset(
                    connection,
                    dataset_id,
                    image_root_url=image_root,
                    xml_root_url=xml_root,
                    checkpoint_file=checkpoint_file,
                    checkpoint=checkpoint,
                    commit_every=commit_every,
                    progress_every=progress_every,
                    retry_failed=retry_failed,
                )
            except Exception as exc:
                checkpoint.last_error = format_failure_message(exc)
                save_checkpoint(checkpoint_file, checkpoint)
                raise
    finally:
        clear_checkpoint_guard()
        connection.close()

    summary = checkpoint.to_dict()
    print_progress(
        f"全部补录完成：dataset {len(checkpoint.completed_dataset_ids)}/{len(dataset_ids)}，"
        f"累计插入 label {checkpoint.stats.labels_inserted} 条，"
        f"无标注 XML {checkpoint.stats.images_no_objects} 张，"
        f"未找到 XML {checkpoint.stats.images_xml_not_found} 张"
    )
    return summary


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill label rows from NAS XML for existing raw_dataset_detail records.",
    )
    parser.add_argument("--image-url", required=True, help="HTTP directory URL for image root")
    parser.add_argument("--xml-url", required=True, help="HTTP directory URL for XML root")
    parser.add_argument(
        "--dataset-ids",
        required=True,
        help="Dataset ids to backfill, e.g. 3922 or 3922-3957 or 3922,3923,3957",
    )
    parser.add_argument("--checkpoint-file", help=f"Checkpoint JSON path, default: {DEFAULT_CHECKPOINT_FILE}")
    parser.add_argument("--reset-checkpoint", action="store_true", help="Delete checkpoint and sidecars before run")
    parser.add_argument("--retry-failed", action="store_true", help="Retry sidecar records marked xml_not_found/http_error")
    parser.add_argument(
        "--commit-every",
        type=int,
        default=500,
        help="Commit to DB and flush sidecar every N processed images (does not limit total)",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=1000,
        help="Print progress every N processed images (does not limit total)",
    )
    parser.add_argument("--http-timeout", type=int, default=nas_import.HTTP_TIMEOUT_SECONDS)
    parser.add_argument("--http-retries", type=int, default=nas_import.HTTP_RETRY_COUNT)
    parser.add_argument("--http-retry-delay", type=float, default=nas_import.HTTP_RETRY_DELAY_SECONDS)
    parser.add_argument("--db-host", default="127.0.0.1")
    parser.add_argument("--db-port", type=int, default=3306)
    parser.add_argument("--db-name", default="cobra")
    parser.add_argument("--db-user", default="root")
    parser.add_argument("--db-password", default="")
    parser.add_argument("--db-charset", default="utf8mb4")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.commit_every <= 0:
        raise SystemExit("--commit-every 必须大于 0")
    configure_http_client(args.http_timeout, args.http_retries, args.http_retry_delay)
    dataset_ids = parse_dataset_ids(args.dataset_ids)
    db_options = DbOptions(
        host=args.db_host,
        port=args.db_port,
        database=args.db_name,
        user=args.db_user,
        password=args.db_password,
        charset=args.db_charset,
        progress_every=args.progress_every,
    )
    try:
        summary = backfill_labels(
            args.image_url,
            args.xml_url,
            dataset_ids,
            db_options,
            checkpoint_path=args.checkpoint_file,
            reset_checkpoint=args.reset_checkpoint,
            commit_every=args.commit_every,
            progress_every=args.progress_every,
            retry_failed=args.retry_failed,
        )
        print(json.dumps({"labelBackfill": summary}, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(f"label 补录失败: {format_failure_message(exc)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
