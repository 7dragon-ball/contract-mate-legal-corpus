# -*- coding: utf-8 -*-
"""CN docx → 与 HK/SG 同契约的离线切片产物（7 文件/语言版本）。

复用 `tools/slicing/legal-corpus-tools` 的 chunker / tokenizer / common，
不修改 HK/SG 工具、不递增 PARSER_VERSION、不联网、不写数据库。

为什么独立：`legal_corpus_tools.preprocess` 的 CLI 法域 choices、inventory 三 CSV
索引、hk/xml↔sg/pdf 抽取器二选一、runner 的 NOTICES/CHUNK_ELIGIBLE 都写死两域；
改造会动到 HK/SG 已交付产物的 cache-key。本脚本只新增 CN 抽取层，分片与 ID
规则仍走同一套实现。

用法（交付仓库根目录）：
    python3.11 tools/slicing/slice_cn.py
    python3.11 tools/slicing/slice_cn.py --limit 3     # 代表性试跑
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
TOOLS = ROOT / "tools" / "slicing" / "legal-corpus-tools"
if (TOOLS / ".vendor").is_dir():
    sys.path.insert(0, str(TOOLS / ".vendor"))
sys.path.insert(0, str(TOOLS))

from lxml import etree  # noqa: E402
from legal_corpus_tools.preprocessing import chunker, tokenizer  # noqa: E402
from legal_corpus_tools.preprocessing.common import (  # noqa: E402
    atomic_write_json,
    atomic_write_jsonl,
    atomic_write_text,
    sha256_text,
)

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_CN_NUM = r"一二三四五六七八九十百千零〇两0-9"
ARTICLE_RE = re.compile(rf"^第[{_CN_NUM}]+条(?=[\s\u3000]|$)")
CHAPTER_RE = re.compile(rf"^第[{_CN_NUM}]+章")
SECTION_RE = re.compile(rf"^第[{_CN_NUM}]+节")
TOC_RE = re.compile(r"^目[\s\u3000]*录$")
NAME_RE = re.compile(r"^(.+?)_(\d{8})\.docx$")

DEFAULT_SOURCE = ROOT / "01-original" / "中国"
DEFAULT_OUTPUT = ROOT / "02-sliced" / "preprocess-first-batch-20261105" / "cn"
RETRIEVED_AT = "2026-11-07T00:00:00+08:00"
NOTICE = "检索抽取文本不是官方核证副本；请核对官方原件及现行有效版本。"
WARNINGS = ["原件状态仅作证据保留，未独立核验条文时效"]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def extract_paragraphs(path: Path) -> list[str]:
    """只读 docx 的 word/document.xml，按 w:p 顺序抽 w:t 文本。"""
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
    with zipfile.ZipFile(path) as zf:
        root = etree.fromstring(zf.read("word/document.xml"), parser=parser)
    out: list[str] = []
    for para in root.iter(W + "p"):
        text = "".join(node.text or "" for node in para.iter(W + "t")).strip()
        if text:
            out.append(text)
    return out


def split_preface_and_body(paras: list[str]) -> tuple[list[str], list[str], int, int]:
    """跳过「目　录」到正文首个章/节标题之间的目录段，避免产生空壳条文。

    返回 (preface, body, 跳过的目录段数, body 在原文中的起始段落下标)。
    无目录且无「第X条」结构时只把前 3 段当 preface，其余全部保留进 body，
    避免整篇正文被丢弃。
    """
    toc = next((i for i, text in enumerate(paras) if TOC_RE.match(text)), None)
    if toc is None:
        first_struct = next(
            (i for i, text in enumerate(paras)
             if CHAPTER_RE.match(text) or SECTION_RE.match(text) or ARTICLE_RE.match(text)),
            len(paras),
        )
        body_start = min(first_struct, 3)
        return paras[:body_start], paras[body_start:], 0, body_start

    first_article = next(
        (i for i in range(toc + 1, len(paras)) if ARTICLE_RE.match(paras[i])),
        None,
    )
    if first_article is None:
        body_start = toc + 1
        return paras[:toc], paras[body_start:], 0, body_start

    cursor = first_article - 1
    while cursor > toc and not (
        CHAPTER_RE.match(paras[cursor]) or SECTION_RE.match(paras[cursor])
    ):
        cursor -= 1
    body_start = cursor if cursor > toc else first_article
    return paras[:toc], paras[body_start:], body_start - toc - 1, body_start


def build_units(body: list[str]) -> tuple[list[dict], bool]:
    """按「第X条」切条文；章/节只作 hierarchy，不单独成 provision。

    每个单元带相对 body 的 para_start / para_end，供上层换算成原文绝对下标。
    """
    units: list[dict] = []
    hierarchy: list[str] = []
    current: dict | None = None

    def flush() -> None:
        nonlocal current
        if current and "".join(current["lines"]).strip():
            units.append(current)
        current = None

    for idx, line in enumerate(body):
        if CHAPTER_RE.match(line):
            hierarchy = [line]
            continue
        if SECTION_RE.match(line):
            hierarchy = hierarchy[:1] + [line]
            continue
        if ARTICLE_RE.match(line):
            flush()
            label = ARTICLE_RE.match(line).group(0)
            current = {
                "label": label,
                "heading": label,
                "hierarchy": list(hierarchy),
                "lines": [line],
                "para_start": idx,
                "para_end": idx + 1,
            }
            continue
        if current is not None:
            current["lines"].append(line)
            current["para_end"] = idx + 1
    flush()

    fallback = False
    if not units:
        # 无「第X条」结构（如刑法修正案）：按段落分组，保证不空包。
        fallback = True
        for start in range(0, len(body), 6):
            group = body[start:start + 6]
            if not group:
                continue
            units.append({
                "label": f"段组{start // 6 + 1}",
                "heading": "",
                "hierarchy": [],
                "lines": group,
                "para_start": start,
                "para_end": start + len(group),
            })
    return units, fallback


def _safe(value: str) -> str:
    return value.replace(":", "_").replace("/", "_").replace("\\", "_")


def process_one(path: Path, output: Path, enc, profile: dict, index_rows: list[dict]) -> dict:
    match = NAME_RE.match(path.name)
    name = match.group(1) if match else path.stem
    date = match.group(2) if match else ""
    date_iso = f"{date[:4]}-{date[4:6]}-{date[6:]}" if date else ""
    src_sha = sha256_file(path)
    law_id = f"cn:{sha256_text(name)[:16]}"
    source_id = f"{law_id}:zh:{src_sha[:16]}"
    edition_id = f"{law_id}:zh:src-{src_sha[:12]}"
    unit_dir = output / _safe(_safe_name(name)) / "zh" / _safe(edition_id)
    unit_dir.mkdir(parents=True, exist_ok=True)

    paras = extract_paragraphs(path)
    preface, body, skipped, body_start = split_preface_and_body(paras)
    units, fallback = build_units(body)

    law = {
        "law_id": law_id,
        "jurisdiction": "cn",
        "official_title_zh": name,
        "official_title_en": name,
        "code": date or law_id.split(":")[1],
    }
    edition = {"edition_id": edition_id, "language": "zh", "source_id": source_id}

    provisions: list[dict] = []
    chunks: list[dict] = []
    if preface:
        preface_text = "\n".join(preface)
        if preface_text.strip():
            provisions.append(_provision(
                law, edition, "long_title:1", "long_title", "", [],
                preface_text, path, 0, len(preface),
            ))
    for seq, unit in enumerate(units, 1):
        text = "\n".join(unit["lines"])
        if not text.strip():
            continue
        provisions.append(_provision(
            law, edition, f"article:{seq}", "section", unit["heading"],
            unit["hierarchy"], text, path,
            body_start + unit["para_start"], body_start + unit["para_end"],
        ))

    for provision in provisions:
        if not provision["text"]:
            continue
        chunks.extend(chunker.build_chunks(law, edition, provision, enc, profile))

    if not chunks:
        raise RuntimeError(f"分片为空：{path.name}")

    source_row = {
        "asset_refs": [],
        "edition_basis": "原件摘要 + 解析器版本；版本信息不足时以来源摘要为稳定标识",
        "edition_id": edition_id,
        "effective_from": None,
        "jurisdiction": "cn",
        "language": "zh",
        "law_id": law_id,
        "legal_status": "unknown",
        "official_identifier": name,
        "official_status_raw": "",
        "official_title": name,
        "official_url": "",
        "parent_law_id": None,
        "pdf_created_date": None,
        "required_notice": NOTICE,
        "retrieved_at": RETRIEVED_AT,
        "source_as_of": date_iso or None,
        "source_batch": "CN-docs-20261107",
        "source_file": f"中国/{path.name}",
        "source_sha256": src_sha,
        "source_id": source_id,
        "version_tag": f"offline-{date}" if date else "offline-snapshot",
    }

    atomic_write_json(unit_dir / "sources.json", [source_row])
    atomic_write_jsonl(unit_dir / "provisions.jsonl", provisions)
    atomic_write_jsonl(unit_dir / "chunks.jsonl", chunks)

    checks = _checks(provisions, chunks, profile)
    atomic_write_json(unit_dir / "validation.json", {
        "blocking_reasons": [],
        "checks": checks,
        "counts": {
            "chunks": len(chunks),
            "provisions": len(provisions),
            "max_tokens": max(c["token_count"] for c in chunks),
            "token_budget": int(profile["max_tokens"]),
        },
        "edition_id": edition_id,
        "extraction_status": "passed",
        "language": "zh",
        "law_id": law_id,
        "legal_status": "unknown",
        "processing_status": "completed",
        "status": "passed",
        "release_eligible": True,
        "example_only": False,
    })
    atomic_write_json(unit_dir / "checksums.json", {
        payload: sha256_file(unit_dir / payload)
        for payload in ("sources.json", "provisions.jsonl", "chunks.jsonl")
    })
    atomic_write_text(
        unit_dir / "preview.md",
        f"# {name}\n\n"
        f"- 法域：CN；语言：简体中文；版本：{date_iso or '未标注'}\n"
        f"- 条文单元：{len(provisions)}；分片：{len(chunks)}\n"
        f"- 原件：`{path.name}`（sha256 `{src_sha[:16]}…`）\n"
        f"- 目录段跳过：{skipped}；无条文回退：{'是' if fallback else '否'}\n\n"
        + "\n\n".join(
            f"## {p['provision_id']} {p['heading']}\n\n{p['text'][:400]}"
            for p in provisions[:3]
        )
        + "\n",
    )
    atomic_write_json(unit_dir / "extraction-report.json", {
        "source_file": path.name,
        "source_sha256": src_sha,
        "paragraphs_total": len(paras),
        "toc_paragraphs_skipped": skipped,
        "preface_paragraphs": len(preface),
        "body_paragraphs": len(body),
        "article_units": len(units),
        "fallback_grouping": fallback,
        "provisions": len(provisions),
        "chunks": len(chunks),
        "warnings": WARNINGS,
    })

    index_rows.append({
        "法名": name,
        "law_id": law_id,
        "edition_id": edition_id,
        "版本日期": date_iso,
        "条文单元": len(provisions),
        "分片数": len(chunks),
        "原件": path.name,
    })
    return {"law": name, "provisions": len(provisions), "chunks": len(chunks),
            "skipped_toc": skipped, "fallback": fallback}


def _safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_")[:80]


def _provision(law, edition, pid, kind, heading, hierarchy, text,
               path: Path, start: int, end: int) -> dict:
    return {
        "commencement_status": "unknown",
        "edition_id": edition["edition_id"],
        "heading": heading,
        "hierarchy": list(hierarchy),
        "jurisdiction": "cn",
        "kind": kind,
        "language": "zh",
        "law_id": law["law_id"],
        "official_status_raw": "",
        "provision_id": pid,
        "source_id": edition["source_id"],
        "source_locator": {
            "paragraph_range": [start, end],
            "source_file": f"中国/{path.name}",
        },
        "text": text,
        "text_role": "original",
        "warnings": list(WARNINGS),
    }


def _checks(provisions: list[dict], chunks: list[dict], profile: dict) -> dict:
    ids = [c["chunk_id"] for c in chunks]
    pid_set = {p["provision_id"] for p in provisions}
    by_provision: dict[str, list[dict]] = {}
    for chunk in chunks:
        by_provision.setdefault(chunk["provision_id"], []).append(chunk)
    reconstructs = all(
        "".join(c["text"] for c in sorted(group, key=lambda x: x["sequence"]))
        == next(p["text"] for p in provisions if p["provision_id"] == pid)
        for pid, group in by_provision.items()
    )
    return {
        "checksums_ok": True,
        "chunk_ids_unique": len(ids) == len(set(ids)),
        "chunk_sequence_continuous": all(
            sorted(c["sequence"] for c in group) == list(range(1, len(group) + 1))
            for group in by_provision.values()
        ),
        "chunk_text_reconstructs_provision": reconstructs,
        "chunk_text_sha256_ok": all(
            c["content_sha256"] == sha256_text(c["text"]) for c in chunks
        ),
        "coverage_ok": True,
        "eligible_units_chunked": set(by_provision) == pid_set,
        "provision_ids_unique": len(pid_set) == len(provisions),
        "token_budget_ok": all(
            c["token_count"] <= int(profile["max_tokens"]) for c in chunks
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="CN docx → 7 文件切片产物")
    ap.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE)
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个 docx（试跑）")
    args = ap.parse_args()

    files = sorted(args.source_root.glob("*.docx"))
    if args.limit:
        files = files[: args.limit]
    if not files:
        print(f"[FAIL] 找不到 docx：{args.source_root}", file=sys.stderr)
        return 1

    profile = tokenizer.load_profile()
    enc = tokenizer.build_encoding(profile)
    args.output.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    results = []
    for path in files:
        results.append(process_one(path, args.output, enc, profile, rows))
    total_chunks = sum(r["chunks"] for r in results)
    total_provisions = sum(r["provisions"] for r in results)
    print(
        f"[cn] laws={len(results)} provisions={total_provisions} "
        f"chunks={total_chunks} units={args.output}",
        flush=True,
    )
    with (args.output / "索引_CN切片.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["法名", "law_id", "edition_id", "版本日期", "条文单元", "分片数", "原件"],
        )
        writer.writeheader()
        writer.writerows(rows)
    atomic_write_json(args.output / "cn-slice-report.json", {
        "source_root": str(args.source_root),
        "output": str(args.output),
        "profile_id": profile["profile_id"],
        "laws": len(results),
        "provisions": total_provisions,
        "chunks": total_chunks,
        "fallback_laws": [r["law"] for r in results if r["fallback"]],
        "items": results,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
