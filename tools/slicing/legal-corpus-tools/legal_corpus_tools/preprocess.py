# -*- coding: utf-8 -*-
"""统一 CLI：inventory / run / validate / compare / stop。

用法（工程根目录）：
  .venv/bin/python -m legal_corpus_tools.preprocess inventory --source-root ... --output ...
  .venv/bin/python -m legal_corpus_tools.preprocess run ... --profile config/chunk-profile.local.json --resume
  .venv/bin/python -m legal_corpus_tools.preprocess validate --output ...
  .venv/bin/python -m legal_corpus_tools.preprocess compare --source-root ... --output ...
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 离线依赖副本（可选）：工程内若存在 .vendor/ 则优先使用，否则用当前解释器的 site-packages
_VENDOR = Path(__file__).resolve().parents[1] / '.vendor'
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

from legal_corpus_tools.preprocessing.common import (  # noqa: E402
    DEFAULT_OUTPUT, PROJECT_ROOT, OutputLock, clear_stop, read_json, request_stop)
from legal_corpus_tools.preprocessing.compare import build_comparison  # noqa: E402
from legal_corpus_tools.preprocessing.inventory import build_inventory  # noqa: E402
from legal_corpus_tools.preprocessing.report import build_summary, write_report  # noqa: E402
from legal_corpus_tools.preprocessing.runner import run as run_batch  # noqa: E402
from legal_corpus_tools.preprocessing.validate import validate_all  # noqa: E402

# 默认语料根目录：仓库内 01-original/；可用 --source-root 覆盖
DEFAULT_SOURCE = PROJECT_ROOT.parents[2] / '01-original'


def _profile_path(value: str | None) -> Path | None:
    if not value:
        return None
    p = Path(value)
    if not p.is_absolute() and not p.exists():
        p = PROJECT_ROOT / value
    return p


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(prog='legal_corpus_tools.preprocess',
                                     description='首批法律文本离线预处理与分片')
    sub = parser.add_subparsers(dest='cmd', required=True)
    for cmd in ('inventory', 'run', 'validate', 'compare', 'stop', 'clear-stop'):
        p = sub.add_parser(cmd)
        p.add_argument('--source-root', default=str(DEFAULT_SOURCE))
        p.add_argument('--output', default=str(DEFAULT_OUTPUT))
        if cmd in ('run', 'validate'):
            p.add_argument('--profile', default='config/chunk-profile.local.json')
        if cmd in ('run', 'compare'):
            p.add_argument('--law-id', action='append', dest='law_ids')
        if cmd == 'compare':
            p.add_argument('--all-versions', action='store_true')
        if cmd == 'run':
            p.add_argument('--resume', action='store_true')
            p.add_argument('--jurisdiction', choices=['hk', 'sg'])
            p.add_argument('--limit', type=int)
    args = parser.parse_args(argv)
    output = Path(args.output).resolve()
    source_root = Path(args.source_root).resolve()
    output.mkdir(parents=True, exist_ok=True)

    if args.cmd == 'stop':
        request_stop(output)
        print(f'已请求优雅停止：{output / ".stop"}（当前法例安全落盘后退出）')
        return 0
    if args.cmd == 'clear-stop':
        clear_stop(output)
        print('已清除停止标记')
        return 0

    with OutputLock(output):
        if args.cmd == 'inventory':
            inv = build_inventory(source_root, output)
            s = inv['actual_scale']
            print(json.dumps({'command': 'inventory', 'actual_scale': s,
                              'scale_comparison': inv['scale_comparison'],
                              'source_failures': len(inv['source_failures']),
                              'output': str(output / 'inventory.json')},
                             ensure_ascii=False, indent=2))
            return 0
        if args.cmd == 'run':
            clear_stop(output)
            result = run_batch(source_root, output, _profile_path(args.profile),
                               args.resume, args.jurisdiction, args.law_ids, args.limit,
                               ' '.join([Path(sys.argv[0]).name] + argv))
            summary = build_summary(output, result['inventory'], result['results'])
            write_report(output, summary, result['inventory'],
                         read_json(output / 'run-config.json'))
            print(json.dumps({'command': 'run', 'versions_processed': len(result['results']),
                              'totals': summary['totals'],
                              'token_distribution': summary['token_distribution'],
                              'report': str(output / 'report.md')},
                             ensure_ascii=False, indent=2))
            return 0
        if args.cmd == 'compare':
            result = build_comparison(source_root, output,
                                      law_ids=args.law_ids,
                                      all_versions=args.all_versions)
            print(json.dumps({'command': 'compare', **result},
                             ensure_ascii=False, indent=2))
            return 0
        if args.cmd == 'validate':
            summary = validate_all(output, _profile_path(args.profile))
            print(json.dumps({'command': 'validate', 'totals': summary['totals'],
                              'token_distribution': summary['token_distribution'],
                              'report': str(output / 'report.md')},
                             ensure_ascii=False, indent=2))
            failed = (summary['totals']['blocked_versions'] +
                      summary['totals']['failed_versions'])
            return 0 if failed == 0 else 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
