"""法律语料标准包契约校验。

只做纯内存校验：不连数据库、不落盘、不产生删除动作。
契约以 7 文件包为准；schema 版本 / 必填字段 / profile 白名单集中在本模块常量，
正式包契约变更时只改这里。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import zipfile
import zlib
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = "1"
SUPPORTED_JURISDICTIONS = frozenset({"cn", "hk", "sg"})
SUPPORTED_PROFILES = frozenset({"local-cl100k-1200-v1"})

PAYLOAD_NAMES = (
    "catalog-snapshot.csv",
    "chunks.jsonl",
    "provisions.jsonl",
    "sources.json",
    "validation.json",
)
REQUIRED_FILES = frozenset((*PAYLOAD_NAMES, "checksums.json", "manifest.json"))
REQUIRED_CHUNK_FIELDS = (
    "chunk_id",
    "law_id",
    "edition_id",
    "provision_id",
    "jurisdiction",
    "language",
    "sequence",
    "index_header",
    "text",
    "content_sha256",
    "source_id",
    "source_locator",
    "tokenizer_profile",
)

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_ENTRIES = 64
# HK 首批全量语料（128 个语言版本 / 69525 分片）解压后约 162 MiB，
# 压缩后仍受 MAX_ARCHIVE_BYTES=64 MiB 约束；这里只放宽解压后总量上限。
MAX_UNCOMPRESSED = 256 * 1024 * 1024
MAX_RATIO = 200
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_JSON_LINE_BYTES = 1024 * 1024
MAX_JSON_DEPTH = 12

_SYMLINK_MODE = 0o120000
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


class PackageValidationError(Exception):
    """包不合格。code 供上层返回可机读拒绝原因，message 供排障。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclass(frozen=True)
class ValidatedPack:
    manifest: dict[str, Any]
    chunks: list[dict[str, Any]]
    provisions: list[dict[str, Any]]
    sources: list[dict[str, Any]]
    files: dict[str, bytes]
    checksums: dict[str, str]
    package_id: str
    jurisdiction: str


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _reject(code: str, message: str) -> None:
    raise PackageValidationError(code, message)


def _assert_depth(obj: Any, name: str) -> None:
    stack: list[tuple[Any, int]] = [(obj, 1)]
    while stack:
        value, depth = stack.pop()
        if depth > MAX_JSON_DEPTH:
            _reject("json_too_deep", f"{name} 嵌套深度超过 {MAX_JSON_DEPTH}")
        if isinstance(value, dict):
            stack.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            stack.extend((item, depth + 1) for item in value)


def _json_value(raw: bytes, name: str) -> Any:
    if len(raw) > MAX_JSON_BYTES:
        _reject("json_too_large", f"{name} 超过 {MAX_JSON_BYTES} 字节")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _reject("json_invalid", f"{name} 不是合法 UTF-8 JSON: {exc}")
    _assert_depth(value, name)
    return value


def _json_object(raw: bytes, name: str) -> dict[str, Any]:
    value = _json_value(raw, name)
    if not isinstance(value, dict):
        _reject("json_invalid", f"{name} 顶层必须是对象")
    return value


def _json_array(raw: bytes, name: str) -> list[Any]:
    value = _json_value(raw, name)
    if not isinstance(value, list):
        _reject("json_invalid", f"{name} 顶层必须是数组")
    return value


def _read_jsonl(raw: bytes, name: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for lineno, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        if len(line) > MAX_JSON_LINE_BYTES:
            _reject("json_line_too_large", f"{name} 第 {lineno} 行超过 {MAX_JSON_LINE_BYTES} 字节")
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            _reject("json_invalid", f"{name} 第 {lineno} 行不是合法 JSON: {exc}")
        if not isinstance(value, dict):
            _reject("json_invalid", f"{name} 第 {lineno} 行必须是对象")
        _assert_depth(value, f"{name}:{lineno}")
        rows.append(value)
    return rows


def _check_member(info: zipfile.ZipInfo, seen: set[str]) -> None:
    name = info.filename
    if info.flag_bits & 0x1:
        _reject("archive_encrypted", f"加密条目 {name}")
    if not name or name.endswith("/") or "\x00" in name:
        _reject("archive_unsafe_name", f"目录项或空名 {name!r}")
    if name.startswith("/") or "\\" in name or _WINDOWS_DRIVE.match(name) or not name.isascii():
        _reject("archive_unsafe_name", f"路径不安全或非 UTF-8 文件名 {name!r}")
    if any(part in ("", ".", "..") for part in name.split("/")):
        _reject("archive_unsafe_name", f"路径穿越或空路径段 {name!r}")
    if ((info.external_attr >> 16) & 0o170000) == _SYMLINK_MODE:
        _reject("archive_symlink", f"符号链接 {name}")
    if name in seen:
        _reject("archive_duplicate_name", f"重复文件名 {name}")
    seen.add(name)
    if info.compress_size and info.file_size > 1024:
        if info.file_size / info.compress_size > MAX_RATIO:
            _reject("archive_ratio", f"压缩比超限 {name}")


def validate_archive(data: bytes) -> dict[str, bytes]:
    """安全解出整包，返回 文件名 → 字节；不落盘、不解压到文件系统。"""
    if not data:
        _reject("archive_invalid", "空包")
    if len(data) > MAX_ARCHIVE_BYTES:
        _reject("archive_too_large", f"包大小 {len(data)} 超过 {MAX_ARCHIVE_BYTES}")
    if not zipfile.is_zipfile(io.BytesIO(data)):
        _reject("archive_invalid", "不是合法 ZIP")
    files: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_ENTRIES:
                _reject("archive_entry_limit", f"条目数 {len(infos)} 超过 {MAX_ENTRIES}")
            total = sum(info.file_size for info in infos)
            if total > MAX_UNCOMPRESSED:
                _reject("archive_uncompressed_limit", f"解压后 {total} 字节超过 {MAX_UNCOMPRESSED}")
            seen: set[str] = set()
            for info in infos:
                _check_member(info, seen)
                files[info.filename] = zf.read(info)
    except PackageValidationError:
        raise
    except (
        zipfile.BadZipFile,
        RuntimeError,
        NotImplementedError,
        OSError,
        EOFError,
        zlib.error,
    ) as exc:
        _reject("archive_invalid", f"ZIP 读取失败: {type(exc).__name__}: {exc}")
    return files


def _check_identity(
    manifest: dict[str, Any],
    validation: dict[str, Any],
    checksums: dict[str, Any],
    files: dict[str, bytes],
    jurisdiction: str,
) -> tuple[str, dict[str, Any]]:
    if str(manifest.get("schema_version")) != SCHEMA_VERSION:
        _reject("schema_unsupported", f"schema_version={manifest.get('schema_version')}")
    if manifest.get("jurisdiction") != jurisdiction:
        _reject(
            "jurisdiction_mismatch",
            f"包内 {manifest.get('jurisdiction')} 与请求 {jurisdiction} 不符",
        )
    if not manifest.get("scenario_id"):
        _reject("manifest_incomplete", "缺 scenario_id")
    if manifest.get("example_only") or validation.get("example_only"):
        _reject("example_only", "示例包禁止发布")
    if validation.get("status") != "passed" or validation.get("release_eligible") is not True:
        _reject("validation_blocked", f"status={validation.get('status')}")
    if validation.get("source_verification") not in (None, "signed"):
        _reject("validation_blocked", f"source_verification={validation.get('source_verification')}")
    if validation.get("legal_status") not in (None, "verified"):
        _reject("validation_blocked", f"legal_status={validation.get('legal_status')}")
    if not manifest.get("legal_as_of") or not manifest.get("corpus_commit"):
        _reject("manifest_incomplete", "缺版次/截至日/corpus_commit")
    if not manifest.get("source_notices"):
        _reject("manifest_incomplete", "缺来源声明")
    profile = manifest.get("chunk_profile")
    if not isinstance(profile, dict) or profile.get("id") not in SUPPORTED_PROFILES:
        _reject("profile_incompatible", f"chunk_profile={profile}")

    if set(checksums) != set(PAYLOAD_NAMES):
        _reject("checksum_invalid", "checksums.json 覆盖的文件集与契约不符")
    for name in PAYLOAD_NAMES:
        expected = checksums[name]
        if not isinstance(expected, str) or expected != sha256_bytes(files[name]):
            _reject("checksum_mismatch", f"载荷摘要不符 {name}")
    package_id = sha256_text(canonical_json(checksums))
    if manifest.get("package_id") != package_id:
        _reject("package_id_mismatch", "package_id 与摘要清单重算结果不符")
    return package_id, profile


def _manifest_editions(manifest: dict[str, Any]) -> set[str]:
    editions = manifest.get("editions")
    if not isinstance(editions, list) or not editions:
        _reject("manifest_incomplete", "缺 editions")
    ids: set[str] = set()
    for item in editions:
        if not isinstance(item, dict) or not all(
            isinstance(item.get(key), str) and item[key]
            for key in ("edition_id", "law_id", "language", "official_title")
        ):
            _reject("manifest_incomplete", "edition 记录字段不全")
        if item["edition_id"] in ids:
            _reject("manifest_incomplete", f"edition_id 重复 {item['edition_id']}")
        ids.add(item["edition_id"])
    return ids


def _check_chunks(
    chunks: list[dict[str, Any]],
    jurisdiction: str,
    profile: dict[str, Any],
) -> None:
    seen: set[str] = set()
    sequences: dict[tuple[str, str], list[int]] = defaultdict(list)
    for chunk in chunks:
        absent = [key for key in REQUIRED_CHUNK_FIELDS if key not in chunk]
        if absent:
            _reject("chunk_invalid", f"chunk 缺字段 {absent}")
        cid = chunk["chunk_id"]
        if not isinstance(cid, str) or not cid:
            _reject("chunk_invalid", "chunk_id 非法")
        if cid in seen:
            _reject("chunk_duplicate_id", f"chunk_id 重复 {cid}")
        seen.add(cid)
        if chunk["jurisdiction"] != jurisdiction:
            _reject("jurisdiction_mismatch", f"chunk 法域不符 {cid}")
        text = chunk["text"]
        if not isinstance(text, str) or not text:
            _reject("chunk_invalid", f"text 为空 {cid}")
        if chunk["content_sha256"] != sha256_text(text):
            _reject("content_sha256_mismatch", f"content_sha256 错误 {cid}")
        header = chunk["index_header"]
        if not isinstance(header, str) or (header and header in text):
            _reject("index_header_in_text", f"index_header 混入正文或类型错误 {cid}")
        sequence = chunk["sequence"]
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
            _reject("sequence_invalid", f"sequence 非法 {cid}")
        if chunk["tokenizer_profile"] != profile["id"]:
            _reject("profile_incompatible", f"tokenizer_profile 不符 {cid}")
        max_tokens = profile.get("max_tokens")
        token_count = chunk.get("token_count")
        if max_tokens is not None and token_count is not None:
            if not isinstance(token_count, int) or token_count > max_tokens:
                _reject("chunk_too_long", f"分片超限 {cid}")
        if not all(isinstance(chunk[key], str) for key in ("law_id", "edition_id", "provision_id", "source_id")):
            _reject("chunk_invalid", f"身份字段类型错误 {cid}")
        sequences[(chunk["edition_id"], chunk["provision_id"])].append(sequence)
    for key, values in sequences.items():
        if sorted(values) != list(range(1, len(values) + 1)):
            _reject("sequence_invalid", f"sequence 不连续 {key}")


def _check_references(
    chunks: list[dict[str, Any]],
    provisions: list[dict[str, Any]],
    sources: list[Any],
    edition_ids: set[str],
    jurisdiction: str,
) -> None:
    source_ids: set[str] = set()
    for source in sources:
        if not isinstance(source, dict) or not isinstance(source.get("source_id"), str):
            _reject("reference_unresolved", "sources.json 记录缺 source_id")
        source_ids.add(source["source_id"])
    provision_keys: set[tuple[str, str]] = set()
    for provision in provisions:
        if not isinstance(provision.get("edition_id"), str) or not isinstance(
            provision.get("provision_id"), str
        ):
            _reject("reference_unresolved", "provisions.jsonl 记录缺身份字段")
        if "jurisdiction" in provision and provision["jurisdiction"] != jurisdiction:
            _reject("jurisdiction_mismatch", f"provision 法域不符 {provision['provision_id']}")
        provision_keys.add((provision["edition_id"], provision["provision_id"]))
    for chunk in chunks:
        cid = chunk["chunk_id"]
        if chunk["edition_id"] not in edition_ids:
            _reject("reference_unresolved", f"edition 引用不可解析 {cid}")
        if chunk["source_id"] not in source_ids:
            _reject("reference_unresolved", f"source 引用不可解析 {cid}")
        if (chunk["edition_id"], chunk["provision_id"]) not in provision_keys:
            _reject("reference_unresolved", f"provision 引用不可解析 {cid}")


def _check_catalog(raw: bytes, jurisdiction: str, chunks: list[dict[str, Any]]) -> None:
    try:
        text = raw.decode("utf-8")
        reader = csv.DictReader(io.StringIO(text))
    except (UnicodeDecodeError, csv.Error) as exc:
        _reject("catalog_invalid", f"catalog-snapshot.csv 无法解析: {exc}")
    required = {"scenario_id", "law_id", "jurisdiction", "inclusion_decision"}
    if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
        _reject("catalog_invalid", "catalog-snapshot.csv 缺列")
    law_ids = {chunk["law_id"] for chunk in chunks}
    try:
        for row in reader:
            if row.get("jurisdiction") != jurisdiction:
                _reject("jurisdiction_mismatch", "catalog 行法域不符")
            if row.get("inclusion_decision") == "include" and row.get("law_id") not in law_ids:
                _reject("catalog_unresolved", f"include 条目无已核验正文 {row.get('law_id')}")
    except csv.Error as exc:
        _reject("catalog_invalid", f"catalog-snapshot.csv 行解析失败: {exc}")


def validate_pack(files: dict[str, bytes], *, jurisdiction: str) -> ValidatedPack:
    """校验已解出的 7 文件包；任一不合格即抛 PackageValidationError。"""
    if jurisdiction not in SUPPORTED_JURISDICTIONS:
        _reject("jurisdiction_invalid", f"不支持的法域 {jurisdiction}")
    missing = sorted(REQUIRED_FILES - set(files))
    extra = sorted(set(files) - REQUIRED_FILES)
    if missing:
        _reject("missing_files", f"缺文件 {missing}")
    if extra:
        _reject("unexpected_files", f"契约外的文件 {extra}（本期只接受 7 文件包）")

    manifest = _json_object(files["manifest.json"], "manifest.json")
    validation = _json_object(files["validation.json"], "validation.json")
    checksums = _json_object(files["checksums.json"], "checksums.json")
    package_id, profile = _check_identity(manifest, validation, checksums, files, jurisdiction)

    chunks = _read_jsonl(files["chunks.jsonl"], "chunks.jsonl")
    provisions = _read_jsonl(files["provisions.jsonl"], "provisions.jsonl")
    sources = _json_array(files["sources.json"], "sources.json")
    counts = manifest.get("counts")
    if not isinstance(counts, dict):
        _reject("counts_invalid", "manifest.counts 缺失")
    if counts.get("chunks") != len(chunks) or counts.get("provisions") != len(provisions):
        _reject("counts_invalid", "counts 与真实行数不符")
    if validation.get("chunk_count") != len(chunks) or validation.get("provision_count") != len(provisions):
        _reject("counts_invalid", "validation 计数与真实行数不符")
    if not chunks:
        _reject("counts_invalid", "空包：chunks.jsonl 无分片")

    edition_ids = _manifest_editions(manifest)
    _check_chunks(chunks, jurisdiction, profile)
    _check_references(chunks, provisions, sources, edition_ids, jurisdiction)
    _check_catalog(files["catalog-snapshot.csv"], jurisdiction, chunks)

    return ValidatedPack(
        manifest=manifest,
        chunks=chunks,
        provisions=provisions,
        sources=sources,
        files=files,
        checksums=checksums,
        package_id=package_id,
        jurisdiction=jurisdiction,
    )


def validate_package(data: bytes, *, jurisdiction: str) -> ValidatedPack:
    """上传/导入共用入口：ZIP 边界校验 → 7 文件契约校验。"""
    return validate_pack(validate_archive(data), jurisdiction=jurisdiction)
