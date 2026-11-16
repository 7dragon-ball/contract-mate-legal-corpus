# -*- coding: utf-8 -*-
"""新加坡 SSO PDF：逐页抽取、页眉页脚清洗、目录/正文边界、条号识别与跨页拼接。"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import fitz

from .common import PARSER_VERSION

SECTION_LINE = re.compile(r'^\s*(\d{1,3}[A-Z]?)\.\s*(.*)$')
NEW_BLOCK = re.compile(r'^\s*(?:\(\d+[A-Za-z]?\)|\([a-z]{1,3}\)|\d+[A-Z]?\.\s|PART\s|DIVISION\s|SCHEDULE\b|\[)')
PART_LINE = re.compile(r'^\s*(PART|DIVISION|CHAPTER)\s+([0-9IVXLC]+)\b', re.I)
SCHEDULE_LINE = re.compile(r'^\s*(THE\s+)?(FIRST|SECOND|THIRD|FOURTH|FIFTH|SIXTH|SEVENTH|EIGHTH|NINTH|TENTH)?\s*SCHEDULE\b', re.I)
TOC_MARK = re.compile(r'ARRANGEMENT OF SECTIONS|Table of Contents|CONTENTS', re.I)
BODY_START = re.compile(r'^\s*(An Act to|Enacting Formula|No\.\s*S\s*\d+|In exercise of the powers)', re.I)
TAIL_MARK = re.compile(r'COMPARATIVE TABLE|LEGISLATIVE HISTORY|LEGISLATIVE HISTORY', re.I)
VERSION_AS_AT = re.compile(r'Current version as at\s+([0-9]{1,2}\s+[A-Za-z]{3,9}\s+[0-9]{4})', re.I)
PDF_CREATED = re.compile(r'PDF created date on:\s*([0-9]{1,2}\s+[A-Za-z]{3,9}\s+[0-9]{4})', re.I)
IN_FORCE_FROM = re.compile(r'version in force from\s+([0-9/]+)', re.I)


def _header_footer_candidates(pages: list[dict]) -> set[str]:
    """按重复模式 + 页面位置识别页眉页脚文本（不全局删除相同字符串）。"""
    top: Counter = Counter()
    bottom: Counter = Counter()
    for p in pages:
        h = p['height']
        for ln in p['lines']:
            y = ln['bbox'][1] if ln['bbox'] else 0
            t = ln['text'].strip()
            if not t:
                continue
            if y < h * 0.12:
                top[t] += 1
            elif y > h * 0.88:
                bottom[t] += 1
    repeated = {t for t, n in top.items() if n >= 2}
    repeated |= {t for t, n in bottom.items() if n >= 2}
    repeated |= {t for t in top if VERSION_AS_AT.search(t) or PDF_CREATED.search(t)}
    repeated |= {t for t in bottom if IN_FORCE_FROM.search(t)}
    return repeated


def _grid_lines(page) -> tuple[int, int]:
    """统计绘图中的水平/垂直长线段：带边框表格在文本里没有 '|'，必须靠矢量线检测。"""
    h = v = 0
    try:
        drawings = page.get_drawings()
    except Exception:
        return 0, 0
    for d in drawings:
        for item in d.get('items', []):
            kind = item[0]
            if kind == 'l' and len(item) >= 3:
                p1, p2 = item[1], item[2]
                if abs(p1.y - p2.y) < 2 and abs(p1.x - p2.x) > 40:
                    h += 1
                elif abs(p1.x - p2.x) < 2 and abs(p1.y - p2.y) > 12:
                    v += 1
            elif kind == 're' and len(item) >= 2:
                r = item[1]
                w, ht = abs(getattr(r, 'width', 0)), abs(getattr(r, 'height', 0))
                if w > 40 and ht < 6:
                    h += 1
                elif ht > 12 and w < 6:
                    v += 1
    return h, v


def _extract_pages(doc) -> list[dict]:
    pages = []
    for i, page in enumerate(doc, 1):
        d = page.get_text('dict')
        lines = []
        for block in d.get('blocks', []):
            if block.get('type') != 0:
                continue
            for line in block.get('lines', []):
                text = ''.join(span.get('text', '') for span in line.get('spans', []))
                lines.append({'text': text, 'bbox': [round(x, 1) for x in line.get('bbox', [])]})
        try:
            annots = len(list(page.annots() or []))
        except Exception:
            annots = 0
        try:
            widgets = len(list(page.widgets() or []))
        except Exception:
            widgets = 0
        raw = page.get_text('text')
        gh, gv = _grid_lines(page)
        pages.append({
            'page': i, 'width': round(page.rect.width, 1), 'height': round(page.rect.height, 1),
            'raw_text': raw, 'lines': lines,
            'n_images': len(page.get_images(full=True)),
            'n_drawings': len(page.get_drawings()),
            'n_annots': annots, 'n_widgets': widgets,
            'grid_hlines': gh, 'grid_vlines': gv,
        })
    return pages


def _clean_pages(pages: list[dict], repeated: set[str]) -> dict:
    stats: dict[str, Any] = {'raw_chars': 0, 'cleaned_chars': 0,
                             'blank_pages': [], 'suspect_pages': [], 'short_pages': [],
                             'removed_lines': 0, 'multi_column_pages': [],
                             'table_candidate_pages': [], 'image_pages': []}
    for p in pages:
        kept = []
        removed = []
        h = p['height']
        for ln in p['lines']:
            t = ln['text'].strip()
            # 只删真正位于页眉/页脚带的重复行；同一字符串出现在表格中部时不得删（如表头 2020 Ed.）
            y = ln['bbox'][1] if ln['bbox'] else 0
            at_edge = y < h * 0.12 or y > h * 0.88
            if t in repeated and at_edge:
                removed.append(t)
                continue
            kept.append(ln)
        p['cleaned_lines'] = kept
        offset, spans = 0, []
        for ln in kept:
            start = offset
            end = start + len(ln['text'])
            spans.append({'start': start, 'end': end, 'text': ln['text'],
                          'bbox': ln['bbox']})
            offset = end + 1  # 行间单个 \n
        p['cleaned_text'] = '\n'.join(ln['text'] for ln in kept)
        p['line_spans'] = spans
        p['removed_header_footer'] = removed
        stats['raw_chars'] += len(p['raw_text'])
        stats['cleaned_chars'] += len(p['cleaned_text'])
        stats['removed_lines'] += len(removed)
        no_text = not p['raw_text'].strip()
        has_objects = p['n_images'] or p['n_drawings'] or p['n_annots'] or p['n_widgets']
        if no_text and not has_objects:
            p['is_blank'] = True
            p['blank_reason'] = '无文字、图像、绘图、注释及表单'
            stats['blank_pages'].append(p['page'])
        else:
            p['is_blank'] = False
            p['blank_reason'] = ''
            cleaned_len = len(p['cleaned_text'].strip())
            if cleaned_len < 120:
                stats['short_pages'].append(p['page'])
            # 疑似扫描页只认「完全无文字 + 含位图图像」。表格矢量线、对照表数字页
            # 都有可读文字，属短页而非扫描页，不得据此阻断整部法律。
            if cleaned_len < 20 and p['n_images'] > 0 and not p['raw_text'].strip():
                p['suspect'] = True
                stats['suspect_pages'].append(p['page'])
            else:
                p['suspect'] = False
        if p['n_images']:
            stats['image_pages'].append(p['page'])
        # 多栏粗略检测：同一 y 带上出现左右两个明显分离的 x 起点簇
        xs = defaultdict(list)
        for ln in kept:
            if ln['bbox']:
                xs[round(ln['bbox'][1] / 12)].append(round(ln['bbox'][0]))
        bands = [v for v in xs.values() if len(set(v)) >= 2 and max(v) - min(v) > 160]
        if len(bands) >= 3:
            stats['multi_column_pages'].append(p['page'])
        p['table_grid'] = p.get('grid_hlines', 0) >= 2 and p.get('grid_vlines', 0) >= 1
        if p['table_grid'] or sum(1 for ln in kept if ln['text'].count('|') >= 2) >= 2:
            stats['table_candidate_pages'].append(p['page'])
    return stats


def _is_section_line(text: str) -> re.Match | None:
    m = SECTION_LINE.match(text)
    if not m:
        return None
    rest = m.group(2).strip()
    if not rest:
        return None                        # 目录中的独立条号行
    # SSO 附属法例常用「7.  Where —」「35A.  In this Part —」引出分项列表；
    # 破折号结尾的短标题同样是正文条文，不能只按长度阈值吞掉。
    if (rest.startswith(('—', '–', '-', '(')) or
            rest.endswith(('—', '–', '-', ':')) or len(rest) >= 25):
        return m
    return None


def _is_schedule_heading(text: str) -> bool:
    """附表标题是独立的全大写行（FIRST SCHEDULE / SECOND SCHEDULE）。

    正文里的「First Schedule; or」「Seventh Schedule —」「Schedule to the ...」
    只是对附表的引用，绝不能据此切换附表上下文，否则后面的正文条文会被
    错误挂到 schedule:N/section:M 之下。
    """
    if not SCHEDULE_LINE.match(text):
        return False
    core = text.strip().rstrip(' \t.,;:—–-')
    return bool(core) and core.isupper()


def _merge_soft(lines: list[str]) -> str:
    out: list[str] = []
    buf = ''
    for ln in lines:
        t = ln.strip()
        if not t:
            continue
        if NEW_BLOCK.match(t) or PART_LINE.match(t) or _is_schedule_heading(t):
            if buf:
                out.append(buf)
            buf = t
        elif buf:
            buf = buf + ' ' + t
        else:
            buf = t
    if buf:
        out.append(buf)
    return '\n'.join(out)


def _versions(pages: list[dict]) -> dict:
    raw = '\n'.join(p['raw_text'] for p in pages[:3])
    all_raw = '\n'.join(p['raw_text'] for p in pages)
    as_of = VERSION_AS_AT.search(all_raw)
    created = PDF_CREATED.search(all_raw)
    force = IN_FORCE_FROM.search(all_raw)
    evidence = []
    for line in all_raw.splitlines():
        if VERSION_AS_AT.search(line) or PDF_CREATED.search(line) or IN_FORCE_FROM.search(line):
            evidence.append(line.strip())
    return {
        'source_as_of': as_of.group(1) if as_of else None,
        'pdf_created_date': created.group(1) if created else None,
        'effective_from': force.group(1) if force else None,
        'version_evidence': list(dict.fromkeys(evidence))[:12],
        'cover_note': raw.strip()[:300],
        'legal_status': 'unknown',
        'note': '现行版本日期与 PDF 生成日不等于修法或条文生效日；未联网核验时效',
    }


def _make_unit(pid: str, kind: str, heading: str, hierarchy: list[str],
               lines: list[dict], unit_of: str) -> dict:
    text = _merge_soft([l['text'] for l in lines])
    pages = sorted({l['page'] for l in lines})
    spans = []
    for pg in pages:
        seg = [l for l in lines if l['page'] == pg]
        if seg:
            spans.append({'page': pg, 'start': min(l['start'] for l in seg),
                          'end': max(l['end'] for l in seg)})
    return {
        'provision_id': pid, 'kind': kind, 'heading': heading,
        'hierarchy': list(hierarchy), 'text': text,
        'source_locator': {'pages': pages, 'page_spans': spans,
                           'offset_unit': 'characters in raw-pages.jsonl cleaned_text',
                           'official_section': pid.split(':')[-1] if kind == 'section' else None},
        'commencement_status': 'unknown',
        'warnings': ['仅从本地 PDF 抽取；未联网核验当前时效'],
        'text_role': 'original',
    }


def extract(path: Path) -> dict:
    """返回 units / raw_pages / report / warnings；结构不可靠时以 page blocks 降级。"""
    doc = fitz.open(str(path))
    pages = _extract_pages(doc)
    doc.close()
    repeated = _header_footer_candidates(pages)
    stats = _clean_pages(pages, repeated)
    versions = _versions(pages)

    flat: list[dict] = []
    for p in pages:
        for s in p['line_spans']:
            if not s['text'].strip():
                continue
            flat.append({'page': p['page'], 'start': s['start'], 'end': s['end'],
                         'text': s['text'], 'bbox': s['bbox']})
    flat.sort(key=lambda x: (x['page'], x['start']))

    toc_pages: list[int] = []
    body_start_index = None
    first_section_index = None
    toc_mode = False
    for i, ln in enumerate(flat):
        if TOC_MARK.search(ln['text']):
            toc_mode = True
            toc_pages.append(ln['page'])
        if BODY_START.search(ln['text']) and body_start_index is None:
            body_start_index = i
            toc_mode = False
        m = _is_section_line(ln['text'])
        if m:
            rest = m.group(2).strip()
            strong_body = rest.startswith(('—', '–', '-', '(')) or len(rest) >= 60
            if not toc_mode or strong_body:
                first_section_index = i
                break
    if first_section_index is None:
        # 无可靠条号：整份降级为 page blocks
        return _fallback(pages, flat, stats, versions, repeated,
                         '未识别到可靠条文起始行，无法生成正式 provisions')

    # 目录区按「首个正文条文之前、且存在目录标记」的全部页登记
    front = flat[:first_section_index]
    if any(TOC_MARK.search(l['text']) for l in front):
        toc_pages = sorted({l['page'] for l in front})
    units: list[dict] = []
    if front:
        units.append(_make_unit('preface:1', 'preface', '', [], front, 'preface'))
        units[-1]['warnings'].append('含目录、制定公式等前置内容；目录行不作为条文正文')

    body = flat[first_section_index:]
    # 按条号 / Part / 附表 / 尾部资料切块
    markers: list[tuple[int, str, str, str, list[str]]] = []
    part_stack: list[str] = []
    schedule_prefix = ''
    tail_index = None
    for i, ln in enumerate(body):
        t = ln['text'].strip()
        if TAIL_MARK.search(t) and i > 0:
            tail_index = i
            break
        m = PART_LINE.match(t)
        if m:
            part_stack = [t]
            # 附表内的数字分部（PART 1 / PART 2）不结束附表上下文；
            # 正文 Part 用罗马数字（PART II / PART III / PART V）。
            if not m.group(2).isdigit():
                schedule_prefix = ''
            continue
        if _is_schedule_heading(t):
            sid = f'schedule:{len([x for x in markers if x[1] == "schedule"]) + 1}'
            markers.append((i, 'schedule', sid, t.strip(), list(part_stack)))
            schedule_prefix = sid + '/'
            continue
        sec = _is_section_line(t)
        if sec:
            num = sec.group(1)
            base = f'section:{num}'
            markers.append((i, 'section', (schedule_prefix + base) if schedule_prefix else base,
                            num, list(part_stack)))

    seen: set[str] = set()
    normalized_markers = []
    for i, kind, pid, num, hier in markers:
        if pid in seen:
            n = 2
            while f'{pid}#{n}' in seen:
                n += 1
            pid = f'{pid}#{n}'
        seen.add(pid)
        normalized_markers.append((i, kind, pid, num, hier))

    for j, (idx, kind, pid, num, hier) in enumerate(normalized_markers):
        end = normalized_markers[j + 1][0] if j + 1 < len(normalized_markers) else (tail_index or len(body))
        seg = body[idx:end]
        if not seg:
            continue
        heading = ''
        # 条文标题：取条号上方最近的短标题行（若未被目录占用）
        if kind == 'section':
            back = 1
            while idx - back >= 0 and back <= 3:
                prev = body[idx - back]['text'].strip()
                if prev and not _is_section_line(prev) and len(prev) < 90 and not NEW_BLOCK.match(prev):
                    heading = prev
                    break
                back += 1
        u = _make_unit(pid, kind, heading, hier, seg, kind)
        if kind == 'schedule':
            u['heading'] = num
        units.append(u)

    if tail_index is not None and tail_index < len(body):
        tail = body[tail_index:]
        units.append(_make_unit('supplement:historical', 'supplement', '', [], tail, 'supplement'))
        units[-1]['warnings'].append('修订/对照表等历史资料，与正文条文分列')

    warnings: list[str] = []
    if stats['suspect_pages']:
        warnings.append(f"疑似扫描页（无文字且含图像）：{stats['suspect_pages'][:10]}")
    if stats['short_pages']:
        warnings.append(f"短文本页（已逐页保留并核对，不等于扫描页）：{stats['short_pages'][:10]}")
    if stats['multi_column_pages']:
        warnings.append(f"多栏版面候选页：{stats['multi_column_pages'][:10]}")
    if stats['image_pages']:
        warnings.append(f"含图像页：{stats['image_pages'][:10]}；图像内容不由纯文本覆盖")
    if stats['table_candidate_pages']:
        warnings.append(
            f"表格候选页：{stats['table_candidate_pages'][:10]}；带边框表格按纯文本行抽取，"
            "行列对应关系未结构化恢复，文本已保留但不得据此宣称表格内容完整，需人工补充")

    section_count = sum(1 for u in units if u['kind'] == 'section')
    # 多栏候选、短页、表格、图像均登记为版面/资产提示，不单独阻断整部法律：
    # 右侧修订注记与两栏对照表会被粗略多栏检测命中，但正文仍是单栏且条号可识别。
    # 只有完全无法识别条文（或真正无文字的扫描页）才降级为 page blocks 草稿。
    blocked = section_count == 0 or bool(stats['suspect_pages'])
    if blocked:
        return _fallback(pages, flat, stats, versions, repeated,
                         '未识别到可靠条文或存在无文字扫描页，生成 page blocks 草稿（不进入正式 provisions）')

    report = {
        'parser_version': PARSER_VERSION,
        'format': 'pdf',
        'total_pages': len(pages),
        'blank_pages': stats['blank_pages'],
        'suspect_pages': stats['suspect_pages'],
        'short_pages': stats['short_pages'],
        'layout_blocking_policy': '多栏候选/短页/表格/图像只登记为版面提示；仅无可靠条号或无文字扫描页才降级',
        'raw_chars': stats['raw_chars'],
        'cleaned_chars': stats['cleaned_chars'],
        'removed_header_footer_lines': stats['removed_lines'],
        'repeated_header_footer_patterns': sorted(repeated),
        'toc_pages': sorted(set(toc_pages)),
        'body_start_line': {'page': flat[first_section_index]['page'],
                            'start': flat[first_section_index]['start']},
        'body_end_line': {'page': flat[-1]['page'], 'end': flat[-1]['end']},
        'section_count': section_count,
        'schedule_count': sum(1 for u in units if u['kind'] == 'schedule'),
        'table_candidate_pages': stats['table_candidate_pages'],
        'table_structure_note': (
            '带边框表格的行列对应关系未结构化恢复；文本保留于 supplement，'
            '不得据此宣称表格内容完整，需人工补充') if stats['table_candidate_pages'] else None,
        'image_pages': stats['image_pages'],
        'multi_column_pages': stats['multi_column_pages'],
        'unassigned_text': '目录/前置内容归入 preface，修订对照表归入 supplement，无静默丢弃',
        'abnormal_pages': stats['suspect_pages'] + stats['multi_column_pages'],
        'version_fields': versions,
        'warnings': warnings,
        'by_kind': dict(Counter(u['kind'] for u in units)),
    }
    return {'units': units, 'raw_pages': pages, 'report': report,
            'warnings': warnings, 'blocked': False, 'versions': versions}


def _fallback(pages, flat, stats, versions, repeated, reason: str) -> dict:
    blocks = []
    for p in pages:
        lines = [s for s in p['line_spans'] if s['text'].strip()]
        # 每 12 行一个草稿块，仅用于人工检查
        for i in range(0, len(lines), 12):
            seg = lines[i:i + 12]
            blocks.append({
                'unit_id': f'page:block:{p["page"]}:{i // 12 + 1}',
                'segmentation_level': 'page_block',
                'kind': 'page_block', 'heading': '', 'hierarchy': [],
                'text': '\n'.join(s['text'] for s in seg),
                'source_locator': {'pages': [p['page']],
                                   'page_spans': [{'page': p['page'],
                                                   'start': seg[0]['start'],
                                                   'end': seg[-1]['end']}],
                                   'offset_unit': 'characters in raw-pages.jsonl cleaned_text'},
                'commencement_status': 'unknown', 'warnings': [reason],
                'text_role': 'draft',
            })
    report = {
        'parser_version': PARSER_VERSION, 'format': 'pdf',
        'total_pages': len(pages), 'blank_pages': stats['blank_pages'],
        'suspect_pages': stats['suspect_pages'], 'raw_chars': stats['raw_chars'],
        'cleaned_chars': stats['cleaned_chars'],
        'removed_header_footer_lines': stats['removed_lines'],
        'repeated_header_footer_patterns': sorted(repeated),
        'toc_pages': [], 'section_count': 0, 'schedule_count': 0,
        'table_candidate_pages': stats['table_candidate_pages'],
        'image_pages': stats['image_pages'],
        'multi_column_pages': stats['multi_column_pages'],
        'unassigned_text': '全部正文以 page blocks 草稿保留',
        'abnormal_pages': stats['suspect_pages'] + stats['multi_column_pages'],
        'version_fields': versions,
        'warnings': [reason], 'by_kind': {'page_block': len(blocks)},
        'blocking_reason': reason,
    }
    return {'units': [], 'draft_blocks': blocks, 'raw_pages': pages,
            'report': report, 'warnings': [reason], 'blocked': True,
            'versions': versions}


def raw_page_row(p: dict) -> dict:
    return {
        'page': p['page'], 'width': p['width'], 'height': p['height'],
        'raw_text': p['raw_text'], 'cleaned_text': p['cleaned_text'],
        'line_spans': p['line_spans'],
        'removed_header_footer': p['removed_header_footer'],
        'n_images': p['n_images'], 'n_drawings': p['n_drawings'],
        'n_annots': p['n_annots'], 'n_widgets': p['n_widgets'],
        'grid_hlines': p.get('grid_hlines', 0), 'grid_vlines': p.get('grid_vlines', 0),
        'table_grid': p.get('table_grid', False),
        'is_blank': p['is_blank'], 'blank_reason': p['blank_reason'],
        'suspect': p.get('suspect', False),
    }
