# -*- coding: utf-8 -*-
"""CN 正式导入包生成器。

复用 `tools/packing/` 的聚合、7 文件建包与 B0 双重校验实现，只覆盖
CN 专属的版次/来源声明常量；不修改 HK/SG 打包器、不重写 HK/SG 已有包、
不联网、不写数据库。

用法（交付仓库根目录或任意目录）：
    python3.11 tools/packing/build_cn_pack.py
"""
from __future__ import annotations

import json
import sys
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

import build_official_packs as base  # noqa: E402
import package_validator as pv  # noqa: E402

STAMP = "20261107"
OUT_DIR = ROOT / "03-import-packs"

base.LEGAL_AS_OF = "2026-11-07"
base.CORPUS_COMMIT = "cn-docs-20261107"
base.SOURCE_NOTICE = (
    "由 2026-11-07 CN docx 离线切片产物聚合，来源与版次见 sources.json"
)


def main() -> int:
    base.SRC, base.SOURCE_LABEL = base.resolve_source()
    data = base.collect("cn")
    files = base.build_files(data, "cn")
    unpacked = sum(len(value) for value in files.values())
    print(
        f"[cn] chunks={data['counts']['chunks']} "
        f"provisions={data['counts']['provisions']} "
        f"laws={data['counts']['laws']} editions={len(data['editions'])} "
        f"unpacked={unpacked / 1048576:.2f} MiB",
        flush=True,
    )

    pack = pv.validate_pack(files, jurisdiction="cn")
    print(f"[cn] validate_pack OK package_id={pack.package_id}", flush=True)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"cn-official-pack-{STAMP}.zip"
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for name in sorted(pv.REQUIRED_FILES):
            zf.writestr(name, files[name])
    raw = out.read_bytes()
    archive_pack = pv.validate_pack(pv.validate_archive(raw), jurisdiction="cn")
    if archive_pack.package_id != pack.package_id:
        print("[FAIL] 归档前后 package_id 不一致", file=sys.stderr)
        return 1
    print(
        f"[cn] zip={len(raw) / 1048576:.2f} MiB "
        "validate_archive+validate_pack OK -> " + str(out),
        flush=True,
    )

    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": f"{base.SOURCE_LABEL}/cn",
        "packs": {
            "cn": {
                "path": base.rel_path(out),
                "bytes": len(raw),
                "unpacked_bytes": unpacked,
                "package_id": pack.package_id,
                "sha256": pv.sha256_bytes(raw),
                "counts": data["counts"],
                "sources": data["source_count"],
                "legal_as_of": base.LEGAL_AS_OF,
                "corpus_commit": base.CORPUS_COMMIT,
            }
        },
    }
    report_path = OUT_DIR / f"official-packs-cn-{STAMP}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告：{report_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
