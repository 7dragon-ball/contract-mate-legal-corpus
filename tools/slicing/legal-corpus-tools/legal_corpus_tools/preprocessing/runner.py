# -*- coding: utf-8 -*-
"""首批离线预处理运行器：逐语言版本解析、分片、原子落盘、进度与续跑。"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from .chunker import build_chunks
from .common import (CHUNK_ID_VERSION, PARSER_VERSION, PROJECT_ROOT, SCHEMA_VERSION,
                     EXTRACTION_BLOCKED, EXTRACTION_PASSED,
                     PROCESSING_COMPLETED, PROCESSING_FAILED, append_issue, append_progress,
                     atomic_write_json, atomic_write_jsonl, atomic_write_text, canonical_json,
                     read_json, read_jsonl, sha256_file, sha256_text, stable_id, stop_requested)
from .hk_xml import XmlExtractor
from .inventory import build_inventory
from .sg_pdf import extract as extract_pdf, raw_page_row
from .tokenizer import build_encoding, load_profile

CHUNK_ELIGIBLE = {'section', 'schedule', 'long_title', 'heading', 'note', 'other',
                  'table', 'supplement'}
NOTICES = {
    'hk': '检索抽取文本不是 HKeL 核证副本；请核对官方原件及现行整合版本。',
    'sg': 'SSO 使用条款见 https://sso.agc.gov.sg/Terms-of-Use；抽取文本不是官方核证文本。',
}


def _safe(s: str) -> str:
    return s.replace(':', '_').replace('/', '_')


def _version_dir(output: Path, law: dict, language: str, edition_id: str) -> Path:
    return output / law['jurisdiction'] / _safe(law['law_id']) / language / _safe(edition_id)


def _archive_stale_versions(output: Path, law: dict, language: str,
                            current_dir: Path) -> list[str]:
    """原件摘要变化会生成新的 edition_id 目录。

    把同一法例／语言下遗留的旧 blocked / 失败版本目录移到 `_archived_versions/`，
    避免汇总层把旧 validation.json 扫成「幽灵阻断版本」。已 passed 的历史版本
    属于真实版本记录，不移动、不删除。
    """
    parent = current_dir.parent
    if not parent.is_dir():
        return []
    archived: list[str] = []
    for old in sorted(parent.iterdir()):
        if not old.is_dir() or old == current_dir or old.name.startswith('.'):
            continue
        vp = old / 'validation.json'
        if not vp.is_file():
            continue
        val = read_json(vp, {})
        if val.get('law_id') != law['law_id'] or val.get('language') != language:
            continue
        if (val.get('extraction_status') == EXTRACTION_PASSED and
                val.get('processing_status') != PROCESSING_FAILED):
            continue
        # 归档放在输出目录之外，正式产物目录只保留有效版本
        dest_root = output.parent / (output.name + '.archived')
        dest_root.mkdir(parents=True, exist_ok=True)
        dest = dest_root / old.name
        i = 1
        while dest.exists():
            dest = dest_root / f'{old.name}__{i}'
            i += 1
        shutil.move(str(old), str(dest))
        archived.append(str(dest))
    return archived


def _cache_key(src: dict, profile: dict, fmt: str) -> str:
    return sha256_text(canonical_json({
        'source_sha256': src['source_sha256'], 'source_format': fmt,
        'parser_version': profile['parser_version'],
        'splitter_version': profile['splitter_version'],
        'profile_sha256': profile['_profile_sha256'],
        'chunk_id_version': CHUNK_ID_VERSION,
        'cleaning_version': 'page-clean-1.0.0',
        'schema_version': SCHEMA_VERSION,
    }))


def _checksums(vdir: Path) -> dict:
    out = {}
    for root, dirs, files in os.walk(vdir):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for f in sorted(files):
            p = Path(root) / f
            if p.name in ('checksums.json',) or p.suffix == '.tmp':
                continue
            out[str(p.relative_to(vdir))] = sha256_file(p)
    return out


def _preview(law: dict, edition: dict, units: list[dict], report: dict, chunks: list[dict]) -> str:
    lines = [f"# {law['code']} · {law['official_title_zh'] or law['official_title_en']}", '',
             f"- 语言：`{edition['language']}`", f"- 版本：`{edition['edition_id']}`",
             f"- 状态：`{edition['extraction_status']}`（技术处理完成不等于法律时效已核验）",
             f"- 结构单元：{len(units)}；切片：{len(chunks)}；发布资格：否", '']
    if report.get('coverage'):
        c = report['coverage']
        lines.append(f"文本覆盖对账：{c['ok']}，片段 {c['total_text_fragments']}，"
                     f"缺失 {len(c['missing'])}，重复 {len(c['duplicated'])}。")
    if report.get('total_pages') is not None:
        lines.append(f"PDF 页数 {report['total_pages']}，空白页 {report['blank_pages']}，"
                     f"目录页 {report.get('toc_pages', [])}，条文 {report.get('section_count', 0)}。")
    lines += ['', '## 结构单元摘录（前 8 条）', '']
    for u in units[:8]:
        lines += [f"### `{u['provision_id']}` · {u['kind']} · {u['heading'][:60]}", '',
                  '```text', u['text'][:700], '```', '']
    return '\n'.join(lines) + '\n'


def _source_record(law: dict, src: dict, edition_id: str, language: str,
                   version: dict, fmt: str, assets: list[dict]) -> dict:
    return {
        'source_id': f"{law['law_id']}:{language}:{src['source_sha256'][:16]}",
        'law_id': law['law_id'], 'jurisdiction': law['jurisdiction'],
        'language': language, 'official_title': (law['official_title_en'] if language == 'en'
                                                else law['official_title_zh']),
        'official_identifier': law['code'], 'official_url': law['official_url'],
        'edition_id': edition_id, 'parent_law_id': None,
        'source_file': src['source_file'], 'source_format': fmt,
        'source_sha256': src['source_sha256'], 'source_url': law['official_url'],
        'source_type': 'official_xml' if fmt == 'xml' else 'official_pdf',
        'source_status': 'downloaded', 'source_batch': src['source_batch'],
        'version_tag': src['version_tag'], 'source_as_of': version.get('source_as_of'),
        'retrieved_at': src.get('retrieved_at_local_mtime'),
        'effective_from': version.get('effective_from'),
        'version_evidence': version.get('version_evidence', []),
        'pdf_created_date': version.get('pdf_created_date'),
        'official_status_raw': version.get('official_status_raw'),
        'verification_status': 'pending', 'legal_status': 'unknown',
        'required_notice': NOTICES[law['jurisdiction']],
        'terms_url': ('https://www.elegislation.gov.hk/importantnotices' if law['jurisdiction'] == 'hk'
                      else 'https://sso.agc.gov.sg/Terms-of-Use'),
        'edition_basis': '原件摘要 + 解析器版本；版本信息不足时以来源摘要为稳定标识',
        'asset_refs': [a.get('resolved_path') for a in assets if a.get('resolved_path')],
        'unselected_copies': [u['source_file'] for u in src.get('_unselected', [])],
    }


def _write_blocked(vdir: Path, law: dict, edition: dict, reasons: list[str],
                   extra: dict | None = None) -> dict:
    vdir.mkdir(parents=True, exist_ok=True)
    atomic_write_jsonl(vdir / 'provisions.jsonl', [])
    atomic_write_jsonl(vdir / 'chunks.jsonl', [])
    rep = {'parser_version': PARSER_VERSION, 'blocking_reasons': reasons,
           'warnings': reasons, 'by_kind': {}}
    if extra:
        rep.update(extra)
    atomic_write_json(vdir / 'extraction-report.json', rep)
    val = {'schema_version': SCHEMA_VERSION, 'law_id': law['law_id'],
           'edition_id': edition['edition_id'], 'language': edition['language'],
           'processing_status': PROCESSING_COMPLETED, 'extraction_status': EXTRACTION_BLOCKED,
           'source_verification': 'pending', 'legal_status': 'unknown',
           'release_eligible': False, 'checks': {'source_hash_matches_inventory': None},
           'blocking_reasons': reasons, 'warnings': reasons,
           'counts': {'provisions': 0, 'chunks': 0, 'max_tokens': 0}}
    atomic_write_json(vdir / 'validation.json', val)
    atomic_write_json(vdir / 'sources.json', [])
    atomic_write_text(vdir / 'preview.md',
                      f"# {law['code']} · {edition['language']}\n\n阻断：" + '；'.join(reasons) + '\n')
    atomic_write_json(vdir / 'checksums.json', _checksums(vdir))
    return val


def _validate_version(vdir: Path, law: dict, edition: dict, profile: dict,
                      enc, blocked: bool, reasons: list[str], report: dict) -> dict:
    provisions = read_jsonl(vdir / 'provisions.jsonl')
    chunks = read_jsonl(vdir / 'chunks.jsonl')
    checks: dict[str, Any] = {}
    warnings: list[str] = list(reasons)
    if report.get('needs_asset_review'):
        warnings.append('存在图像/图表资产：图像内容不由纯文本覆盖，已登记路径与摘要，需人工补充')
    checks['source_hash_matches_inventory'] = None if blocked else True
    pids = [p['provision_id'] for p in provisions]
    checks['provision_ids_unique'] = len(pids) == len(set(pids))
    cids = [c['chunk_id'] for c in chunks]
    checks['chunk_ids_unique'] = len(cids) == len(set(cids))
    checks['chunk_text_sha256_ok'] = all(c['content_sha256'] == sha256_text(c['text']) for c in chunks)
    by_pid: dict[str, list[dict]] = {}
    for c in chunks:
        by_pid.setdefault(c['provision_id'], []).append(c)
    recon = True
    seq_ok = True
    for pid, cs in by_pid.items():
        cs.sort(key=lambda x: x['sequence'])
        if [c['sequence'] for c in cs] != list(range(1, len(cs) + 1)):
            seq_ok = False
        prov = next((p for p in provisions if p['provision_id'] == pid), None)
        if prov is None or ''.join(c['text'] for c in cs) != prov['text']:
            recon = False
    checks['chunk_sequence_continuous'] = seq_ok
    checks['chunk_text_reconstructs_provision'] = recon
    checks['eligible_units_chunked'] = all(p['provision_id'] in by_pid for p in provisions
                                           if p['kind'] in CHUNK_ELIGIBLE and p['text'].strip())
    max_seen = 0
    budget_ok = True
    for c in chunks:
        from .tokenizer import count_tokens
        actual = count_tokens(enc, c['index_header'], c['text'])
        max_seen = max(max_seen, actual)
        if actual != c['token_count'] or actual > int(profile['max_tokens']):
            budget_ok = False
    checks['token_budget_ok'] = budget_ok
    checks['coverage_ok'] = bool(report.get('coverage', {}).get('ok')) if report.get('coverage') else None
    raw_pages = read_jsonl(vdir / 'raw-pages.jsonl') if (vdir / 'raw-pages.jsonl').exists() else []
    if report.get('total_pages') is not None:
        checks['raw_pages_complete'] = len(raw_pages) == report['total_pages']
    atomic_write_json(vdir / 'checksums.json', _checksums(vdir))
    cs = read_json(vdir / 'checksums.json', {})
    checks['checksums_ok'] = all(sha256_file(vdir / rel) == digest
                                 for rel, digest in cs.items() if (vdir / rel).is_file())
    failed = [k for k, v in checks.items() if v is False]
    status = EXTRACTION_BLOCKED if (blocked or failed) else EXTRACTION_PASSED
    if failed:
        warnings.append('校验未通过项：' + ', '.join(failed))
    val = {
        'schema_version': SCHEMA_VERSION, 'law_id': law['law_id'],
        'edition_id': edition['edition_id'], 'language': edition['language'],
        'processing_status': PROCESSING_COMPLETED, 'extraction_status': status,
        'source_verification': 'pending', 'legal_status': 'unknown',
        'release_eligible': False,
        'release_blockers': ['来源时效与现行整合版本未联网核验', '未生成/未验证下游正式导入包契约',
                             '本次任务不含打包与上传'],
        'checks': checks, 'blocking_reasons': warnings if status == EXTRACTION_BLOCKED else [],
        'warnings': warnings,
        'counts': {'provisions': len(provisions), 'chunks': len(chunks),
                   'max_tokens': max_seen, 'token_budget': int(profile['max_tokens'])},
    }
    atomic_write_json(vdir / 'validation.json', val)
    atomic_write_json(vdir / 'checksums.json', _checksums(vdir))
    return val


def process_version(source_root: Path, output: Path, law: dict, ed: dict,
                    profile: dict, enc, resume: bool = False) -> dict:
    src = ed['selected_version']
    if src is None or ed['inventory_status'] != 'ready':
        edition_id = f"{law['law_id']}:{ed['language']}:blocked"
        edition = {'edition_id': edition_id, 'language': ed['language'],
                   'source_id': None, 'extraction_status': EXTRACTION_BLOCKED}
        vdir = _version_dir(output, law, ed['language'], edition_id)
        val = _write_blocked(vdir, law, edition, ed['block_reasons'] or ['选源未通过'])
        for reason in ed['block_reasons'] or ['选源未通过']:
            append_issue(output, {'level': 'version', 'law_id': law['law_id'],
                                  'language': ed['language'], 'code': 'INVENTORY_BLOCKED',
                                  'message': reason})
        return {'law_id': law['law_id'], 'edition_id': edition_id,
                'language': ed['language'], 'status': 'blocked', 'validation': val,
                'version_dir': str(vdir.relative_to(output))}

    edition_id = f"{law['law_id']}:{ed['language']}:src-{src['source_sha256'][:12]}"
    source_id = f"{law['law_id']}:{ed['language']}:{src['source_sha256'][:16]}"
    edition = {'edition_id': edition_id, 'language': ed['language'],
               'source_id': source_id, 'extraction_status': EXTRACTION_PASSED}
    vdir = _version_dir(output, law, ed['language'], edition_id)
    archived = _archive_stale_versions(output, law, ed['language'], vdir)
    cache_key = _cache_key(src, profile, src['format'])
    cache_path = vdir / 'cache-key.json'
    prior = read_json(vdir / 'validation.json', {}) if resume else {}
    if (resume and (vdir / 'checksums.json').exists() and
            read_json(cache_path, {}).get('key') == cache_key and prior and
            prior.get('extraction_status') == EXTRACTION_PASSED and
            not any(v is False for v in (prior.get('checks') or {}).values())):
        return {'law_id': law['law_id'], 'edition_id': edition_id,
                'language': ed['language'], 'status': 'skipped_resume',
                'version_dir': str(vdir.relative_to(output)),
                'archived_stale': archived}

    # 上一轮降级草稿不得残留在本轮正式版本目录中
    for stale_name in ('draft-page-blocks.jsonl', 'draft-chunks.jsonl'):
        stale_path = vdir / stale_name
        if stale_path.is_file():
            stale_path.unlink()

    path = (source_root / src['source_file']).resolve()
    src['_unselected'] = ed['unselected_copies']
    reasons: list[str] = []
    try:
        if not path.is_file():
            raise FileNotFoundError(f'原件不存在：{src["source_file"]}')
        if sha256_file(path) != src['source_sha256']:
            raise RuntimeError('运行期原件摘要与 inventory 不符，阻断该版本')
        if law['jurisdiction'] == 'hk':
            fmt = 'xml'
            result = XmlExtractor(path, path.parent).extract()
            units = result['units']
            report = {
                'parser_version': profile['parser_version'], 'format': 'xml',
                'meta': result['meta'], 'coverage': result['coverage'],
                'assets': result['assets'], 'tables': result['tables'],
                'notes': result['notes'], 'warnings': result['warnings'],
                'by_kind': result['by_kind'], 'total_text_fragments': result['coverage']['total_text_fragments'],
                'unrenderable_content': result['assets'],
                'needs_asset_review': bool(result['assets']),
                'text_coverage_scope': '覆盖对账仅针对 main 的文本节点；图像/图形内容不在纯文本覆盖内',
                'empty_text_units': sum(1 for u in result['units'] if not u['text'].strip()),
            }
            version = {'source_as_of': result['meta'].get('date'),
                       'official_status_raw': result['meta'].get('docstatus'),
                       'version_evidence': [f"HKeL XML dc:date={result['meta'].get('date')}",
                                            f"docStatus={result['meta'].get('docstatus')}"]}
            report['version_fields'] = version
            if not result['coverage']['ok']:
                reasons.append('XML 文本片段覆盖对账未通过（缺失或重复）')
            if any(a.get('status') != 'asset_available' for a in result['assets']):
                reasons.append('存在无法解析的图像资产引用')
        else:
            fmt = 'pdf'
            result = extract_pdf(path)
            units = result['units']
            report = result['report']
            version = result['versions']
            atomic_write_jsonl(vdir / 'raw-pages.jsonl',
                               [raw_page_row(p) for p in result['raw_pages']])
            if result['blocked']:
                reasons.extend(result['warnings'])
                blocks = result.get('draft_blocks', [])
                atomic_write_jsonl(vdir / 'draft-page-blocks.jsonl', blocks)
                drafts = []
                for b in blocks:
                    drafts.append({
                        'chunk_id': stable_id('draft', {'edition_id': edition_id,
                                                        'unit_id': b['unit_id'],
                                                        'text': b['text']}),
                        'law_id': law['law_id'], 'edition_id': edition_id,
                        'provision_id': b['unit_id'], 'jurisdiction': law['jurisdiction'],
                        'language': ed['language'], 'sequence': 1,
                        'index_header': '', 'text': b['text'],
                        'content_sha256': sha256_text(b['text']),
                        'source_id': source_id, 'source_locator': b['source_locator'],
                        'token_count': None, 'tokenizer_profile': None,
                        'segmentation_level': 'page_block',
                    })
                atomic_write_jsonl(vdir / 'draft-chunks.jsonl', drafts)
                units = []   # 草稿绝不混入正式 provisions
        if reasons and not units:
            edition['extraction_status'] = EXTRACTION_BLOCKED
    except Exception as exc:
        reasons.append(f'解析失败：{type(exc).__name__}: {exc}')
        val = _write_blocked(vdir, law, edition, reasons)
        append_issue(output, {'level': 'version', 'law_id': law['law_id'],
                              'language': ed['language'], 'code': 'EXTRACTION_FAILED',
                              'message': reasons[-1]})
        return {'law_id': law['law_id'], 'edition_id': edition_id,
                'language': ed['language'], 'status': 'failed', 'validation': val,
                'version_dir': str(vdir.relative_to(output))}

    provisions = []
    for u in units:
        p = dict(u)
        p.update({'law_id': law['law_id'], 'edition_id': edition_id,
                  'jurisdiction': law['jurisdiction'], 'language': ed['language'],
                  'source_id': source_id})
        provisions.append(p)
    chunks = []
    for p in provisions:
        if p['kind'] not in CHUNK_ELIGIBLE:
            continue                       # 目录/封面不进入入库切片
        chunks.extend(build_chunks(law, edition, p, enc, profile))

    source = _source_record(law, src, edition_id, ed['language'], version, fmt,
                            result.get('assets', []))
    atomic_write_json(vdir / 'sources.json', [source])
    atomic_write_jsonl(vdir / 'provisions.jsonl', provisions)
    atomic_write_jsonl(vdir / 'chunks.jsonl', chunks)
    atomic_write_json(vdir / 'extraction-report.json', report)
    atomic_write_text(vdir / 'preview.md', _preview(law, edition, provisions, report, chunks))
    atomic_write_json(cache_path, {'key': cache_key})
    blocked = bool(reasons) and not provisions
    val = _validate_version(vdir, law, edition, profile, enc, blocked, reasons, report)
    for r in reasons:
        append_issue(output, {'level': 'version', 'law_id': law['law_id'],
                              'language': ed['language'], 'code': 'EXTRACTION_WARNING',
                              'message': r})
    return {'law_id': law['law_id'], 'edition_id': edition_id,
            'language': ed['language'], 'status': 'completed',
            'extraction_status': val['extraction_status'],
            'provisions': len(provisions), 'chunks': len(chunks),
            'validation': val, 'version_dir': str(vdir.relative_to(output)),
            'archived_stale': archived}


def run(source_root: Path, output: Path, profile_path: Path | None, resume: bool,
        jurisdiction: str | None, law_ids: list[str] | None, limit: int | None,
        command: str) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    profile = load_profile(profile_path)
    enc = build_encoding(profile)
    inv = read_json(output / 'inventory.json')
    if inv is None or not resume:
        inv = build_inventory(source_root, output)
    laws = inv['laws']
    if jurisdiction:
        laws = [l for l in laws if l['jurisdiction'] == jurisdiction]
    if law_ids:
        wanted = {x if ':' in x else f'{jurisdiction or "hk"}:{x}' for x in law_ids}
        laws = [l for l in laws if l['law_id'] in wanted or l['code'] in law_ids]
    if limit:
        laws = laws[:limit]
    atomic_write_json(output / 'run-config.json', {
        'schema_version': SCHEMA_VERSION, 'command': command,
        'source_root': str(source_root), 'output': str(output),
        'profile': {k: v for k, v in profile.items() if k in
                    ('profile_id', 'splitter_version', 'parser_version', 'max_tokens',
                     'overlap_tokens', 'counting_text_rule', 'tokenizer')},
        'profile_sha256': profile['_profile_sha256'],
        'selection': {'jurisdiction': jurisdiction, 'law_ids': law_ids or [], 'limit': limit,
                      'selected_laws': len(laws),
                      'total_versions': sum(len(l['editions']) for l in laws)},
        'network_calls': 0, 'model_calls': 0,
        'resume': resume, 'pid': os.getpid(),
        'started_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'resume_command': f'.venv/bin/python -m legal_corpus_tools.preprocess run '
                          f'--source-root "{source_root}" --output "{output}" '
                          f'--profile "{profile["_profile_path"]}" --resume',
        'stop_command': '在输出目录创建 .stop 文件；当前法例安全落盘后退出',
    })
    results = []
    for law in laws:
        if stop_requested(output):
            append_issue(output, {'level': 'run', 'code': 'GRACEFUL_STOP',
                                  'message': '检测到 .stop，当前法例完成后停止'})
            break
        for ed in law['editions']:
            rec = process_version(source_root, output, law, ed, profile, enc, resume=resume)
            rec['law_code'] = law['code']
            append_progress(output, rec)
            results.append(rec)
    return {'profile': profile, 'inventory': inv, 'results': results}
