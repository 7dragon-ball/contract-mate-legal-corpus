# -*- coding: utf-8 -*-
"""首批离线预处理：原件核验、范围对账与按语言版本选源。"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

from .common import (PROJECT_ROOT, SCHEMA_VERSION, append_issue, atomic_write_json,
                     language_code, normalize_law_id, read_csv, safe_relpath,
                     sha256_file)

CONCRETE_TYPES = ('主法', '附属法例', '商品行业扩展')
IMAGE_FORMATS = {'JPG', 'JPEG', 'PNG', 'GIF', 'SVG', 'BMP', 'WEBP'}
REFERENCE_REASONS = {
    'official_html': '官方 HTML 对照件；与选中官方 XML/PDF 内容不重复抽取',
    'early_html': '早期核心集阅读版 HTML；仅作辅助对照，不升级为官方原件',
    'web_snapshot': '本地网页文本快照；正文容器未独立核验，不作为官方原件',
    'asset': '来源图表资源，按资产关联，不充当正文或语言版本',
}


def _mtime_iso(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec='seconds')
    except OSError:
        return None


def _role_of(row: dict) -> str:
    fmt = (row.get('格式') or '').upper()
    batch = row.get('来源批次') or ''
    if fmt in IMAGE_FORMATS:
        return 'asset'
    if batch.startswith('SSO补充下载'):
        return 'official_pdf_current'
    if fmt == 'PDF':
        return 'official_pdf'
    if fmt == 'XML':
        return 'official_xml'
    if batch == '贸易合同法律文本':
        return 'early_html'
    if batch == '两地现行法律全集':
        return 'official_html' if fmt == 'HTML' else 'web_snapshot'
    return 'unknown'


def _source_record(row: dict, root: Path, verified: dict) -> dict:
    rel = row['目标相对路径']
    return {
        'source_file': rel,
        'source_sha256': row['sha256'],
        'bytes': int(row['字节数']),
        'format': (row.get('格式') or '').lower(),
        'language': language_code(row.get('语言', ''), rel),
        'source_batch': row.get('来源批次', ''),
        'role': _role_of(row),
        'note': row.get('备注', ''),
        'duplicate_sources': [x for x in (row.get('重复来源') or '').split(';') if x],
        'hash_verified': verified.get(rel, {}).get('ok', False),
        'hash_check': verified.get(rel, {}).get('detail', ''),
        'retrieved_at_local_mtime': _mtime_iso(root / rel),
        'version_tag': row['sha256'][:12],       # 版本信息不足时以原件摘要作稳定标识
        'source_as_of': None,
        'effective_from': None,
        'legal_status': 'unknown',
        'official_status_raw': None,
        'verification_status': 'pending',
    }


def build_inventory(source_root: Path, output: Path) -> dict:
    """核验全部索引文件，输出 inventory.json；不修改输入原件。"""
    root = source_root.resolve()
    entry_rows = read_csv(root / '索引_清单条目.csv')
    file_rows = read_csv(root / '索引_已复制文件.csv')
    out_rows = read_csv(root / '索引_未复制条目.csv') if (root / '索引_未复制条目.csv').exists() else []

    concrete = [r for r in entry_rows if r['条目类型'] in CONCRETE_TYPES]
    excluded_scope = [r for r in entry_rows if r['条目类型'] not in CONCRETE_TYPES]

    # 参考值只用于对比，不作为成功条件
    reference = {
        'concrete_laws': 142,
        'copied_files': 369,
        'bytes': 156534344,
        'note': '输入 README 记载的参考规模（含三份 SSO 补下载 PDF）；运行期以索引重算值为准',
    }
    actual_bytes = sum(int(r['字节数']) for r in file_rows)

    # 1. 全部文件核验：存在性、大小、SHA256、路径安全
    verified: dict[str, dict] = {}
    failures: list[dict] = []
    for row in file_rows:
        rel = row['目标相对路径']
        rec: dict[str, Any] = {'ok': False, 'detail': ''}
        try:
            path = safe_relpath(root, rel)
            if not path.is_file():
                rec['detail'] = '文件不存在'
            elif path.stat().st_size != int(row['字节数']):
                rec['detail'] = f"大小不符：实际 {path.stat().st_size} / 索引 {row['字节数']}"
            else:
                digest = sha256_file(path)
                if digest != row['sha256']:
                    rec['detail'] = f'SHA256 不符：实际 {digest[:16]}… / 索引 {row["sha256"][:16]}…'
                else:
                    rec = {'ok': True, 'detail': '存在性/大小/SHA256 一致'}
        except (ValueError, OSError) as exc:
            rec['detail'] = f'路径校验失败：{exc}'
        verified[rel] = rec
        if not rec['ok']:
            failures.append({'source_file': rel, 'reason': rec['detail']})
            append_issue(output, {'level': 'source', 'law_id': None,
                                  'source_file': rel, 'code': 'SOURCE_HASH_OR_PATH_MISMATCH',
                                  'message': rec['detail']})

    # 2. 按 law_id 关联条目与文件
    by_law: dict[str, dict] = {}
    for r in concrete:
        law_id = normalize_law_id('香港' if r['法域'] == '香港' else '新加坡', r['清单标识'])
        by_law[law_id] = {
            'law_id': law_id,
            'jurisdiction': 'hk' if r['法域'] == '香港' else 'sg',
            'code': r['清单标识'],
            'entry_type': r['条目类型'],
            'official_title_zh': r['中文名称'],
            'official_title_en': r['英文名称'],
            'official_url': r['官方链接'],
            'priority': r['级别'],
            'category': r['业务分类'],
            'catalog_status': r['本地状态'],
            'files': [],
            'editions': [],
        }
    unmatched = []
    for row in file_rows:
        law_id = normalize_law_id(row['法域'], row['清单标识'])
        if law_id not in by_law:
            unmatched.append(row['目标相对路径'])
            continue
        by_law[law_id]['files'].append(_source_record(row, root, verified))

    # 3. 逐语言版本选源
    for law_id, law in by_law.items():
        files = law['files']
        langs = sorted({f['language'] for f in files if f['role'] != 'asset'}) or ['en']
        for lang in langs:
            pool = [f for f in files if f['language'] == lang and f['role'] != 'asset']
            assets = [f for f in files if f['role'] == 'asset' and
                      (f['language'] == lang or True)]  # 资产按法例关联，见下方去重说明
            # 资产只登记一次，避免中英文版本重复计数
            if lang != langs[0]:
                assets = []
            unverified = [f for f in pool if not f['hash_verified']]
            pdfs = [f for f in pool if f['role'] in ('official_pdf', 'official_pdf_current')]
            xmls = [f for f in pool if f['role'] == 'official_xml']
            selected = None
            conflicts: list[dict] = []
            reasons: list[str] = []

            if unverified:
                reasons.append('存在未通过摘要/路径核验的来源，阻断该语言版本')
            if law['jurisdiction'] == 'hk':
                if len(xmls) > 1 and len({f['source_sha256'] for f in xmls}) > 1:
                    conflicts.append({'type': 'multiple_official_xml',
                                      'candidates': [f['source_file'] for f in xmls]})
                if xmls:
                    selected = sorted(xmls, key=lambda f: f['source_file'])[0]
                else:
                    reasons.append('未找到官方完整 XML，HTML 不能替代正式原件')
            else:
                if len(pdfs) > 1 and len({f['source_sha256'] for f in pdfs}) > 1:
                    conflicts.append({
                        'type': 'multiple_pdf_versions',
                        'candidates': [{'source_file': f['source_file'],
                                        'version_tag': f['version_tag']} for f in pdfs],
                        'note': '不同摘要的 PDF 无法仅凭文件名或下载时间确定版本，输出冲突而非自动选最新',
                    })
                priority = {'official_pdf_current': 0, 'official_pdf': 1}
                if pdfs and not conflicts:
                    selected = sorted(pdfs, key=lambda f: (priority.get(f['role'], 9),
                                                           f['source_file']))[0]
                elif not pdfs:
                    reasons.append('无已核验官方 PDF；网页/转载 HTML 不自动升级为官方原件')

            if conflicts:
                reasons.append('同一语言存在多个不同摘要的候选原件，需人工确认版本')
            unselected = []
            for f in pool:
                if selected and f['source_file'] == selected['source_file']:
                    continue
                unselected.append({
                    'source_file': f['source_file'],
                    'source_sha256': f['source_sha256'],
                    'role': f['role'],
                    'reason': REFERENCE_REASONS.get(f['role'], '不与选中原件重复抽取'),
                })
            edition = {
                'language': lang,
                'selected_version': selected,
                'unselected_copies': unselected,
                'assets': [{'source_file': f['source_file'],
                            'source_sha256': f['source_sha256'],
                            'bytes': f['bytes'],
                            'reason': REFERENCE_REASONS['asset']} for f in assets],
                'duplicate_sources': [x for f in pool for x in f['duplicate_sources']],
                'version_conflicts': conflicts,
                'inventory_status': 'ready' if (selected and not unverified and not conflicts) else 'blocked',
                'block_reasons': reasons,
            }
            law['editions'].append(edition)

    laws = [by_law[k] for k in sorted(by_law)]
    inventory = {
        'schema_version': SCHEMA_VERSION,
        'source_root': str(root),
        'reference_scale': reference,
        'actual_scale': {
            'concrete_laws': len(laws),
            'indexed_files': len(file_rows),
            'indexed_bytes': actual_bytes,
            'source_verification_ok': len(file_rows) - len(failures),
            'source_verification_failed': len(failures),
            'language_versions': sum(len(l['editions']) for l in laws),
            'ready_versions': sum(1 for l in laws for e in l['editions']
                                  if e['inventory_status'] == 'ready'),
            'blocked_versions': sum(1 for l in laws for e in l['editions']
                                    if e['inventory_status'] == 'blocked'),
        },
        'scale_comparison': {
            'concrete_laws_matches_reference': len(laws) == reference['concrete_laws'],
            'files_matches_reference': len(file_rows) == reference['copied_files'],
        },
        'source_failures': failures,
        'unmatched_files': unmatched,
        'excluded_scope': [{'entry_type': r['条目类型'], 'code': r['清单标识'],
                            'status': r['本地状态']} for r in excluded_scope],
        'out_of_scope_notes': [{'code': r['清单标识'], 'status': r['本地状态']}
                               for r in out_rows],
        'laws': laws,
    }
    atomic_write_json(output / 'inventory.json', inventory)
    return inventory


def inventory_path(output: Path) -> Path:
    return output / 'inventory.json'
