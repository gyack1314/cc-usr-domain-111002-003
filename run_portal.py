#!/usr/bin/env python3
"""本地合成门户启动器，支持隔离模式 ``python3 -I run_portal.py --port 8090``。

``-I`` 会移除脚本目录与当前目录的隐式 sys.path 条目，这里显式插回仓库根目录，
其余参数原样转交给 :mod:`portal.__main__`。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from portal.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main()
