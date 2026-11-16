# -*- coding: utf-8 -*-
"""首批离线预处理：公共工具（稳定摘要、原子落盘、路径安全、互斥锁、进度）。"""
from __future__ import annotations

import csv
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = PROJECT_ROOT / 'build' / 'preprocess-first-batch-20261105'

SCHEMA_VERSION = '1'
# 解析器/分片器版本：行为变更时必须递增，缓存键据此让旧产物失效
PARSER_VERSION = 'preprocess-parser-1.4.0'
SPLITTER_VERSION = 'preprocess-splitter-1.0.0'
# chunk_id 构造规则版本：只进缓存键，不进内容 ID 载荷
CHUNK_ID_VERSION = 'chunk-id-1.1.0'
PROCESSING_PENDING = 'pending'
PROCESSING_COMPLETED = 'completed'
PROCESSING_FAILED = 'failed'
EXTRACTION_PASSED = 'passed'
EXTRACTION_BLOCKED = 'blocked'


def canonical_json(obj: Any) -> str:
    """规范化 JSON：用于内容 ID，不含当前时间/随机值/绝对路径。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode('utf-8')).hexdigest()


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def stable_id(prefix: str, payload: dict) -> str:
    return f'{prefix}:{sha256_text(canonical_json(payload))[:32]}'


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + '\n')


def atomic_write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    lines = [json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
             for r in rows]
    atomic_write_text(path, '\n'.join(lines) + ('\n' if lines else ''))


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def read_csv(path: Path) -> list[dict]:
    """索引含中文与空格路径，必须 utf-8-sig + csv 模块，禁止手工 split。"""
    with open(path, encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


class OutputLock:
    """输出目录互斥锁：拒绝两个进程同时写，不做静默排队。"""

    def __init__(self, output: Path, name: str = '.preprocess.lock'):
        self.path = output / name
        self.fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, 'a+')
        try:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.fh.close()
            self.fh = None
            raise RuntimeError(f'输出目录已被另一个进程占用：{self.path}') from exc
        return self

    def __exit__(self, *exc):
        if self.fh:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            self.fh.close()
            self.fh = None


def safe_relpath(root: Path, rel: str) -> Path:
    """解析输入根目录内的相对路径；拒绝绝对路径、越界与指向外部的符号链接。"""
    if not rel or rel.startswith('/') or '\\' in rel:
        raise ValueError(f'非法相对路径：{rel!r}')
    root_real = root.resolve()
    cur = root_real
    for part in Path(rel).parts:
        if part in ('.', ''):
            continue
        if part == '..':
            raise ValueError(f'路径越界：{rel!r}')
        cur = cur / part
        if cur.is_symlink():
            target = cur.resolve()
            if not target.is_relative_to(root_real):
                raise ValueError(f'符号链接指向输入根目录之外：{rel!r}')
    resolved = (root_real / rel).resolve()
    if not resolved.is_relative_to(root_real):
        raise ValueError(f'路径越界：{rel!r}')
    return resolved


def normalize_law_id(jurisdiction: str, code: str) -> str:
    """法域 + 规范化标识；保留新加坡标识原大小写。"""
    j = 'hk' if jurisdiction in ('hk', '香港') else 'sg'
    if j == 'hk':
        m = re.search(r'(\d+[A-Za-z]?)', code or '')
        if not m:
            raise ValueError(f'无法规范化香港章号：{code!r}')
        # 文档约定 hk:cap60G：章号后缀字母保留原大小写
        return 'hk:cap' + m.group(1)
    c = re.sub(r'[^A-Za-z0-9\-]', '', code or '')
    if not c:
        raise ValueError(f'无法规范化新加坡标识：{code!r}')
    return 'sg:' + c


def language_code(fmt_lang: str, hint: str = '') -> str:
    """统一语言代码：zh-Hant / en。"""
    s = (fmt_lang or '') + ' ' + (hint or '')
    if 'zh-Hant' in s or '繁体' in s or '中文' in s:
        return 'zh-Hant'
    return 'en'


def append_progress(output: Path, record: dict) -> None:
    with open(output / 'progress.jsonl', 'a', encoding='utf-8', newline='\n') as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n')


def append_issue(output: Path, record: dict) -> None:
    """事件日志按整行去重：blocked 版本每轮重试不应把同一问题累计成多条。"""
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n'
    path = output / 'issues.jsonl'
    if path.is_file():
        with open(path, encoding='utf-8') as f:
            if any(existing == line for existing in f):
                return
    with open(path, 'a', encoding='utf-8', newline='\n') as f:
        f.write(line)


def request_stop(output: Path) -> None:
    atomic_write_text(output / '.stop', 'stop requested\n')


def stop_requested(output: Path) -> bool:
    return (output / '.stop').exists()


def clear_stop(output: Path) -> None:
    p = output / '.stop'
    if p.exists():
        p.unlink()
