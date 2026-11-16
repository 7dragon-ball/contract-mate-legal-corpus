# -*- coding: utf-8 -*-
"""锁定 profile 的离线 tokenizer；缺资源或版本不符即报错，不联网、不降级。"""
from __future__ import annotations

import base64
import json
from pathlib import Path

from .common import (PARSER_VERSION, PROJECT_ROOT, SPLITTER_VERSION,
                     canonical_json, sha256_file, sha256_text)

DEFAULT_PROFILE = PROJECT_ROOT / 'config' / 'chunk-profile.local.json'


class TokenizerError(RuntimeError):
    pass


def load_profile(path: Path | None = None) -> dict:
    p = Path(path) if path else DEFAULT_PROFILE
    if not p.is_file():
        raise TokenizerError(f'缺少切片 profile：{p}')
    with open(p, encoding='utf-8') as f:
        profile = json.load(f)
    required = ['profile_id', 'splitter_version', 'parser_version', 'max_tokens',
                'overlap_tokens', 'counting_text_rule', 'tokenizer']
    missing = [k for k in required if k not in profile]
    if missing:
        raise TokenizerError(f'profile 缺字段：{missing}')
    if profile['parser_version'] != PARSER_VERSION or profile['splitter_version'] != SPLITTER_VERSION:
        raise TokenizerError(
            f'profile 版本与实现不符：profile {profile["parser_version"]}/'
            f'{profile["splitter_version"]}，实现 {PARSER_VERSION}/{SPLITTER_VERSION}')
    t = profile['tokenizer']
    if not t.get('offline_only'):
        raise TokenizerError('profile 未声明 offline_only，拒绝运行')
    res = PROJECT_ROOT / t['encoding_resource']
    if not res.is_file():
        raise TokenizerError(f'离线编码资源缺失：{res}（正式运行不自动下载）')
    digest = sha256_file(res)
    if digest != t.get('encoding_resource_sha256'):
        raise TokenizerError(f'编码资源摘要不符：实际 {digest} / profile {t.get("encoding_resource_sha256")}')
    profile['_profile_path'] = str(p.relative_to(PROJECT_ROOT)) if p.is_relative_to(PROJECT_ROOT) else str(p)
    profile['_profile_sha256'] = sha256_text(canonical_json({
        k: v for k, v in profile.items() if not k.startswith('_')}))
    # chunk_id 只绑定切片配置，不绑定解析器版本：解析器升级触发重做，但内容 ID 应保持稳定
    split_fields = ('profile_id', 'splitter_version', 'max_tokens', 'overlap_tokens',
                    'counting_text_rule', 'counting_unit', 'tokenizer',
                    'index_header_max_tokens', 'index_header_shorten_rule',
                    'unicode_policy', 'split_order', 'table_policy', 'reconstruction_rule')
    profile['_split_profile_sha256'] = sha256_text(canonical_json(
        {k: profile[k] for k in split_fields if k in profile}))
    return profile


def build_encoding(profile: dict):
    """从工程内 BPE 文件构造 Encoding，绝不调用 tiktoken.get_encoding（会联网）。"""
    import tiktoken
    from tiktoken_ext.openai_public import cl100k_base

    t = profile['tokenizer']
    if tiktoken.__version__ != t.get('library_version'):
        raise TokenizerError(f'tiktoken 版本不符：实际 {tiktoken.__version__} / 锁定 {t.get("library_version")}')
    cfg = cl100k_base()
    ranks: dict[bytes, int] = {}
    res = PROJECT_ROOT / t['encoding_resource']
    with open(res, 'rb') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            token, rank = line.split()
            ranks[base64.b64decode(token)] = int(rank)
    if len(ranks) != t.get('mergeable_ranks'):
        raise TokenizerError(f'BPE ranks 数量不符：{len(ranks)} / {t.get("mergeable_ranks")}')
    return tiktoken.Encoding(
        name=f'{t["name"]}.local',
        pat_str=cfg['pat_str'],
        mergeable_ranks=ranks,
        special_tokens=cfg['special_tokens'],
    )


def retrieval_text(index_header: str, text: str) -> str:
    """系统实际计数/检索文本：索引头与正文分字段保存，但预算合计计算。"""
    return index_header + '\n' + text


def count_tokens(enc, index_header: str, text: str) -> int:
    return len(enc.encode(retrieval_text(index_header, text), disallowed_special=()))
