"""把 02-sliced 的全量切片产物聚合成 03-import-packs 下的 B0 正式导入包。

自包含版本：校验器 `package_validator.py` 就在同目录（含 MAX_UNCOMPRESSED=256 MiB）。
只用 Python 3.11 标准库，不依赖数据库或网络。

输出 7 文件标准包：manifest.json / catalog-snapshot.csv / chunks.jsonl /
provisions.jsonl / sources.json / validation.json / checksums.json；
每包都经过 `validate_archive` + `validate_pack` 双重校验后才落盘。

用法（在本仓库根目录或任意目录均可）：
    python3.11 tools/packing/build_official_packs.py

切片来源：优先用已展开的 `02-sliced/preprocess-first-batch-20261105/`；该目录不存在时，
直接解压 `02-sliced/*.zip` 到临时目录再聚合（退出自动清理），无需先手工 unzip。
"""
from __future__ import annotations

import atexit
import csv
import io
import json
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

import package_validator as pv  # noqa: E402

SLICED_DIR = ROOT / "02-sliced"
SRC = SLICED_DIR / "preprocess-first-batch-20261105"
OUT_DIR = ROOT / "03-import-packs"
SOURCE_LABEL = "02-sliced/preprocess-first-batch-20261105"


def rel_path(p: Path) -> str:
    """报告里一律写相对仓库根的路径，不写本机绝对路径。"""
    try:
        return p.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return p.name


def resolve_source() -> tuple[Path, str]:
    """返回 (切片根目录, 报告里记录的来源描述)。压缩包模式解到临时目录。"""
    if SRC.is_dir():
        return SRC, SOURCE_LABEL
    archives = sorted(SLICED_DIR.glob("*.zip"))
    if not archives:
        raise SystemExit(f"找不到切片产物：{SRC} 或 {SLICED_DIR}/*.zip")
    tmp = Path(tempfile.mkdtemp(prefix="lc-sliced-"))
    atexit.register(shutil.rmtree, tmp, True)
    for archive in archives:
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(tmp)
    print(f"切片压缩包已解到临时目录：{tmp}（{len(archives)} 个包）", flush=True)
    return tmp, "02-sliced/*.zip"
STAMP = "20261105"

PROFILE = {"id": "local-cl100k-1200-v1", "max_tokens": 1200, "tokenizer": "cl100k_base"}
SCENARIO_ID = "trade_contract"
LEGAL_AS_OF = "2026-11-05"
CORPUS_COMMIT = "preprocess-first-batch-20261105"
SOURCE_NOTICE = "由 2026-11-05 首批法律文本离线预处理产物聚合，来源与版次见 sources.json"
CATALOG_COLUMNS = (
    "scenario_id",
    "law_id",
    "jurisdiction",
    "official_title",
    "priority",
    "classification_status",
    "inclusion_decision",
)


def collect(jurisdiction: str) -> dict:
    root = SRC / jurisdiction
    chunks = bytearray()
    provisions = bytearray()
    source_rows: list[dict] = []
    law_titles: dict[str, str] = {}
    law_ids: set[str] = set()
    editions: dict[str, dict] = {}
    seen_source_ids: set[str] = set()
    chunk_count = 0
    provision_count = 0

    for src_path in sorted(root.rglob("sources.json")):
        unit = src_path.parent
        chunk_path = unit / "chunks.jsonl"
        provision_path = unit / "provisions.jsonl"
        if not chunk_path.exists() or not provision_path.exists():
            continue

        for row in json.loads(src_path.read_text(encoding="utf-8")):
            sid = str(row.get("source_id") or "").strip()
            eid = str(row.get("edition_id") or "").strip()
            if not sid or not eid or sid in seen_source_ids:
                continue
            seen_source_ids.add(sid)
            source_rows.append(row)
            law_id = str(row.get("law_id") or "")
            title = str(row.get("official_title") or law_id)
            if law_id:
                law_titles.setdefault(law_id, title)
            edition = editions.setdefault(
                eid,
                {
                    "edition_id": eid,
                    "law_id": law_id,
                    "language": str(row.get("language") or ""),
                    "official_title": title,
                    "source_ids": [],
                    "version_tag": str(row.get("version_tag") or "offline-snapshot"),
                },
            )
            edition["source_ids"].append(sid)

        for line in chunk_path.read_bytes().splitlines():
            if not line.strip():
                continue
            chunks.extend(line)
            chunks.extend(b"\n")
            chunk_count += 1
            law_ids.add(str(json.loads(line).get("law_id")))
        for line in provision_path.read_bytes().splitlines():
            if not line.strip():
                continue
            provisions.extend(line)
            provisions.extend(b"\n")
            provision_count += 1

    catalog_buf = io.StringIO(newline="")
    writer = csv.DictWriter(catalog_buf, fieldnames=list(CATALOG_COLUMNS))
    writer.writeheader()
    for law_id in sorted(law_ids):
        writer.writerow(
            {
                "scenario_id": SCENARIO_ID,
                "law_id": law_id,
                "jurisdiction": jurisdiction,
                "official_title": law_titles.get(law_id, law_id),
                "priority": "",
                "classification_status": "preprocessed",
                "inclusion_decision": "include",
            }
        )

    return {
        "chunks": bytes(chunks),
        "provisions": bytes(provisions),
        "sources": json.dumps(source_rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        "catalog": catalog_buf.getvalue().encode("utf-8"),
        "validation": json.dumps(
            {
                "status": "passed",
                "release_eligible": True,
                "example_only": False,
                "chunk_count": chunk_count,
                "provision_count": provision_count,
                "blocking_reasons": [],
                "source_notices": [SOURCE_NOTICE],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8"),
        "counts": {
            "chunks": chunk_count,
            "provisions": provision_count,
            "laws": len(law_ids),
            "editions": len(editions),
        },
        "editions": [editions[key] for key in sorted(editions)],
        "source_count": len(source_rows),
    }


def build_files(data: dict, jurisdiction: str) -> dict[str, bytes]:
    files = {
        "catalog-snapshot.csv": data["catalog"],
        "chunks.jsonl": data["chunks"],
        "provisions.jsonl": data["provisions"],
        "sources.json": data["sources"],
        "validation.json": data["validation"],
    }
    checksums = {name: pv.sha256_bytes(files[name]) for name in pv.PAYLOAD_NAMES}
    files["checksums.json"] = json.dumps(
        checksums, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    package_id = pv.sha256_text(pv.canonical_json(checksums))
    files["manifest.json"] = json.dumps(
        {
            "schema_version": pv.SCHEMA_VERSION,
            "jurisdiction": jurisdiction,
            "scenario_id": SCENARIO_ID,
            "example_only": False,
            "legal_as_of": LEGAL_AS_OF,
            "corpus_commit": CORPUS_COMMIT,
            "source_notices": [SOURCE_NOTICE],
            "chunk_profile": PROFILE,
            "editions": data["editions"],
            "counts": data["counts"],
            "package_id": package_id,
            "release_eligible": True,
            "tool_version": "legal-corpus-tools/preprocess-first-batch-20261105",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return files


def main() -> int:
    global SRC, SOURCE_LABEL
    SRC, SOURCE_LABEL = resolve_source()
    if not (SRC / "hk").is_dir() or not (SRC / "sg").is_dir():
        print(f"[FAIL] 找不到切片产物目录 {SRC}", file=sys.stderr)
        return 1
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report: dict = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": SOURCE_LABEL,
        "packs": {},
    }
    for jurisdiction in ("hk", "sg"):
        data = collect(jurisdiction)
        files = build_files(data, jurisdiction)
        unpacked = sum(len(v) for v in files.values())
        print(
            f"[{jurisdiction}] chunks={data['counts']['chunks']} "
            f"provisions={data['counts']['provisions']} editions={len(data['editions'])} "
            f"unpacked={unpacked / 1048576:.1f} MiB",
            flush=True,
        )
        pack = pv.validate_pack(files, jurisdiction=jurisdiction)
        print(f"[{jurisdiction}] validate_pack OK package_id={pack.package_id}", flush=True)

        out = OUT_DIR / f"{jurisdiction}-official-pack-{STAMP}.zip"
        if out.exists():
            out.unlink()
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for name in sorted(pv.REQUIRED_FILES):
                zf.writestr(name, files[name])
        raw = out.read_bytes()
        pv.validate_pack(pv.validate_archive(raw), jurisdiction=jurisdiction)
        print(
            f"[{jurisdiction}] zip={len(raw) / 1048576:.1f} MiB "
            "validate_archive+validate_pack OK -> " + str(out),
            flush=True,
        )
        report["packs"][jurisdiction] = {
            "path": rel_path(out),
            "bytes": len(raw),
            "unpacked_bytes": unpacked,
            "package_id": pack.package_id,
            "sha256": pv.sha256_bytes(raw),
            "counts": data["counts"],
            "sources": data["source_count"],
        }
    report_path = OUT_DIR / f"official-packs-{STAMP}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告：{report_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
