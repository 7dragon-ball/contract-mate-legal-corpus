# -*- coding: utf-8 -*-
"""首批预处理：针对真实风险的测试（附表丢失、目录误判、跨页、Unicode、摘要、续跑、token 预算）。"""
from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path

import pytest

from legal_corpus_tools.preprocessing.chunker import build_chunks
from legal_corpus_tools.preprocessing.common import (PROJECT_ROOT, canonical_json,
                                                     normalize_law_id, safe_relpath,
                                                     sha256_text)
from legal_corpus_tools.preprocessing.hk_xml import NOTE_TAGS, XmlExtractor
from legal_corpus_tools.preprocessing.inventory import build_inventory
from legal_corpus_tools.preprocessing.runner import (_archive_stale_versions,
                                                     process_version)
from legal_corpus_tools.preprocessing.sg_pdf import extract as extract_pdf
from legal_corpus_tools.preprocessing.tokenizer import (build_encoding, count_tokens,
                                                        load_profile)

SOURCE_ROOT = Path(os.environ.get('LEGAL_CORPUS_ROOT') or PROJECT_ROOT.parents[2] / '01-original')
BATCH_OUT = PROJECT_ROOT / 'build' / 'preprocess-first-batch-20261105'

needs_corpus = pytest.mark.skipif(not SOURCE_ROOT.is_dir(), reason='缺少只读输入语料目录')


@pytest.fixture(scope='module')
def env():
    profile = load_profile()
    return profile, build_encoding(profile)


def _find(jurisdiction: str, pattern: str) -> Path:
    hits = sorted((SOURCE_ROOT / jurisdiction).glob(f'**/{pattern}'))
    assert hits, f'未找到 {jurisdiction}/{pattern}'
    return hits[0]


def _law_edition(profile, law_id: str, language: str = 'en'):
    inv = json.loads((BATCH_OUT / 'inventory.json').read_text(encoding='utf-8'))
    law = next(l for l in inv['laws'] if l['law_id'] == law_id)
    ed = next(e for e in law['editions'] if e['language'] == language)
    return law, ed


# ---------------- 环境与路径 ----------------
def test_profile_locks_offline_resource(env):
    profile, enc = env
    assert profile['tokenizer']['offline_only'] is True
    assert profile['max_tokens'] == 1200
    assert len(enc.encode('Incoterms® 2020 — 第3條 售賣和售賣協議')) > 0
    for s in ['2.—(1) A contract of sale of goods.', '貨品售賣條例 § 3(1)', '😀 Unicode 边界']:
        assert enc.decode(enc.encode(s)) == s


def test_law_id_normalization():
    assert normalize_law_id('香港', 'Cap. 26') == 'hk:cap26'
    assert normalize_law_id('香港', 'Cap. 60G') == 'hk:cap60G'
    assert normalize_law_id('新加坡', 'SGCA2002-RG1') == 'sg:SGCA2002-RG1'


def test_safe_relpath_rejects_escape(tmp_path):
    (tmp_path / 'inside').mkdir()
    (tmp_path / 'inside' / 'a.xml').write_text('x', encoding='utf-8')
    assert safe_relpath(tmp_path, 'inside/a.xml').is_file()
    with pytest.raises(ValueError):
        safe_relpath(tmp_path, 'inside/../outside')
    with pytest.raises(ValueError):
        safe_relpath(tmp_path, '/etc/passwd')


# ---------------- 核验与选源 ----------------
@needs_corpus
def test_inventory_recomputes_scale(tmp_path):
    inv = build_inventory(SOURCE_ROOT, tmp_path)
    s = inv['actual_scale']
    assert s['concrete_laws'] == 142
    assert s['indexed_files'] == 369          # 三份 SSO 补下载 PDF 已全部归档
    assert s['source_verification_failed'] == 0
    assert s['language_versions'] >= 200
    assert s['blocked_versions'] == 0        # RIEA1995-RG1 官方原件补齐后不再阻断
    assert inv['scale_comparison'] == {'concrete_laws_matches_reference': True,
                                       'files_matches_reference': True}
    assert any(l['law_id'] == 'sg:SGCA2002-RG1' and
               e['selected_version']['source_batch'].startswith('SSO补充下载')
               for l in inv['laws'] for e in l['editions']
               if e['selected_version'] is not None)
    riea = next(l for l in inv['laws'] if l['law_id'] == 'sg:RIEA1995-RG1')
    ed = riea['editions'][0]
    assert ed['inventory_status'] == 'ready'
    assert ed['selected_version']['role'] == 'official_pdf_current'
    assert ed['selected_version']['source_sha256'] == (
        '9a4d3b880f1cb18b00eda40e881b46f8d1a44f585da943bf5d1813084468ad38')


def test_stale_blocked_version_archived_when_source_changes(tmp_path):
    """原件补齐后 edition_id 会变；旧 blocked 目录必须归档，passed 历史版本必须保留。"""
    law = {'law_id': 'sg:RIEA1995-RG1', 'jurisdiction': 'sg'}
    current = tmp_path / 'sg' / 'sg_RIEA1995-RG1' / 'en' / 'new_edition'
    current.mkdir(parents=True)
    parent = current.parent

    def mk(name, status, processing='completed'):
        d = parent / name
        d.mkdir(parents=True)
        (d / 'validation.json').write_text(json.dumps({
            'law_id': law['law_id'], 'language': 'en',
            'extraction_status': status, 'processing_status': processing,
        }), encoding='utf-8')
        return d

    old_blocked = mk('sg_RIEA1995-RG1_en_blocked', 'blocked')
    old_failed = mk('sg_RIEA1995-RG1_en_src-old', 'blocked', 'failed')
    old_passed = mk('sg_RIEA1995-RG1_en_src-aaa', 'passed')
    other_lang = parent.parent / 'zh-Hant' / 'sg_RIEA1995-RG1_zh-Hant_blocked'
    other_lang.mkdir(parents=True)
    (other_lang / 'validation.json').write_text(json.dumps({
        'law_id': law['law_id'], 'language': 'zh-Hant',
        'extraction_status': 'blocked', 'processing_status': 'completed',
    }), encoding='utf-8')

    archived = _archive_stale_versions(tmp_path, law, 'en', current)
    assert len(archived) == 2
    assert not old_blocked.exists() and not old_failed.exists()
    assert old_passed.exists(), '已 passed 的历史版本记录不得被移动'
    assert other_lang.exists(), '其他语言版本不受影响'
    dest_root = tmp_path.parent / (tmp_path.name + '.archived')
    assert (dest_root / old_blocked.name / 'validation.json').is_file()
    assert all(not p.startswith(str(tmp_path) + '/') for p in archived), \
        '归档必须在正式输出目录之外'
    assert _archive_stale_versions(tmp_path, law, 'en', current) == [], '归档应幂等'


def _mini_corpus(tmp_path: Path, wrong_sha: str | None = None) -> Path:
    root = tmp_path / 'corpus'
    d = root / '香港' / '主法' / 'Cap.1_测试' / '全集_英文'
    d.mkdir(parents=True)
    body = '<?xml version="1.0"?><ordinance><main><section id="s1"><num value="1">1.</num><heading>T</heading><content>Text</content></section></main></ordinance>'
    f = d / 'cap_1_en_c.xml'
    f.write_text(body, encoding='utf-8')
    sha = hashlib.sha256(f.read_bytes()).hexdigest()
    rel = str(f.relative_to(root))
    entry_cols = ['序号', '法域', '条目类型', '清单标识', '中文名称', '英文名称', '级别',
                  '业务分类', '官方链接', '本地状态', '已复制文件数', '源文件命中数', '目标目录']
    file_cols = ['序号', '法域', '条目类型', '清单标识', '中文名称', '目标相对路径',
                 '源绝对路径', '来源批次', '格式', '语言', '字节数', 'sha256', '重复来源', '备注']
    with open(root / '索引_清单条目.csv', 'w', encoding='utf-8-sig', newline='') as fh:
        w = csv.writer(fh); w.writerow(entry_cols)
        w.writerow([1, '香港', '主法', 'Cap. 1', '《測試》', 'Test', 1, '合同基础',
                    'https://example.invalid/cap1', '已复制', 1, 1, rel])
    with open(root / '索引_已复制文件.csv', 'w', encoding='utf-8-sig', newline='') as fh:
        w = csv.writer(fh); w.writerow(file_cols)
        w.writerow([1, '香港', '主法', 'Cap. 1', '《測試》', rel, str(f),
                    '两地现行法律全集', 'XML', '英文', f.stat().st_size,
                    wrong_sha or sha, '', ''])
    return root


def test_inventory_hash_mismatch_blocks_version(tmp_path):
    out = tmp_path / 'out'; out.mkdir()
    good = build_inventory(_mini_corpus(tmp_path / 'good'), out)
    assert good['actual_scale']['source_verification_failed'] == 0
    root2 = tmp_path / 'bad'
    root2.mkdir()
    bad_root = _mini_corpus(root2, wrong_sha='0' * 64)
    out2 = tmp_path / 'out2'; out2.mkdir()
    bad = build_inventory(bad_root, out2)
    assert bad['actual_scale']['source_verification_failed'] == 1
    assert bad['laws'][0]['editions'][0]['inventory_status'] == 'blocked'
    assert '摘要' in ''.join(bad['laws'][0]['editions'][0]['block_reasons']) or \
           '核验' in ''.join(bad['laws'][0]['editions'][0]['block_reasons'])


# ---------------- 香港 XML ----------------
@needs_corpus
def test_hk_xml_coverage_and_note_roles():
    p = _find('香港', 'cap_26_20180920000000_en_c.xml')
    r = XmlExtractor(p, p.parent).extract()
    assert r['coverage']['ok'] is True
    assert r['coverage']['total_text_fragments'] > 500
    assert r['coverage']['missing'] == [] and r['coverage']['duplicated'] == []
    assert any(u['provision_id'] == 'section:3' for u in r['units'])
    assert r['notes'] and all(n['role'] in ('sourcenote', 'referencenote',
                                            'commencementnote', 'editorialnote', 'note')
                              for n in r['notes'])
    s3 = next(u for u in r['units'] if u['provision_id'] == 'section:3')
    assert 'Sale of goods' in s3['text'] or 'sale' in s3['text'].lower()
    assert s3['source_locator']['xml_id']


@needs_corpus
def test_hk_xml_keeps_long_schedules_and_tables():
    p = _find('香港', 'cap_60G_*_en_c.xml')
    r = XmlExtractor(p, p.parent).extract()
    assert r['coverage']['ok'] is True
    assert len(r['units']) > 900, '长附表/条文不得被截断'
    assert len(r['tables']) > 0


@needs_corpus
def test_hk_xml_no_glued_words_and_original_chars_preserved():
    """独立用原件 main.itertext() 对账，不用解析器自己的覆盖账本自证。"""
    import re
    from collections import Counter

    from lxml import etree

    def no_ws(s):
        return re.sub(r'\s+', '', s or '')

    p = _find('香港', 'cap_26_20180920000000_en_c.xml')
    r = XmlExtractor(p, p.parent).extract()
    s1 = next(u for u in r['units'] if u['provision_id'] == 'section:1')
    assert 'as the Sale of Goods Ordinance' in s1['text'], '相邻文本片段不得无间隔拼接'
    assert 'as theSale' not in s1['text']
    tree = etree.parse(str(p), etree.XMLParser(resolve_entities=False, no_network=True,
                                               load_dtd=False, huge_tree=True))
    main = next(e for e in tree.getroot().iter()
                if isinstance(e.tag, str) and etree.QName(e).localname.lower() == 'main')
    raw = Counter(no_ws(''.join(main.itertext())))
    got = Counter(no_ws(''.join(u['text'] for u in r['units'])))
    assert sum((raw - got).values()) == 0, '原文字符不得被吞掉'
    assert set((got - raw).keys()) <= {'|'}, '额外字符只允许表格结构化分隔符'


@needs_corpus
def test_hk_xml_embedded_notes_are_chunkable_units():
    """来源注释不能只留在 notes 索引里；空文本父容器被过滤时尤其容易整段丢失。"""
    import re
    p = _find('香港', 'cap_60G_*_en_c.xml')
    r = XmlExtractor(p, p.parent).extract()
    notes = [u for u in r['units'] if u['kind'] == 'note']
    assert len(notes) > 100
    assert all(u['text_role'] in NOTE_TAGS for u in notes)
    # 顶层注释无 note_parent，嵌入式注释必须有，后者才是本次修复的对象
    embedded = [u for u in notes if u.get('note_parent')]
    assert len(embedded) > 100
    joined = ''.join(u['text'] for u in notes)
    assert 'L.N. 247 of 1997' in joined
    assert 'Schedule 1 replaced' in joined, '注释内片段须保留原文空白'
    assert r['coverage']['ok'] is True


@needs_corpus
def test_hk_xml_cisg_schedule_not_dropped():
    p = _find('香港', 'cap_641_*_en_c.xml')
    r = XmlExtractor(p, p.parent).extract()
    assert r['coverage']['ok'] is True
    assert any(u['kind'] == 'schedule' for u in r['units'])
    assert 'United Nations Convention' in ' '.join(u['text'] for u in r['units'])


def test_hk_xml_disables_external_entities(tmp_path):
    p = tmp_path / 'evil.xml'
    p.write_text('<?xml version="1.0"?><!DOCTYPE ordinance [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
                 '<ordinance><main><section id="s1"><num>1.</num><content>&x;</content></section></main></ordinance>',
                 encoding='utf-8')
    r = XmlExtractor(p, tmp_path).extract()
    texts = ' '.join(u['text'] for u in r['units'])
    assert 'root:' not in texts and '/bin/bash' not in texts  # 外部实体未被展开
    assert r['coverage']['ok'] is True, '实体引用须有明确排除记录，不能让覆盖账本崩溃'
    assert any(e['reason'] == 'unresolved_entity_reference_not_expanded'
               for e in r['coverage']['excluded'])


# ---------------- 新加坡 PDF ----------------
@needs_corpus
def test_sg_pdf_toc_not_second_body():
    r = extract_pdf(_find('新加坡', 'SGA1979.pdf'))
    assert r['blocked'] is False
    assert r['report']['toc_pages'], '目录页应被识别'
    assert r['report']['section_count'] >= 60
    kinds = [u['kind'] for u in r['units']]
    assert kinds.count('preface') == 1
    sections = [u for u in r['units'] if u['kind'] == 'section']
    for u in sections:
        assert len(u['text']) >= 20
        # 附表内条号按契约带父级路径（schedule:N/section:M），不算第二套正文
        assert u['provision_id'].startswith('section:') or '/section:' in u['provision_id']
    # 已废止／删除的短条文仍须保留，不能被「短文本即噪声」规则吞掉
    assert any('[Deleted' in u['text'] or '[Repealed' in u['text'] for u in sections)
    # 目录不得产生重复条号；若被误判为正文，下面唯一性检查会失败
    assert len({u['provision_id'] for u in sections}) == len(sections)
    assert len([u for u in sections if u['provision_id'].startswith('section:')]) >= 60


@needs_corpus
def test_sg_pdf_cross_page_and_tail():
    r = extract_pdf(_find('新加坡', 'SGA1979.pdf'))
    cross = [u for u in r['units'] if len(u['source_locator']['pages']) > 1]
    assert cross, '应有跨页条文被合并'
    for u in cross:
        assert all(s['start'] <= s['end'] for s in u['source_locator']['page_spans'])
    assert r['report']['body_end_line']['page'] == r['report']['total_pages']
    assert r['units'][-1]['kind'] in ('supplement', 'schedule', 'section')


@needs_corpus
def test_sg_pdf_two_page_supplement_download():
    r = extract_pdf(_find('新加坡', 'SGCA2002-S534-2019.pdf'))
    assert r['blocked'] is False
    assert r['report']['total_pages'] == 2
    assert r['report']['section_count'] == 4
    assert r['versions']['source_as_of'] == '05 Nov 2026'
    assert r['versions']['pdf_created_date'] == '05 Nov 2026'
    assert r['versions']['effective_from'] is None  # 现行版本日不等于条文生效日


@needs_corpus
def test_sg_pdf_table_header_not_treated_as_running_header():
    """末页 COMPARATIVE TABLE 是带边框两栏表；表头 2020 Ed. 不得被页眉清洗误删。"""
    r = extract_pdf(_find('新加坡', 'SGA1979.pdf'))
    assert r['blocked'] is False
    rep = r['report']
    assert 46 in rep['table_candidate_pages'], '矢量边框表格须被单独检测'
    assert rep['table_structure_note'], '表格行列关系未恢复须明确留痕，不得宣称完整'
    last = r['raw_pages'][-1]
    assert last['table_grid'] is True
    assert '2020 Ed.' in last['cleaned_text']
    assert '2020 Ed.' not in last['removed_header_footer']
    assert '2020 Ed.' in ' '.join(u['text'] for u in r['units'])


@needs_corpus
def test_sg_pdf_schedule_reference_does_not_hijack_body_sections():
    """RIEA1995-RG1：正文里的「First Schedule; or」只是引用，不是附表标题。

    不得把第 7–19 条挂到 schedule:N/section:M 之下；「7.  Where —」、
    「35A.  In this Part —」这类短破折号条文也不得被长度阈值吞掉。
    """
    r = extract_pdf(_find('新加坡', 'RIEA1995-RG1.pdf'))
    assert r['blocked'] is False
    ids = [u['provision_id'] for u in r['units']]
    for n in ['7', '8', '9', '10', '13', '14', '19', '35A', '35B', '46']:
        assert f'section:{n}' in ids, f'正文条 {n} 应为顶层 section'
        assert not any(x.endswith(f'/section:{n}') for x in ids), \
            f'正文条 {n} 被错误挂到附表路径下'
    schedules = [x for x in ids if x.startswith('schedule:') and '/section:' not in x]
    assert len(schedules) == 8, '正文引用不得产生额外附表标题'
    u7 = next(u for u in r['units'] if u['provision_id'] == 'section:7')
    assert u7['text'].startswith('7.  Where —') and '(a)' in u7['text']
    u35a = next(u for u in r['units'] if u['provision_id'] == 'section:35A')
    assert 'declarant' in u35a['text']
    assert any(u['kind'] == 'supplement' for u in r['units'])


@needs_corpus
def test_sg_pdf_short_comparative_table_tail_is_not_a_scanned_page():
    """尾页只有几十个字符 + 表格矢量线：登记短页即可，不得阻断整部法律。"""
    r = extract_pdf(_find('新加坡', 'CA2004.pdf'))
    assert r['blocked'] is False
    rep = r['report']
    assert rep['suspect_pages'] == [], '对照表尾页有可读文字，不是扫描页'
    assert rep['total_pages'] in rep['short_pages']
    assert rep['section_count'] >= 100
    assert r['units'] and any(u['kind'] == 'supplement' for u in r['units'])


@needs_corpus
def test_sg_pdf_high_multi_column_ratio_still_parses_normal_body():
    """右侧修订注记/两栏对照表会命中多栏候选，但正文单栏且条号可识别，不应降级。"""
    r = extract_pdf(_find('新加坡', 'ESIEA2006.pdf'))
    assert r['blocked'] is False
    assert len(r['report']['multi_column_pages']) > 20
    assert r['report']['section_count'] >= 30
    assert r['report']['layout_blocking_policy']


@needs_corpus
def test_sg_pdf_30_page_supplement_download():
    r = extract_pdf(_find('新加坡', 'SGCA2002-RG1.pdf'))
    assert r['blocked'] is False
    assert r['report']['total_pages'] == 30
    assert r['report']['section_count'] >= 20


# ---------------- 分片与稳定性 ----------------
def test_chunker_unicode_reconstruction_and_budget(env):
    profile, enc = env
    law = {'law_id': 'hk:test', 'jurisdiction': 'hk', 'code': 'Cap. 999',
           'official_title_zh': '《測試條例》', 'official_title_en': 'Test Ordinance'}
    edition = {'edition_id': 'hk:test:zh-Hant:src-abc', 'language': 'zh-Hant',
               'source_id': 'hk:test:zh-Hant:abc'}
    text = ('（1）本條例測試 Unicode 分片與還原：貨品售賣、交付、風險轉移。😀 '
            'Incoterms® 2020 — CIF；第 3 條。' * 160)
    prov = {'provision_id': 'section:3', 'kind': 'section', 'heading': '測試條文',
            'hierarchy': ['第I部 總則'], 'text': text,
            'source_locator': {'xml_id': 'ID_TEST'}}
    chunks = build_chunks(law, edition, prov, enc, profile)
    assert len(chunks) > 1, '超长条文应切分'
    assert ''.join(c['text'] for c in chunks) == text
    assert all(text.find(c['text']) >= 0 for c in chunks)
    assert [c['sequence'] for c in chunks] == list(range(1, len(chunks) + 1))
    assert all(c['token_count'] == count_tokens(enc, c['index_header'], c['text'])
               for c in chunks)
    assert all(c['token_count'] <= profile['max_tokens'] for c in chunks)
    assert all(c['content_sha256'] == hashlib.sha256(c['text'].encode()).hexdigest()
               for c in chunks)
    assert build_chunks(law, edition, prov, enc, profile)[0]['chunk_id'] == chunks[0]['chunk_id']


def test_chunk_id_independent_of_parser_version(env):
    """解析器版本升级触发重做，但不得改写只绑定切片配置的内容 ID。"""
    profile, enc = env
    law = {'law_id': 'hk:test', 'jurisdiction': 'hk', 'code': 'Cap. 999',
           'official_title_zh': '《測試條例》', 'official_title_en': 'Test Ordinance'}
    edition = {'edition_id': 'hk:test:zh-Hant:src-abc', 'language': 'zh-Hant',
               'source_id': 'hk:test:zh-Hant:abc'}
    prov = {'provision_id': 'section:3', 'kind': 'section', 'heading': '測試條文',
            'hierarchy': ['第I部 總則'], 'text': '（1）貨品售賣、交付與風險轉移。' * 200,
            'source_locator': {'xml_id': 'ID_TEST'}}
    before = build_chunks(law, edition, prov, enc, profile)
    changed = dict(profile)
    changed['parser_version'] = 'preprocess-parser-9.9.9'
    changed['_profile_sha256'] = sha256_text(canonical_json(
        {k: v for k, v in changed.items() if not k.startswith('_')}))
    after = build_chunks(law, edition, prov, enc, changed)
    assert [c['chunk_id'] for c in before] == [c['chunk_id'] for c in after]


@needs_corpus
def test_resume_skips_completed_version(env, tmp_path):
    profile, enc = env
    out = tmp_path / 'out'; out.mkdir()
    inv = build_inventory(SOURCE_ROOT, out)
    law = next(l for l in inv['laws'] if l['law_id'] == 'hk:cap26')
    ed = next(e for e in law['editions'] if e['language'] == 'en')
    first = process_version(SOURCE_ROOT, out, law, ed, profile, enc, resume=False)
    assert first['status'] == 'completed'
    second = process_version(SOURCE_ROOT, out, law, ed, profile, enc, resume=True)
    assert second['status'] == 'skipped_resume'


@needs_corpus
def test_rerun_version_content_is_stable(env, tmp_path):
    """同输入、解析器、profile 强制重跑两次：正文/片数/内容 ID/token/摘要逐项一致。"""
    profile, enc = env
    out = tmp_path / 'out'; out.mkdir()
    inv = build_inventory(SOURCE_ROOT, out)
    law = next(l for l in inv['laws'] if l['law_id'] == 'hk:cap26')
    ed = next(e for e in law['editions'] if e['language'] == 'en')

    def snapshot():
        rec = process_version(SOURCE_ROOT, out, law, ed, profile, enc, resume=False)
        assert rec['status'] == 'completed'
        vd = out / rec['version_dir']
        rows = lambda name: [json.loads(x) for x in
                             (vd / name).read_text(encoding='utf-8').splitlines() if x]
        provs, chunks = rows('provisions.jsonl'), rows('chunks.jsonl')
        return {
            'provision_ids': [p['provision_id'] for p in provs],
            'provision_texts': [p['text'] for p in provs],
            'chunk_ids': [c['chunk_id'] for c in chunks],
            'chunk_texts': [c['text'] for c in chunks],
            'token_counts': [c['token_count'] for c in chunks],
            'content_sha256': [c['content_sha256'] for c in chunks],
            'checksums': json.loads((vd / 'checksums.json').read_text(encoding='utf-8')),
        }

    first, second = snapshot(), snapshot()
    for key in first:
        assert first[key] == second[key], f'重跑不稳定：{key}'
    assert first['chunk_ids'] and len(set(first['chunk_ids'])) == len(first['chunk_ids'])
    assert first['checksums']


@needs_corpus
def test_trial_run_outputs_stable_ids_and_budget(env):
    profile, enc = env
    if not (BATCH_OUT / 'summary.json').is_file():
        pytest.skip('尚未执行代表性试跑')
    checked = 0
    for vp in sorted(BATCH_OUT.glob('*/*/*/*/validation.json')):
        vd = vp.parent
        chunks = [json.loads(x) for x in (vd / 'chunks.jsonl').read_text(encoding='utf-8').splitlines() if x]
        provisions = {json.loads(x)['provision_id']: json.loads(x)
                      for x in (vd / 'provisions.jsonl').read_text(encoding='utf-8').splitlines() if x}
        for c in chunks:
            assert c['token_count'] == count_tokens(enc, c['index_header'], c['text'])
            assert c['token_count'] <= profile['max_tokens']
            assert c['provision_id'] in provisions
            checked += 1
    assert checked > 100
