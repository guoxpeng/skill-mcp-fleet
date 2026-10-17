# -*- coding: utf-8 -*-
"""mcp-fleet 测试套件公共配置：把 scripts/ 加入 sys.path。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)
