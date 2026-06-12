#!/usr/bin/env python3
"""Import annotated NAS/MinIO image URLs into Cobra-friendly records.

The script is intentionally standalone. By default it performs a dry run:
it traverses a NAS-like source, pairs images with VOC XML files, parses labels,
and prints a report. It does not upload files to MinIO.
"""

from __future__ import annotations

import argparse
import html.parser
import json
import os
import posixpath
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp"}
XML_SUFFIX = ".xml"
IGNORED_PAIR_DIRS = {"jpg", "jpeg", "image", "images", "img", "xml", "annotation", "annotations", "label", "labels"}
MAX_DATASET_ADDRESS_LENGTH = 200
MAX_TASK_DESCRIPTION_LENGTH = 500


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
    dataset_name: str
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
    progress_every: int = 10
    failure_status: str = "reject"


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
    images_by_basename: dict[str, dict] = {}
    images_by_key: dict[str, dict] = {}
    for image in images:
        basename = posixpath.basename(image["name"].replace("\\", "/")).lower()
        images_by_basename[basename] = image
        images_by_key.setdefault(normalize_pair_key(image["name"]), image)

    used_image_urls: set[str] = set()
    used_xml_urls: set[str] = set()
    pairs: list[dict] = []

    for xml_file in xml_files:
        image = None
        xml_filename = (xml_file.get("filename") or "").strip().lower()
        if xml_filename:
            image = images_by_basename.get(xml_filename)
        if image is None:
            image = images_by_key.get(normalize_pair_key(xml_file["name"]))
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
    raw_pairs = report.get("pairs") or []
    pairs, skipped_duplicate_count = filter_duplicate_pairs(
        raw_pairs,
        allow_duplicates=options.allow_duplicates,
        existing_names=existing_names or set(),
        existing_addresses=existing_addresses or set(),
    )
    if not pairs:
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
    task_name = task_base_name if task_base_name.startswith("数据导入-") else f"数据导入-{task_base_name}"
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


def write_database(report: dict, import_options: ImportOptions, db_options: DbOptions) -> dict:
    connection = open_mysql_connection(db_options)
    task_id: int | None = None
    dataset_id: int | None = None
    try:
        with connection.cursor() as cursor:
            existing_names: set[str] = set()
            existing_addresses: set[str] = set()
            if not import_options.allow_duplicates:
                existing_names, existing_addresses = load_existing_raw_details(cursor, report.get("pairs") or [])
            payload = build_db_payload(report, import_options, existing_names, existing_addresses)
            total_count = len(payload["rawDetails"])
            print(
                f"开始写入数据库：待写入 {total_count} 张图片，跳过重复 {payload.get('skippedDuplicateCount', 0)} 张",
                file=sys.stderr,
                flush=True,
            )
            task_id = insert_task(cursor, payload["task"])
            dataset_id = insert_dataset(cursor, payload["dataset"], task_id)
            connection.commit()

            print(f"导入任务已创建：taskId={task_id}, 状态=doing", file=sys.stderr, flush=True)
            inserted = insert_details(cursor, payload, dataset_id, db_options)
            update_dataset_total(cursor, dataset_id, inserted["rawDetailCount"])
            update_task_status(cursor, task_id, "done")
        connection.commit()
        print(
            f"数据库写入完成：taskId={task_id}, datasetId={dataset_id}, 状态=done, 图片={inserted['rawDetailCount']}",
            file=sys.stderr,
            flush=True,
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


def load_existing_raw_details(cursor: Any, pairs: list[dict]) -> tuple[set[str], set[str]]:
    names = sorted({pair.get("name") for pair in pairs if pair.get("name")})
    addresses = sorted({pair.get("address") for pair in pairs if pair.get("address")})
    existing_names: set[str] = set()
    existing_addresses: set[str] = set()

    for chunk in chunked(names, 500):
        placeholders = ", ".join(["%s"] * len(chunk))
        cursor.execute(f"SELECT name, address FROM raw_dataset_detail WHERE name IN ({placeholders})", chunk)
        for name, address in cursor.fetchall():
            if name:
                existing_names.add(name)
            if address:
                existing_addresses.add(address)

    for chunk in chunked(addresses, 500):
        placeholders = ", ".join(["%s"] * len(chunk))
        cursor.execute(f"SELECT name, address FROM raw_dataset_detail WHERE address IN ({placeholders})", chunk)
        for name, address in cursor.fetchall():
            if name:
                existing_names.add(name)
            if address:
                existing_addresses.add(address)

    return existing_names, existing_addresses


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


def insert_details(cursor: Any, payload: dict, dataset_id: int, db_options: DbOptions) -> dict:
    raw_count = 0
    raw_ids = allocate_seq_ids(cursor, "raw_dataset_detail_seq", len(payload["rawDetails"]))
    for raw_id, raw in zip(raw_ids, payload["rawDetails"]):
        raw_params = {**raw, "id": raw_id, "dataset_id": dataset_id}
        cursor.execute(
            """
            INSERT INTO raw_dataset_detail
              (id, name, address, dataset_id, thumbnail)
            VALUES
              (%(id)s, %(name)s, %(address)s, %(dataset_id)s, %(thumbnail)s)
            """,
            raw_params,
        )
        raw_count += 1
        maybe_print_progress(raw_count, len(payload["rawDetails"]), db_options)
        maybe_throttle_writes(raw_count, len(payload["rawDetails"]), db_options)
    return {"rawDetailCount": raw_count}


def format_db_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")


def maybe_print_progress(current_count: int, total_count: int, db_options: DbOptions) -> None:
    if total_count <= 0:
        return
    if db_options.progress_every <= 0:
        return
    if current_count != total_count and current_count % db_options.progress_every != 0:
        return
    percent = current_count * 100 / total_count
    print(f"写入进度：{current_count}/{total_count} ({percent:.1f}%)", file=sys.stderr, flush=True)


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
        return list_http_or_minio_files(source_url)
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
    return files


def list_split_http_dirs(image_url: str, xml_url: str) -> list[SourceFile]:
    return list_http_or_minio_files(image_url) + list_http_or_minio_files(xml_url)


def list_http_or_minio_files(source_url: str) -> list[SourceFile]:
    try:
        files = list_minio_public_prefix(source_url)
        if files:
            return files
    except Exception as exc:
        minio_error = exc
    else:
        minio_error = None

    try:
        files = list_http_index(source_url)
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


def list_http_index(source_url: str) -> list[SourceFile]:
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


def read_file_text(file_info: dict) -> str:
    if file_info.get("local_path"):
        return Path(file_info["local_path"]).read_text(encoding="utf-8")
    return read_url_text(file_info["url"])


def read_url_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "cobra-nas-import/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        data = response.read()
    for encoding in ("utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


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
    files = list_source_files(source_url, local_root, public_base_url, image_url, xml_url)
    image_files = [file.to_dict() for file in files if Path(file.name).suffix.lower() in IMAGE_SUFFIXES]
    raw_xml_files = [file.to_dict() for file in files if Path(file.name).suffix.lower() == XML_SUFFIX]

    parsed_xml_files = []
    xml_errors = []
    for xml_file in raw_xml_files:
        try:
            parsed = parse_voc_xml(read_file_text(xml_file))
            parsed_xml_files.append({**xml_file, **parsed})
        except (ET.ParseError, UnicodeDecodeError, urllib.error.URLError, OSError) as exc:
            xml_errors.append({"name": xml_file["name"], "url": xml_file["url"], "error": str(exc)})

    pairs, unmatched_images, unmatched_xml = pair_images_and_xml(image_files, parsed_xml_files)
    annotated_pairs = [pair for pair in pairs if pair["xml"].get("objects")]
    classification_count = build_classification_count(annotated_pairs)
    ids_by_label = label_id_map(classification_count)

    return {
        "source": source_url or local_root or {"imageUrl": image_url, "xmlUrl": xml_url},
        "totalFiles": len(files),
        "imageCount": len(image_files),
        "xmlCount": len(raw_xml_files),
        "parsedXmlCount": len(parsed_xml_files),
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
            for pair in annotated_pairs
        ],
        "unmatchedImages": unmatched_images[:50],
        "unmatchedXml": unmatched_xml[:50],
        "xmlErrors": xml_errors[:50],
    }


def write_report(report: dict, output_path: str | None) -> None:
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if output_path:
        Path(output_path).write_text(text, encoding="utf-8")
    else:
        print(text)


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
    parser.add_argument("--dataset-name", help="Dataset name used when --write-db is set")
    parser.add_argument("--business-domain", help="dataset.business_domain used when --write-db is set")
    parser.add_argument("--business-scenario", help="dataset.business_scenario / tag_scene id used when --write-db is set")
    parser.add_argument("--created-by", type=int, default=1, help="task_entity.created_by, default: 1")
    parser.add_argument("--executor", type=int, default=1, help="task_entity.executor, default: 1")
    parser.add_argument("--task-name", help="task_entity.name, default: 知识融合-{dataset-name}")
    parser.add_argument("--task-description", help="task_entity.description")
    parser.add_argument("--modality", default="image", help="dataset.modality, default: image")
    parser.add_argument("--max-import-count", type=int, help="Limit how many annotated image/XML pairs are inserted this run")
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
        default=10,
        help="Print DB write progress after every N inserted images. Default: 10. Use 0 to disable",
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
        if args.report_input:
            report = json.loads(Path(args.report_input).read_text(encoding="utf-8"))
        else:
            report = analyze(args.source_url, args.local_root, args.public_base_url, args.image_url, args.xml_url)
        if args.output or not args.write_db:
            write_report(report, args.output)
        if args.write_db:
            missing = [
                name
                for name, value in {
                    "--dataset-name": args.dataset_name,
                    "--business-domain": args.business_domain,
                    "--business-scenario": args.business_scenario,
                }.items()
                if not value
            ]
            if missing:
                raise ValueError(f"--write-db 缺少必要参数: {', '.join(missing)}")
            result = write_database(
                report,
                ImportOptions(
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
                ),
                DbOptions(
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
                ),
            )
            print(json.dumps({"dbWrite": result}, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(f"导入预检失败: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
