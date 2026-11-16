# -*- coding: utf-8 -*-
"""汇总统计与可读报告；技术通过、法律时效与发布资格分开陈述。"""
from __future__ import annotations

import csv
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .common import atomic_write_json, atomic_write_text, read_json, read_jsonl


def build_summary(output: Path, inventory: dict, results: list[dict] | None = None) -> dict:
    laws = inventory.get('laws', [])
    versions = []
    token_values: list[int] = []
    over_budget = 0
    pages_total = 0
    provisions_total = 0
    chunks_total = 0
    issues = read_jsonl(output / 'issues.jsonl') if (output / 'issues.jsonl').exists() else []
    anomalies: list[dict] = []
    for vd in sorted(output.glob('*/*/*/*')):
        vp = vd / 'validation.json'
        if not vp.is_file():
            continue
        val = read_json(vp, {})
        rep = read_json(vd / 'extraction-report.json', {})
        sources = read_json(vd / 'sources.json', []) or []
        source_file = sources[0].get('source_file', '') if sources else ''
        for w in (rep.get('warnings') or []):
            if '多栏版面候选页' in w:
                code = 'MULTI_COLUMN'
            elif '表格候选页' in w:
                code = 'TABLE_STRUCTURE_NOT_RESTORED'
            elif '含图像页' in w:
                code = 'IMAGE_CONTENT_NOT_COVERED'
            elif '可疑页' in w:
                code = 'SUSPECT_PAGE'
            else:
                code = 'EXTRACTION_WARNING'
            nums = re.findall(r'\[([0-9, ]+)\]', w)
            anomalies.append({
                'law_id': val.get('law_id'), 'language': val.get('language'),
                'edition_id': val.get('edition_id'), 'code': code,
                'source_file': source_file, 'pages': ';'.join(nums[:2]),
                'version_dir': str(vd.relative_to(output)), 'message': w,
                'extraction_status': val.get('extraction_status'),
            })
        pages_total += int(rep.get('total_pages') or 0)
        provisions_total += int(val.get('counts', {}).get('provisions') or 0)
        chunks_total += int(val.get('counts', {}).get('chunks') or 0)
        for c in read_jsonl(vd / 'chunks.jsonl'):
            t = c.get('token_count')
            if isinstance(t, int):
                token_values.append(t)
                if t > int(val.get('counts', {}).get('token_budget') or 1200):
                    over_budget += 1
        versions.append({
            'law_id': val.get('law_id'), 'edition_id': val.get('edition_id'),
            'language': val.get('language'),
            'extraction_status': val.get('extraction_status'),
            'processing_status': val.get('processing_status'),
            'release_eligible': val.get('release_eligible', False),
            'provisions': val.get('counts', {}).get('provisions', 0),
            'chunks': val.get('counts', {}).get('chunks', 0),
            'total_pages': rep.get('total_pages'),
            'source_file': source_file,
            'version_dir': str(vd.relative_to(output)),
            'blocking_reasons': val.get('blocking_reasons', []),
            'warnings': val.get('warnings', []),
        })
    by_jur: dict[str, dict] = defaultdict(lambda: Counter())
    for v in versions:
        j = (v['law_id'] or '').split(':')[0]
        by_jur[j]['versions'] += 1
        by_jur[j][v['extraction_status'] or 'unknown'] += 1
        by_jur[j]['provisions'] += v['provisions'] or 0
        by_jur[j]['chunks'] += v['chunks'] or 0
    token_stats = {}
    if token_values:
        s = sorted(token_values)
        token_stats = {
            'count': len(s), 'min': s[0], 'max': s[-1],
            'mean': round(statistics.mean(s), 1),
            'median': s[len(s) // 2],
            'p95': s[min(len(s) - 1, int(len(s) * 0.95))],
            'over_budget': over_budget,
        }
    summary = {
        'schema_version': '1',
        'scope': {
            'concrete_laws': len(laws),
            'language_versions_in_inventory': sum(len(l['editions']) for l in laws),
            'language_versions_with_results': len(versions),
            'indexed_files': inventory.get('actual_scale', {}).get('indexed_files'),
            'source_verification_failed': inventory.get('actual_scale', {}).get('source_verification_failed'),
        },
        'by_jurisdiction': {k: dict(v) for k, v in by_jur.items()},
        'totals': {
            'provisions': provisions_total, 'chunks': chunks_total,
            'pdf_pages_processed': pages_total,
            'passed_versions': sum(1 for v in versions if v['extraction_status'] == 'passed'),
            'blocked_versions': sum(1 for v in versions if v['extraction_status'] == 'blocked'),
            'failed_versions': sum(1 for v in versions if v['processing_status'] == 'failed'),
            'release_eligible_versions': sum(1 for v in versions if v['release_eligible']),
            'issues': len(issues),
        },
        'token_distribution': token_stats,
        'anomaly_count': len(anomalies),
        'anomalies_by_code': dict(Counter(a['code'] for a in anomalies)),
        'anomalies': anomalies,
        'model_calls': 0, 'network_calls': 0,
        'package_generated': False,
        'package_note': '本次为前置离线处理，未生成正式导入包；发布契约、来源时效与下游 schema 尚未确认',
        'versions': versions,
    }
    _write_version_lists(output, summary, laws, anomalies)
    atomic_write_json(output / 'summary.json', summary)
    return summary


def _csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    with open(path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)


def _write_version_lists(output: Path, summary: dict, laws: list[dict],
                         anomalies: list[dict]) -> None:
    codes = {l['law_id']: l.get('code', '') for l in laws}
    ok_fields = ['law_id', 'code', 'language', 'edition_id', 'provisions', 'chunks',
                 'total_pages', 'source_file', 'version_dir']
    passed = [{**v, 'code': codes.get(v['law_id'], '')} for v in summary['versions']
              if v['extraction_status'] == 'passed']
    blocked = [{**v, 'code': codes.get(v['law_id'], ''),
                'reasons': '；'.join(v['blocking_reasons'] or [])[:500]}
               for v in summary['versions'] if v['extraction_status'] != 'passed']
    _csv(output / '成功语言版本.csv', ok_fields, passed)
    _csv(output / '阻断语言版本.csv',
         ['law_id', 'code', 'language', 'edition_id', 'reasons', 'source_file', 'version_dir'],
         blocked)
    _csv(output / '异常明细.csv',
         ['law_id', 'language', 'edition_id', 'code', 'pages', 'source_file',
          'version_dir', 'message', 'extraction_status'], anomalies)


def write_report(output: Path, summary: dict, inventory: dict,
                 run_config: dict | None = None) -> None:
    laws = {l['law_id']: l for l in inventory.get('laws', [])}
    t = summary['totals']
    L = ['# 首批香港、新加坡法律文本离线预处理报告', '',
         f"输出目录：`{output}`", '',
         '> 本轮只完成离线前置处理。技术处理完成不等于法律时效已核验；'
         '未调用模型、未访问网络、未生成正式导入包。', '',
         '## 1. 范围与原件核验', '',
         f"- 清单具体法例标识：**{summary['scope']['concrete_laws']}**",
         f"- 语言版本（inventory）：**{summary['scope']['language_versions_in_inventory']}**；"
         f"已产出结果：**{summary['scope']['language_versions_with_results']}**",
         f"- 文件索引条目：{summary['scope']['indexed_files']}；"
         f"摘要/路径核验失败：{summary['scope']['source_verification_failed']}",
         f"- 参考规模对照（README）：{inventory.get('reference_scale', {}).get('concrete_laws', 142)} 项 / "
         f"{inventory.get('reference_scale', {}).get('copied_files', 369)} 文件；"
         f"实际按索引重算为 {summary['scope']['concrete_laws']} 项 / "
         f"{summary['scope']['indexed_files']} 文件", '',
         '## 2. 处理统计', '', '| 指标 | 数值 |', '|---|---|',
         f"| 结构单元 provisions | {t['provisions']} |",
         f"| 入库切片 chunks | {t['chunks']} |",
         f"| 已处理 PDF 页数 | {t['pdf_pages_processed']} |",
         f"| extraction_status = passed | {t['passed_versions']} |",
         f"| extraction_status = blocked | {t['blocked_versions']} |",
         f"| 处理失败版本 | {t['failed_versions']} |",
         f"| release_eligible = true | {t['release_eligible_versions']} |",
         f"| issues 记录 | {t['issues']} |", '']
    for j, c in sorted(summary['by_jurisdiction'].items()):
        L.append(f"- **{j.upper()}**：版本 {c.get('versions', 0)}，"
                 f"passed {c.get('passed', 0)}，blocked {c.get('blocked', 0)}，"
                 f"provisions {c.get('provisions', 0)}，chunks {c.get('chunks', 0)}")
    tk = summary.get('token_distribution') or {}
    if tk:
        L += ['', '### token 分布（检索文本：索引头 + 换行 + 正文）', '',
              f"样本 {tk['count']}，最小 {tk['min']}，中位 {tk['median']}，"
              f"均值 {tk['mean']}，P95 {tk['p95']}，最大 {tk['max']}，"
              f"超预算 {tk['over_budget']}。"]
    L += ['', '## 3. 阻断与异常的语言版本', '',
          '| 法域 | 法律 | 语言 | 状态 | 原因 |', '|---|---|---|---|---|']
    blocked_rows = [v for v in summary['versions'] if v['extraction_status'] == 'blocked']
    if blocked_rows:
        for v in blocked_rows[:120]:
            law = laws.get(v['law_id'], {})
            reasons = '；'.join((v['blocking_reasons'] or ['未记录'])[:2])[:160]
            L.append(f"| {v['law_id'].split(':')[0]} | {law.get('code', v['law_id'])} | "
                     f"{v['language']} | blocked | {reasons} |")
    else:
        L.append('| — | — | — | — | 无阻断版本 |')
    if len(blocked_rows) > 120:
        L.append(f'\n（仅列出前 120 条，共 {len(blocked_rows)} 条；完整明细见 summary.json / issues.jsonl）')
    anomalies = summary.get('anomalies', [])
    L += ['', '### 版面／资产提示（非阻断，需人工补充）', '',
          f"合计 **{len(anomalies)}** 条，按类型："
          f"`{summary.get('anomalies_by_code', {})}`。",
          '这些提示来自各版本 `extraction-report.json`，已定位到法例、语言、原件与页码；'
          '`issues.jsonl` 只记录阻断／失败事件，两者统计口径不同。',
          '', '| 类型 | 法例 | 语言 | 页码线索 | 说明 |', '|---|---|---|---|---|']
    if anomalies:
        for a in anomalies[:40]:
            msg = a['message'].replace('|', '\\|')[:150]
            L.append(f"| `{a['code']}` | `{a['law_id']}` | {a['language']} | "
                     f"{a['pages'][:40]} | {msg} |")
        if len(anomalies) > 40:
            L.append(f'\n（仅列出前 40 条，共 {len(anomalies)} 条；完整明细见 `异常明细.csv`）')
    else:
        L.append('| — | — | — | — | 无版面／资产提示 |')
    L += ['', '成功／阻断／异常的可筛选清单：'
          '`成功语言版本.csv`、`阻断语言版本.csv`、`异常明细.csv`。', '',
          '## 4. 状态维度说明', '',
          '- `processing_status`：技术处理是否结束；`completed` 不代表解析正确。',
          '- `extraction_status`：`passed` 仅表示本次自动覆盖/结构/摘要检查通过；'
          '`blocked` 表示全文或结构覆盖无法确认。',
          '- `source_verification`：本批统一 `pending`（未联网核对官方现行整合版本）。',
          '- `legal_status`：本批统一 `unknown`（未把原件 docStatus / PDF 页眉当作条文现行生效）。',
          '- `release_eligible`：全部 `false`。来源时效、下游导入 schema 与正式包契约尚未核验，'
          '本次也不含打包上传。', '',
          '## 5. 未展开与后续动作', '']
    for r in inventory.get('excluded_scope', []):
        L.append(f"- `{r['code']}`（{r['entry_type']}）：{r['status']}；未自动扩张本批范围。")
    L += ['- 图像、附表图片等资产已按路径和 SHA256 登记；非文本内容不由纯文本覆盖，需人工补充。',
          '- 三份 SSO 补下载 PDF 的 `Current version as at 05 Nov 2026` 已记为 `source_as_of`，'
          '不等于修法或条文生效日；其中 `RIEA1995-RG1` 的官方原件补齐后，本批已无解析阻断，'
          '但 `release_eligible` 仍全部为 false。',
          '- 后续：核对来源时效 → 确认下游导入契约 → 更换与最终 embedding 模型匹配的 tokenizer/profile '
          '并重做切片 → 按法域打包（不在本轮执行）。', '',
          '## 6. 运行与调用', '',
          f"- 模型 API 调用：**{summary['model_calls']}**；正式处理网络访问：**{summary['network_calls']}**。",
          '- 正式导入包：**未生成**（不是示例包，也不以本目录冒充发布制品）。']
    if run_config:
        L += [f"- 运行命令：`{run_config.get('command', '')}`",
              f"- 恢复命令：`{run_config.get('resume_command', '')}`",
              f"- 停止方式：{run_config.get('stop_command', '')}"]
    L += ['', '## 7. 交付边界与结论', '']
    if t['blocked_versions'] or t['failed_versions']:
        L.append(f"> **本批仍有 {t['blocked_versions']} 个阻断版本、"
                 f"{t['failed_versions']} 个失败版本，不是「首批全部成功」。** "
                 '阻断明细见 §3 与 `阻断语言版本.csv`；需补齐已核验官方原件后单独重跑，'
                 '不得从首批名单静默删除后宣称整批完成。')
    else:
        L.append('> 本批无阻断版本；但来源时效核验、发布契约与下游 schema 仍未完成，'
                 '不得据此宣称可发布。')
    L += ['', f"- 成功语言版本：**{t['passed_versions']}**（清单 `成功语言版本.csv`）",
          '- 代表性原件对照：`对照/代表性原件对照.md`、`对照/可视核验结论.md`、`对照/audit.json`。',
          '- 补齐版本核验：`对照/补齐版本_RIEA1995-RG1核验.md`、`对照/补充版本_RIEA1995-RG1/`。',
          '- 重跑稳定性：`对照/重跑稳定性报告.md`；测试命令见 README §7.4。',
          '- PID、开始时间、profile 摘要、恢复与停止方式：`run-meta.json`、`run-config.json`。',
          '- 已知限制：带边框表格行列关系未结构化恢复、SSO 条目标题归属不够精确、',
          '  图像内容不由纯文本覆盖；详见 `对照/可视核验结论.md` §5 与 `异常明细.csv`。']
    L += ['', '完整机器可读结果：`summary.json`、`inventory.json`、`progress.jsonl`、'
          '`issues.jsonl`，以及各语言版本目录下的 `validation.json` / `extraction-report.json`。', '']
    atomic_write_text(output / 'report.md', '\n'.join(L))
