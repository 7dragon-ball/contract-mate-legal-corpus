# -*- coding: utf-8 -*-
"""香港 HKeL 官方 XML 全文结构化：禁用外部实体/网络，按 local-name 解析并做文本片段覆盖对账。"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

from lxml import etree

from .common import sha256_file

NOTE_TAGS = {'sourcenote', 'referencenote', 'commencementnote', 'editorialnote',
             'note', 'footnote', 'repealednote', 'amendingnote'}
CONTAINER_HEADINGS = {'part', 'division', 'subdivision', 'chapter'}
LEAF_HEADINGS = {'crossheading'}
TITLE_NODES = {'num', 'heading', 'title', 'label'}
BLOCK_TAGS = {'section', 'subsection', 'paragraph', 'subparagraph', 'content',
              'num', 'leadin', 'def', 'longtitle', 'proviso', 'continued',
              'text', 'item', 'list', 'form', 'appendix'}
ASSET_TAGS = {'img', 'image', 'graphic', 'figure'}
TABLE_TAGS = {'table', 'xhtml:table'}


def _local(el) -> str:
    tag = el.tag
    if not isinstance(tag, str):
        return 'comment'
    try:
        return etree.QName(el).localname.lower()
    except ValueError:
        return tag.lower()


def _norm(s: str | None) -> str:
    if not s:
        return ''
    return re.sub(r'[ \t\r\n\f\v]+', ' ', s).strip()


def _norm_keep(s: str | None) -> str:
    """折叠连续空白但保留首尾边界，避免相邻文本片段被无间隔拼接（如 as theSale）。"""
    if not s:
        return ''
    return re.sub(r'[ \t\r\n\f\v]+', ' ', s)


def _join(parts: list[str]) -> str:
    s = ''.join(parts)
    s = re.sub(r'[ \t]*\n[ \t]*', '\n', s)
    s = re.sub(r'\n{2,}', '\n', s)
    s = re.sub(r' {2,}', ' ', s)
    return s.strip()


class XmlExtractor:
    """逐文本片段记账：每个 main 文本节点恰好进入一个单元，或记录明确排除原因。"""

    def __init__(self, path: Path, language_dir: Path):
        parser = etree.XMLParser(resolve_entities=False, no_network=True,
                                 load_dtd=False, huge_tree=True, recover=False)
        self.tree = etree.parse(str(path), parser)
        self.root = self.tree.getroot()
        self.path = path
        self.language_dir = language_dir
        self.fragments: list[dict] = []
        self.frag_index: dict[str, int] = {}
        self.consumed: Counter = Counter()
        self.unit_of_frag: dict[int, str] = {}
        self.excluded: list[dict] = []
        self.units: list[dict] = []
        self.tables: list[dict] = []
        self.assets: list[dict] = []
        self.notes: list[dict] = []
        self.warnings: list[str] = []
        self.table_refs: dict[str, list[str]] = {}
        self._pid_seen: set[str] = set()

    # ---------------- 文本片段账本 ----------------
    def _safe_path(self, el) -> str:
        """lxml 的 getpath() 对未展开的实体节点会抛 MemoryError，回退到自算父级链路径。"""
        try:
            return self.tree.getpath(el)
        except (MemoryError, ValueError, TypeError, RuntimeError):
            parts: list[str] = []
            cur = el
            while cur is not None:
                parent = cur.getparent()
                if cur.tag is etree.Entity:
                    name = 'entity'
                elif cur.tag is etree.Comment:
                    name = 'comment'
                elif cur.tag is etree.PI:
                    name = 'processing-instruction'
                elif isinstance(cur.tag, str):
                    name = _local(cur)
                else:
                    name = 'node'
                if parent is None:
                    parts.append(name)
                    break
                idx = 0
                for sib in parent:
                    if sib is cur:
                        break
                    idx += 1
                parts.append(f'{name}[{idx}]')
                cur = parent
            return '/' + '/'.join(reversed(parts))

    def _key(self, el, kind: str) -> str:
        # lxml 元素代理对象不保证 id() 稳定，必须用 XPath 作为账本键
        return self._safe_path(el) + '/' + kind

    def _record(self, el, kind: str, path: str) -> None:
        raw = el.text if kind == 'text' else el.tail
        if raw and raw.strip():
            idx = len(self.fragments)
            self.fragments.append({'id': idx, 'xml_path': f'{path}/{kind}', 'text': _norm(raw)})
            self.frag_index[self._key(el, kind)] = idx

    def _walk(self, el, path: str) -> None:
        self._record(el, 'text', path)
        for i, child in enumerate(el):
            cpath = f'{path}/{_local(child)}[{i}]'
            if child.tag is etree.Entity:
                # 实体引用一律不展开：既防 XXE，也不把外部文件内容混入正文；留明确排除记录
                self.excluded.append({'xml_path': cpath,
                                      'reason': 'unresolved_entity_reference_not_expanded'})
                self._record(child, 'tail', cpath)
                continue
            self._walk(child, cpath)
            self._record(child, 'tail', cpath)

    def _consume(self, el, kind: str, unit_id: str) -> str:
        raw = el.text if kind == 'text' else el.tail
        idx = self.frag_index.get(self._key(el, kind))
        if idx is not None:
            self.consumed[idx] += 1
            self.unit_of_frag[idx] = unit_id
        return _norm_keep(raw)

    # ---------------- 渲染 ----------------
    def _render(self, el, unit_id: str) -> str:
        """渲染一个「非容器」节点的混合文本；注释/表格/资产分流，不重复消费。"""
        parts: list[str] = []
        t = self._consume(el, 'text', unit_id)
        if t:
            parts.append(t)
        for child in el:
            tag = _local(child)
            if child.tag is etree.Entity:
                self._consume(child, 'text', unit_id)
                tail = self._consume(child, 'tail', unit_id)
                if tail:
                    parts.append(tail)
                continue
            if tag in NOTE_TAGS:
                # 嵌入式注释也必须是独立规范化单元，否则只挂在 notes 索引里会被下游切片漏掉，
                # 且空文本父容器被过滤时注释会一并从 provisions.jsonl 消失。
                nid = self._unique_pid(f'{unit_id}/note:{len(self.notes) + 1}')
                note_text = self._render_note(child, nid)
                if note_text:
                    self.notes.append({'unit_id': unit_id, 'note_unit_id': nid,
                                       'role': tag, 'text': note_text})
                    self.units.append({
                        'provision_id': nid, 'kind': 'note', 'heading': '',
                        'hierarchy': [], 'text': note_text,
                        'note_parent': unit_id,
                        'source_locator': self._locator(child, nid),
                        'official_status_raw': child.get('status'),
                        'text_role': tag, 'commencement_status': 'unknown',
                        'warnings': [],
                    })
            elif tag in ASSET_TAGS:
                self._register_asset(child, unit_id)
                self._consume(child, 'text', unit_id)
                for sub in child.iterdescendants():
                    self._consume(sub, 'text', unit_id)
                    self._consume(sub, 'tail', unit_id)
            elif tag in TABLE_TAGS:
                tid = self._render_table(child, unit_id)
                self.table_refs.setdefault(unit_id, []).append(tid)
            else:
                sub = self._render(child, unit_id)
                if sub:
                    if tag in BLOCK_TAGS and parts and not ''.join(parts).endswith('\n'):
                        parts.append('\n')
                    parts.append(sub)
                    if tag in BLOCK_TAGS:
                        parts.append('\n')
            tail = self._consume(child, 'tail', unit_id)
            if tail:
                parts.append(tail)
        return _join(parts)

    def _render_note(self, el, unit_id: str) -> str:
        parts: list[str] = []
        t = self._consume(el, 'text', unit_id)
        if t:
            parts.append(t)
        for child in el:
            sub = self._render_note(child, unit_id)
            if sub:
                parts.append(sub)
            tail = self._consume(child, 'tail', unit_id)
            if tail:
                parts.append(tail)
        return _join(parts)

    def _render_table(self, el, parent_id: str) -> str:
        tid = self._unique_pid(f'table:{parent_id}:{len(self.tables) + 1}')
        rows: list[list[str]] = []
        self._consume(el, 'text', tid)
        for tr in el:
            lname = _local(tr)
            if lname in ('tr', 'row', 'tablerow', 'tbody', 'thead'):
                cells = []
                for td in tr:
                    cells.append(self._render(td, tid))
                    self._consume(td, 'tail', tid)
                if any(cells):
                    rows.append(cells)
            else:
                t = self._render(tr, tid)
                if t:
                    rows.append([t])
            self._consume(tr, 'tail', tid)
        text = '\n'.join(' | '.join(r) for r in rows)
        self.tables.append({'provision_id': tid, 'parent': parent_id,
                            'cells': rows, 'reading_order': 'row-major'})
        self.units.append({
            'provision_id': tid, 'kind': 'table', 'heading': '', 'hierarchy': [],
            'text': text, 'cells': rows,
            'source_locator': {'xml_id': el.get('id'), 'xml_path': self._safe_path(el),
                               'fragment_ids': [i for i, u in self.unit_of_frag.items() if u == tid]},
            'text_role': 'structured_table_transcription',
            'commencement_status': 'unknown', 'warnings': [],
        })
        return tid

    def _register_asset(self, el, unit_id: str) -> None:
        ref = ''
        for key, val in el.attrib.items():
            if key.lower().endswith(('href', 'src', 'file')):
                ref = val
                break
        rec: dict[str, Any] = {'unit_id': unit_id, 'reference': ref,
                               'xml_path': self._safe_path(el), 'renderable_by_text': False}
        if ref:
            candidates = [self.language_dir / ref,
                          self.language_dir / 'images' / Path(ref).name,
                          self.language_dir / Path(ref).name]
            cand = next((p for p in candidates if p.is_file() and
                         p.resolve().is_relative_to(self.language_dir.resolve())), None)
            if cand:
                rec.update({'resolved_path': str(cand.relative_to(self.language_dir)),
                            'sha256': sha256_file(cand), 'bytes': cand.stat().st_size,
                            'status': 'asset_available'})
            else:
                rec['status'] = 'asset_missing'
                self.warnings.append(f'图像引用未找到本地资产：{ref}')
        else:
            rec['status'] = 'asset_reference_missing'
            self.warnings.append('图像节点缺少可解析的引用属性')
        self.assets.append(rec)

    # ---------------- 单元 ----------------
    def _unique_pid(self, base: str) -> str:
        if base not in self._pid_seen:
            self._pid_seen.add(base)
            return base
        n = 2
        while f'{base}#{n}' in self._pid_seen:
            n += 1
        pid = f'{base}#{n}'
        self._pid_seen.add(pid)
        return pid

    def _meta(self) -> dict:
        out: dict[str, Any] = {}
        for el in self.root.iter():
            if _local(el) == 'meta':
                for c in el:
                    if (c.text or '').strip():
                        out[_local(c)] = (c.text or '').strip()
                break
        return out

    def _locator(self, el, pid: str) -> dict:
        return {'xml_id': el.get('id'), 'temporalId': el.get('temporalId'),
                'xml_path': self._safe_path(el), 'official_name': el.get('name'),
                'fragment_ids': [i for i, u in self.unit_of_frag.items() if u == pid]}

    def _add_unit(self, el, pid: str, kind: str, heading: str, hierarchy: list[str],
                  text: str | None = None, **extra) -> None:
        if text is None:
            text = self._render(el, pid)
        u = {'provision_id': pid, 'kind': kind, 'heading': heading,
             'hierarchy': list(hierarchy), 'text': text,
             'source_locator': self._locator(el, pid),
             'official_status_raw': el.get('status'),
             'commencement_status': 'unknown',
             'warnings': ['原件状态仅作证据保留，未独立核验条文时效'],
             'text_role': 'original'}
        u.update(extra)
        self.units.append(u)

    def _section_num(self, el) -> str:
        for c in el:
            if _local(c) == 'num':
                return (c.get('value') or _norm(''.join(c.itertext()))).strip('. ')
        name = (el.get('name') or '').strip()
        m = re.match(r's(\d+[A-Za-z]?)$', name)
        return m.group(1) if m else (name or 'unnumbered')

    def _heading_text(self, el) -> str:
        for c in el:
            if _local(c) == 'heading':
                return _norm(''.join(c.itertext()))
        return ''

    def _process_child(self, child, hierarchy: list[str], prefix: str,
                       parent: str | None) -> str:
        """处理容器下的单个子节点，返回用于消费该节点 tail 的 unit_id。"""
        tag = _local(child)
        if tag in NOTE_TAGS:
            pid = self._unique_pid(f'note:{len(self.notes) + 1}')
            text = self._render_note(child, pid)
            self._add_unit(child, pid, 'note', '', hierarchy, text=text,
                           text_role=tag, warnings=[])
            self.notes.append({'unit_id': pid, 'role': tag, 'text': text})
            return pid
        if tag == 'longtitle':
            pid = self._unique_pid('long_title:1')
            self._add_unit(child, pid, 'long_title', '', hierarchy)
            return pid
        if tag in CONTAINER_HEADINGS:
            # 容器标题单独成单元；内部 section/附表按各自身份递归，不吞并到标题里
            pid = self._unique_pid(f'heading:{tag}:{len(self.units) + 1}')
            self._consume(child, 'text', pid)
            title_parts = []
            for c in child:
                if _local(c) in TITLE_NODES:
                    title_parts.append(self._render(c, pid))
                    self._consume(c, 'tail', pid)
            title = ' '.join(x.strip() for x in title_parts if x.strip())
            self._add_unit(child, pid, 'heading', title, hierarchy, text=title,
                           source_locator=self._locator(child, pid))
            new_hier = hierarchy + [title] if title else hierarchy
            for c in child:
                if _local(c) in TITLE_NODES:
                    continue
                owner = self._process_child(c, new_hier, prefix, pid)
                self._consume(c, 'tail', owner)
            return pid
        if tag in LEAF_HEADINGS:
            pid = self._unique_pid(f'heading:{tag}:{len(self.units) + 1}')
            title = self._render(child, pid)
            self._add_unit(child, pid, 'heading', title, hierarchy, text=title)
            return pid
        if tag == 'section':
            num = self._section_num(child)
            pid = self._unique_pid(f'{prefix}section:{num}' if prefix else f'section:{num}')
            self._add_unit(child, pid, 'section', self._heading_text(child), hierarchy,
                           parent=parent)
            return pid
        if tag in ('schedule', 'appendix', 'annex', 'schedules'):
            sid = child.get('id') or f'{len(self.units) + 1}'
            spid = self._unique_pid(f'schedule:{sid}')
            title = self._heading_text(child)
            # 附表容器：标题节点单独消费，其余内容递归
            self._consume(child, 'text', spid)
            title_parts = []
            for c in child:
                if _local(c) in TITLE_NODES:
                    title_parts.append(self._render(c, spid))
                    self._consume(c, 'tail', spid)
            joined_title = ' '.join(x.strip() for x in title_parts if x.strip())
            title = title or joined_title
            self._add_unit(child, spid, 'schedule', title, hierarchy,
                           text=joined_title, source_locator=self._locator(child, spid))
            for c in child:
                if _local(c) in TITLE_NODES:
                    continue
                owner = self._process_child(c, hierarchy + [title or spid], f'{spid}/', spid)
                self._consume(c, 'tail', owner)
            return spid
        if tag in TABLE_TAGS:
            tid = self._render_table(child, parent or 'main')
            self.table_refs.setdefault(parent or 'main', []).append(tid)
            return tid
        if tag in ASSET_TAGS:
            pid = self._unique_pid(f'asset:{len(self.assets) + 1}')
            self._register_asset(child, pid)
            self._consume(child, 'text', pid)
            for sub in child.iterdescendants():
                self._consume(sub, 'text', pid)
                self._consume(sub, 'tail', pid)
            self._add_unit(child, pid, 'asset_reference', '', hierarchy, text='',
                           text_role='non_textual_asset',
                           warnings=['非文本内容，需按资产路径人工补充'])
            return pid
        # 其他有实质内容的节点一律保留
        if ''.join(child.itertext()).strip():
            pid = self._unique_pid(f'other:{tag}:{len(self.units) + 1}')
            self._add_unit(child, pid, 'other', '', hierarchy)
            return pid
        return parent or 'unassigned'

    def extract(self) -> dict:
        main = None
        for el in self.root.iter():
            if _local(el) == 'main':
                main = el
                break
        if main is None:
            raise RuntimeError('XML 缺少 main 节点')
        self._walk(main, 'main')
        meta = self._meta()
        self._consume(main, 'text', 'main')
        for child in main:
            owner = self._process_child(child, [], '', None)
            self._consume(child, 'tail', owner)
        self._consume(main, 'tail', 'main')

        notes_by_unit: dict[str, list[dict]] = {}
        for n in self.notes:
            notes_by_unit.setdefault(n['unit_id'], []).append(n)
        kept = []
        for u in self.units:
            refs = self.table_refs.get(u['provision_id'])
            if refs:
                u['table_refs'] = refs
            if u['provision_id'] in notes_by_unit:
                u['notes'] = notes_by_unit[u['provision_id']]
            if u['kind'] == 'other' and not u['text'].strip():
                # 仅含来源/编者注释的容器：正文为空，注释已在 notes 中按角色留痕
                continue
            kept.append(u)
        self.units = kept
        # 独立注释单元继承父单元的层级；父容器若因空正文被过滤，则保留空层级与 note_parent 溯源
        by_pid = {u['provision_id']: u for u in self.units}
        for u in self.units:
            parent = u.get('note_parent')
            if parent and parent in by_pid:
                u['hierarchy'] = list(by_pid[parent]['hierarchy'])
        missing = [f for f in self.fragments if self.consumed[f['id']] == 0]
        duplicated = [f for f in self.fragments if self.consumed[f['id']] > 1]
        coverage_ok = not missing and not duplicated
        if not coverage_ok:
            self.warnings.append(f'文本覆盖对账未通过：缺失 {len(missing)}、重复 {len(duplicated)}')
        return {
            'meta': meta,
            'units': self.units,
            'coverage': {
                'ok': coverage_ok,
                'total_text_fragments': len(self.fragments),
                'consumed_once': sum(1 for f in self.fragments if self.consumed[f['id']] == 1),
                'missing': missing[:40], 'duplicated': duplicated[:40],
                'excluded': self.excluded[:40], 'excluded_count': len(self.excluded),
                'excluded_reason': '仅空白文本片段、未展开实体引用（防 XXE）不入账本；两者均有明确记录',
            },
            'tables': self.tables, 'assets': self.assets, 'notes': self.notes,
            'warnings': self.warnings,
            'by_kind': dict(Counter(u['kind'] for u in self.units)),
        }
