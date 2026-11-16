# -*- coding: utf-8 -*-
"""结构优先的离线分片：预算含索引头，片段恒为原文连续子串且可无损还原。"""
from __future__ import annotations

import re
from typing import Any

from .common import sha256_text, stable_id
from .tokenizer import count_tokens


def _count(enc, header: str, s: str) -> int:
    return count_tokens(enc, header, s)


def _lines(s: str) -> list[str]:
    parts = s.splitlines(keepends=True)
    return parts or [s]


def _sentences(s: str) -> list[str]:
    """按句末标点切，标点与其后空白归入前段，保证拼接逐字还原。"""
    cuts = []
    for m in re.finditer(r'[。；！？]|[.;!?](?=\s|$)', s):
        e = m.end()
        while e < len(s) and s[e].isspace():
            e += 1
        if e < len(s):
            cuts.append(e)
    out, start = [], 0
    for e in cuts:
        if e > start:
            out.append(s[start:e])
            start = e
    if start < len(s):
        out.append(s[start:])
    return out or [s]


def _fit_prefix(enc, header: str, s: str, budget: int) -> int:
    """二分查找最大原文前缀，绝不 decode 局部 token 字节，Unicode 安全。"""
    lo, hi, best = 1, len(s), 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if _count(enc, header, s[:mid]) <= budget:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _split(enc, header: str, s: str, budget: int, level: int = 0) -> list[str]:
    if not s:
        return []
    if _count(enc, header, s) <= budget:
        return [s]
    if level >= 2:
        k = _fit_prefix(enc, header, s, budget)
        return [s[:k]] + _split(enc, header, s[k:], budget, 2)
    pieces = _lines(s) if level == 0 else _sentences(s)
    if len(pieces) <= 1:
        return _split(enc, header, s, budget, level + 1)
    out: list[str] = []
    buf = ''
    for piece in pieces:
        if buf and _count(enc, header, buf + piece) > budget:
            out.extend(_split(enc, header, buf, budget, level + 1))
            buf = piece
        else:
            buf += piece
    if buf:
        out.extend(_split(enc, header, buf, budget, level + 1))
    return [p for p in out if p]


def make_index_header(law: dict, language: str, provision: dict, enc, profile: dict) -> tuple[str, bool]:
    jur = law['jurisdiction'].upper()
    title = law['official_title_en'] if language == 'en' else law['official_title_zh']
    title = title or law.get('official_title_en') or law['code']
    hierarchy = [h for h in provision.get('hierarchy', []) if h]
    heading = (provision.get('heading') or '').strip()
    pid = provision['provision_id']

    def assemble(t: str, use_hier: bool, use_head: bool) -> str:
        bits = [jur, t]
        if use_hier and hierarchy:
            bits.append(' / '.join(hierarchy))
        if use_head and heading:
            bits.append(heading)
        bits.append(pid)
        return ' · '.join(bits)

    limit = int(profile.get('index_header_max_tokens', 120))
    header = assemble(title, True, True)
    shortened = False
    for use_hier, use_head in ((True, True), (True, False), (False, False)):
        header = assemble(title, use_hier, use_head)
        if _count(enc, header, '') <= limit:
            return header, shortened
        shortened = True
    t = title
    while t and _count(enc, assemble(t + '…', False, False), '') > limit:
        t = t[:-8]
    return assemble((t or title[:8]) + '…', False, False), True


def build_chunks(law: dict, edition: dict, provision: dict, enc, profile: dict) -> list[dict]:
    header, shortened = make_index_header(law, edition['language'], provision, enc, profile)
    budget = int(profile['max_tokens'])
    text = provision['text']
    parts = _split(enc, header, text, budget)
    if ''.join(parts) != text:
        raise RuntimeError(f"分片无法还原原文：{law['law_id']} {provision['provision_id']}")

    chunks: list[dict] = []
    offset = 0
    for seq, part in enumerate(parts, 1):
        start, end = offset, offset + len(part)
        offset = end
        content_sha = sha256_text(part)
        locator = dict(provision.get('source_locator') or {})
        locator['text_start'] = start
        locator['text_end'] = end
        chunk = {
            'chunk_id': stable_id('chk', {
                'edition_id': edition['edition_id'],
                'provision_id': provision['provision_id'],
                'profile_id': profile['profile_id'],
                'split_profile_sha256': profile['_split_profile_sha256'],
                'splitter_version': profile['splitter_version'],
                'sequence': seq,
                'content_sha256': content_sha,
            }),
            'law_id': law['law_id'],
            'edition_id': edition['edition_id'],
            'provision_id': provision['provision_id'],
            'jurisdiction': law['jurisdiction'],
            'language': edition['language'],
            'sequence': seq,
            'index_header': header,
            'index_header_shortened': shortened,
            'text': part,
            'content_sha256': content_sha,
            'source_id': edition['source_id'],
            'source_locator': locator,
            'token_count': _count(enc, header, part),
            'text_token_count': len(enc.encode(part, disallowed_special=())),
            'tokenizer_profile': profile['profile_id'],
            'segmentation_level': 'provision',
        }
        chunks.append(chunk)
    return chunks
