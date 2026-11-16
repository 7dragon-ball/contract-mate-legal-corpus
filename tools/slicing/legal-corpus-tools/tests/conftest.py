# -*- coding: utf-8 -*-
"""测试运行时加载工程内离线依赖副本（.vendor），不联网、不借用其他工程环境。"""
import sys
from pathlib import Path

_VENDOR = Path(__file__).resolve().parents[1] / '.vendor'
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))
