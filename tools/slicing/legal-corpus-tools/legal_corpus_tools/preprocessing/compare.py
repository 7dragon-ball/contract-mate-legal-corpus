# -*- coding: utf-8 -*-
"""代表性原件对照：独立于解析器账本复核 XML 原文/结构，渲染 PDF 关键页供可视核验。

只读输入原件与本批产物；不联网、不调用模型、不修改输入、不写入正式包目录。
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import fitz
from lxml import etree

from .common import atomic_write_json, atomic_write_text, read_json, read_jsonl

# 执行说明 §10.1 指定的代表性输入；全量完成后默认仍只对照这一子集
REPRESENTATIVE_LAWS = {
    'hk:cap26', 'hk:cap586', 'hk:cap60G', 'sg:SGA1979',
    'sg:SGCA2002-RG1', 'sg:SGCA2002-S534-2019', 'sg:IRDA2018',
}


def _no_ws(s: str) -> str:
    return re.sub(r'\s+', '', s or '')


def _char_delta(raw: str, got: str) -> dict:
    """独立字符多重集对照：去空白后按字符计数，不用解析器自己的覆盖账本。"""
    a, b = Counter(_no_ws(raw)), Counter(_no_ws(got))
    miss, extra = a - b, b - a
    return {
        'raw_chars': sum(a.values()), 'provision_chars': sum(b.values()),
        'missing_chars': sum(miss.values()), 'extra_chars': sum(extra.values()),
        'missing_top': miss.most_common(8), 'extra_top': extra.most_common(8),
    }


def _xml_main(path: Path):
    parser = etree.XMLParser(resolve_entities=False, no_network=True,
                             load_dtd=False, huge_tree=True)
    root = etree.parse(str(path), parser).getroot()
    for el in root.iter():
        if isinstance(el.tag, str) and etree.QName(el).localname.lower() == 'main':
            return root, el
    raise RuntimeError(f'XML 缺少 main：{path}')


def _xml_schedule_set(main) -> list[str]:
    out = []
    for el in main.iter():
        if isinstance(el.tag, str) and etree.QName(el).localname.lower() == 'schedule':
            out.append(el.get('id') or el.get('name') or f'pos{len(out) + 1}')
    return out


def _dump_units(unit_dir: Path, stem: str, units: list[dict]) -> list[dict]:
    """首/中/末完整单元单独落盘，避免 Markdown 只放开头预览。"""
    if not units:
        return []
    picks = [('first', units[0]), ('middle', units[len(units) // 2]),
             ('last', units[-1])]
    out = []
    for label, u in picks:
        fn = f'{stem}_{label}.txt'
        atomic_write_text(unit_dir / fn,
                          f"provision_id: {u['provision_id']}\nkind: {u['kind']}\n"
                          f"heading: {u.get('heading', '')}\n"
                          f"hierarchy: {json.dumps(u.get('hierarchy', []), ensure_ascii=False)}\n"
                          f"source_locator: {json.dumps(u.get('source_locator', {}), ensure_ascii=False)}\n"
                          f"--- full text ({len(u['text'])} chars) ---\n{u['text']}\n")
        out.append({'label': label, 'provision_id': u['provision_id'],
                    'kind': u['kind'], 'chars': len(u['text']),
                    'full_text_file': f'units/{fn}',
                    'preview': u['text'][:600]})
    return out


def _xml_audit(vdir: Path, source_root: Path, unit_dir: Path, stem: str) -> dict:
    src = read_json(vdir / 'sources.json')[0]
    path = source_root / src['source_file']
    root, main = _xml_main(path)
    units = read_jsonl(vdir / 'provisions.jsonl')
    rep = read_json(vdir / 'extraction-report.json')
    raw = ''.join(main.itertext())
    delta = _char_delta(raw, ''.join(u['text'] for u in units))
    sched_xml = _xml_schedule_set(main)
    sched_prov = [u['provision_id'] for u in units if u['kind'] == 'schedule']
    return {
        'format': 'xml', 'source_file': src['source_file'],
        'source_sha256': src['source_sha256'],
        'meta': rep.get('meta', {}),
        'coverage': rep.get('coverage', {}),
        'by_kind': rep.get('by_kind', {}),
        'tables': len(rep.get('tables', [])),
        'assets': [{'reference': a.get('reference'), 'status': a.get('status'),
                    'sha256': a.get('sha256')} for a in rep.get('assets', [])],
        'char_delta': delta,
        'schedule_ids_in_xml': sched_xml,
        'schedule_ids_in_provisions': sched_prov,
        'note_units': sum(1 for u in units if u['kind'] == 'note'),
        'selected_units': _dump_units(unit_dir, stem, units),
    }


def _select_pages(report: dict, units: list[dict], total: int) -> tuple[list[int], dict | None]:
    pages: set[int] = {1, total, max(1, total // 2)}
    start = (report.get('body_start_line') or {}).get('page')
    if start:
        pages.update({start, min(start + 1, total)})
    for p in (report.get('abnormal_pages') or [])[:2]:
        pages.add(min(max(1, int(p)), total))
    cross = next((u for u in units
                  if len((u.get('source_locator') or {}).get('pages') or []) > 1), None)
    if cross:
        for p in cross['source_locator']['pages'][:2]:
            pages.add(min(max(1, int(p)), total))
    return sorted(pages), cross


def _pdf_audit(vdir: Path, source_root: Path, unit_dir: Path, stem: str,
               pages_dir: Path) -> dict:
    src = read_json(vdir / 'sources.json')[0]
    path = source_root / src['source_file']
    units = read_jsonl(vdir / 'provisions.jsonl')
    rep = read_json(vdir / 'extraction-report.json')
    raw_pages = {r['page']: r for r in read_jsonl(vdir / 'raw-pages.jsonl')}
    total = rep['total_pages']
    selected, cross = _select_pages(rep, units, total)
    rendered = []
    doc = fitz.open(str(path))
    try:
        for pno in selected:
            pix = doc[pno - 1].get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
            fn = f'{stem}_p{pno:04d}.png'
            pix.save(str(pages_dir / fn))
            rendered.append(f'pages/{fn}')
    finally:
        doc.close()
    page_rows = []
    for pno in selected:
        r = raw_pages.get(pno, {})
        page_rows.append({
            'page': pno,
            'image': f'pages/{stem}_p{pno:04d}.png',
            'raw_chars': len(r.get('raw_text', '')),
            'cleaned_chars': len(r.get('cleaned_text', '')),
            'removed_header_footer': r.get('removed_header_footer', []),
            'is_blank': r.get('is_blank'), 'suspect': r.get('suspect'),
            'n_images': r.get('n_images'), 'n_drawings': r.get('n_drawings'),
            'raw_text_head': r.get('raw_text', '')[:700],
            'cleaned_text_head': r.get('cleaned_text', '')[:700],
        })
    return {
        'format': 'pdf', 'source_file': src['source_file'],
        'source_sha256': src['source_sha256'],
        'total_pages': total, 'raw_pages_saved': len(raw_pages),
        'blank_pages': rep.get('blank_pages', []),
        'toc_pages': rep.get('toc_pages', []),
        'body_start_line': rep.get('body_start_line'),
        'body_end_line': rep.get('body_end_line'),
        'abnormal_pages': rep.get('abnormal_pages', []),
        'section_count': rep.get('section_count'),
        'schedule_count': rep.get('schedule_count'),
        'by_kind': rep.get('by_kind', {}),
        'raw_chars': rep.get('raw_chars'), 'cleaned_chars': rep.get('cleaned_chars'),
        'unassigned_text': rep.get('unassigned_text'),
        'version_fields': rep.get('version_fields'),
        'selected_pages': selected, 'rendered_images': rendered,
        'page_rows': page_rows,
        'cross_page_unit': ({'provision_id': cross['provision_id'],
                             'pages': cross['source_locator']['pages'],
                             'text': cross['text'][:3000]} if cross else None),
        'selected_units': _dump_units(unit_dir, stem, units),
    }


def build_comparison(source_root: Path, output: Path, out_dir: Path | None = None,
                     law_ids: list[str] | None = None, all_versions: bool = False) -> dict:
    """生成原件对照记录；默认只处理代表性 7 部法例 / 10 个语言版本。"""
    source_root = Path(source_root).resolve()
    output = Path(output).resolve()
    out_dir = Path(out_dir).resolve() if out_dir else output / '对照'
    (out_dir / 'pages').mkdir(parents=True, exist_ok=True)
    (out_dir / 'units').mkdir(parents=True, exist_ok=True)
    wanted = None if all_versions else (set(law_ids) if law_ids else REPRESENTATIVE_LAWS)
    audits: list[dict] = []
    for vdir in sorted(p for p in output.glob('*/*/*/*') if (p / 'validation.json').is_file()):
        val = read_json(vdir / 'validation.json')
        law_id = val['law_id']
        if wanted is not None and law_id not in wanted:
            continue
        lang = val['language']
        stem = f'{law_id.replace(":", "_")}_{lang}'
        rep = read_json(vdir / 'extraction-report.json') or {}
        fmt = rep.get('format')
        try:
            audit = (_xml_audit(vdir, source_root, out_dir / 'units', stem)
                     if fmt == 'xml' else
                     _pdf_audit(vdir, source_root, out_dir / 'units', stem,
                                out_dir / 'pages'))
        except Exception as exc:
            audit = {'format': fmt, 'error': f'{type(exc).__name__}: {exc}'}
        audit.update({'law_id': law_id, 'language': lang,
                      'version_dir': str(vdir.relative_to(output)),
                      'extraction_status': val['extraction_status'],
                      'processing_status': val['processing_status'],
                      'release_eligible': val['release_eligible'],
                      'counts': val['counts']})
        audits.append(audit)
    atomic_write_json(out_dir / 'audit.json', {'audits': audits})
    atomic_write_text(out_dir / '代表性原件对照.md', _markdown(audits))
    return {'out_dir': str(out_dir), 'versions': len(audits),
            'xml': sum(1 for a in audits if a['format'] == 'xml'),
            'pdf': sum(1 for a in audits if a['format'] == 'pdf'),
            'rendered_pages': sum(len(a.get('rendered_images', [])) for a in audits)}


def _md_unit_picks(a: dict) -> list[str]:
    lines = ['', '**完整选定单元（首／中／末）**', '',
             '| 位置 | provision_id | kind | 字符数 | 全文文件 |', '|---|---|---|---:|---|']
    for u in a.get('selected_units', []):
        lines.append(f"| {u['label']} | `{u['provision_id']}` | {u['kind']} | "
                     f"{u['chars']} | `{u['full_text_file']}` |")
    for u in a.get('selected_units', []):
        lines += ['', f"#### {u['label']} · `{u['provision_id']}`（预览，完整文本见上表文件）", '',
                  '```text', u['preview'], '```']
    return lines


def _markdown(audits: list[dict]) -> str:
    lines = [
        '# 代表性原件对照记录', '',
        '生成方式：`legal_corpus_tools.preprocess compare`。只读输入原件与本批产物，',
        '不联网、不调用模型、不修改输入。', '',
        '对照方法：', '',
        '- 香港 XML：用独立 `lxml` 解析 `main`，以 `main.itertext()` 与 `provisions.jsonl` 的',
        '  全部 `text` 做去空白字符多重集对照（**不复用解析器自身的覆盖账本**）；',
        '  另比对附表 ID 集合、结构 kind 分布、表格与图像资产，并导出首／中／末完整单元。',
        '- 新加坡 PDF：渲染正文首／正文起点／中部／末页／异常页／跨页条文所在页为 PNG，',
        '  与 `raw-pages.jsonl` 的原始页文本、清洗文本、页眉页脚移除清单逐页对照。', '',
        '> 字符对照中允许的额外字符：表格结构化转录使用的 `|` 分隔符；',
        '> 该字符不来自法律原文，需与「吞字／重复」区分。', '',
        '## 汇总', '',
        '| 法例 | 语言 | 格式 | 状态 | 单元 | 切片 | 原文缺失字符 | 额外字符 |',
        '|---|---|---|---|---:|---:|---:|---:|',
    ]
    for a in audits:
        c = a.get('counts', {})
        d = a.get('char_delta', {})
        lines.append(f"| `{a['law_id']}` | {a['language']} | {a['format']} | "
                     f"{a['extraction_status']} | {c.get('provisions', '-')} | "
                     f"{c.get('chunks', '-')} | {d.get('missing_chars', '—')} | "
                     f"{d.get('extra_chars', '—')} |")
    for a in audits:
        lines += ['', '---', '', f"## `{a['law_id']}` · {a['language']} · {a['format']}", '',
                  f"- 版本目录：`{a['version_dir']}`",
                  f"- 原件：`{a.get('source_file')}`",
                  f"- 原件 SHA256：`{a.get('source_sha256')}`",
                  f"- 技术状态：`{a['processing_status']}`／抽取 `{a['extraction_status']}`／"
                  f"发布资格 `{a['release_eligible']}`（技术通过不等于时效核验）"]
        if a.get('error'):
            lines += ['', f"**对照失败**：{a['error']}"]
            continue
        if a['format'] == 'xml':
            cov = a.get('coverage', {})
            d = a['char_delta']
            lines += ['',
                      f"- 覆盖账本：`ok={cov.get('ok')}`，文本片段 {cov.get('total_text_fragments')}，"
                      f"缺失 {len(cov.get('missing', []))}，重复 {len(cov.get('duplicated', []))}",
                      f"- 原文／产物字符：{d['raw_chars']} / {d['provision_chars']}；"
                      f"**缺失 {d['missing_chars']}**、额外 {d['extra_chars']}"
                      f"（额外分布 `{d['extra_top']}`）",
                      f"- 结构分布：`{json.dumps(a['by_kind'], ensure_ascii=False)}`；"
                      f"结构化表格 {a['tables']} 个；note 单元 {a['note_units']} 个",
                      f"- 原件附表 ID：`{a['schedule_ids_in_xml']}`",
                      f"- 产物附表 ID：`{a['schedule_ids_in_provisions']}`",
                      f"- 图像资产：`{json.dumps(a['assets'], ensure_ascii=False)}`"]
        else:
            lines += ['',
                      f"- 页数 {a['total_pages']}，`raw-pages.jsonl` 记录 {a['raw_pages_saved']} 页；"
                      f"空白页 `{a['blank_pages']}`",
                      f"- 目录页 `{a['toc_pages']}`；正文起 `{a['body_start_line']}`；"
                      f"全文尾 `{a['body_end_line']}`",
                      f"- 条文 {a['section_count']}，附表 {a['schedule_count']}，"
                      f"结构 `{json.dumps(a['by_kind'], ensure_ascii=False)}`",
                      f"- 原始／清洗字符 {a['raw_chars']} → {a['cleaned_chars']}；"
                      f"无法归属文本 `{str(a['unassigned_text'])[:200]}`",
                      f"- 异常页 `{a['abnormal_pages']}`；版本字段 "
                      f"`{json.dumps(a['version_fields'], ensure_ascii=False)}`",
                      f"- 已渲染可视对照页：{', '.join(f'`{x}`' for x in a['rendered_images'])}",
                      '', '**逐页文本对照**', '',
                      '| 页 | 图片 | 原始/清洗字符 | 页眉页脚移除 | 图像/绘图 | 空白/可疑 |',
                      '|---:|---|---:|---|---|---|']
            for r in a['page_rows']:
                lines.append(f"| {r['page']} | `{r['image']}` | {r['raw_chars']}/{r['cleaned_chars']} | "
                             f"{len(r['removed_header_footer'])} 行 | "
                             f"{r['n_images']}/{r['n_drawings']} | "
                             f"{r['is_blank']}/{r['suspect']} |")
            cross = a.get('cross_page_unit')
            if cross:
                lines += ['', f"**跨页条文** `{cross['provision_id']}` · 页 {cross['pages']}", '',
                          '```text', cross['text'], '```']
            lines += ['', '<details><summary>逐页原始文本（前 700 字符）</summary>', '']
            for r in a['page_rows']:
                lines += ['', f"##### 第 {r['page']} 页 · raw", '', '```text',
                          r['raw_text_head'], '```', '', f"##### 第 {r['page']} 页 · cleaned", '',
                          '```text', r['cleaned_text_head'], '```']
            lines += ['', '</details>']
        lines += _md_unit_picks(a)
    return '\n'.join(lines) + '\n'
