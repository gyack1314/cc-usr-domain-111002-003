#!/usr/bin/env python3
"""导出自动化入口（支持隔离模式 ``python3 -I run_export.py ...``）。

``-I`` 会从 sys.path 移除脚本目录与当前目录，因此这里先把仓库根目录
显式插回 sys.path，再交给 :mod:`export_automation.cli`。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from export_automation.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
