# -*- coding: utf-8 -*-
"""validate 子命令：重算每个语言版本的自动校验，不重跑解析、不联网。"""
from __future__ import annotations

from pathlib import Path

from .common import atomic_write_json, read_json, read_jsonl, append_issue
from .report import build_summary, write_report
from .runner import _validate_version
from .tokenizer import build_encoding, load_profile


def validate_all(output: Path, profile_path: Path | None = None) -> dict:
    profile = load_profile(profile_path)
    enc = build_encoding(profile)
    inventory = read_json(output / 'inventory.json', {}) or {}
    laws = {l['law_id']: l for l in inventory.get('laws', [])}
    updated = []
    for vdir in sorted(output.glob('*/*/*/*')):
        vp = vdir / 'validation.json'
        if not vp.is_file():
            continue
        old = read_json(vp, {})
        rep = read_json(vdir / 'extraction-report.json', {}) or {}
        law_id = old.get('law_id', '')
        law = laws.get(law_id) or {
            'law_id': law_id, 'code': law_id, 'jurisdiction': law_id.split(':')[0],
            'official_title_zh': '', 'official_title_en': '', 'official_url': '',
        }
        edition = {'edition_id': old.get('edition_id'), 'language': old.get('language'),
                   'source_id': None, 'extraction_status': old.get('extraction_status')}
        blocked = (old.get('extraction_status') == 'blocked' or
                   bool(rep.get('blocking_reasons')))
        reasons = list(rep.get('warnings') or old.get('blocking_reasons') or [])[:6]
        val = _validate_version(vdir, law, edition, profile, enc, blocked, reasons, rep)
        for k, ok in val['checks'].items():
            if ok is False:
                append_issue(output, {'level': 'validation', 'law_id': law_id,
                                      'language': edition['language'],
                                      'code': 'VALIDATION_FAILED',
                                      'message': f'检查项未通过：{k}'})
        updated.append({'law_id': law_id, 'edition_id': edition['edition_id'],
                        'status': val['extraction_status']})
    summary = build_summary(output, inventory, updated)
    write_report(output, summary, inventory, read_json(output / 'run-config.json'))
    return summary
