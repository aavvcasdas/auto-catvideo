#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""[已迁移] 蒙版版（固定 1.8 秒引导语）

这个脚本原来是一份 876 行的独立实现，和另外 3 个 create_video_final*.py
互为拷贝（片头/配音/字幕逻辑各自漂移，修一处漏三处）。

现在统一走 pipeline 引擎，本文件只保留入口，保证老命令照旧能跑：

    python "create_video_final 蒙版版.py"
    python -m pipeline.cli build --style mask_flash_fixed     # 等价写法

可调项都搬进了 config.yaml（音色/语速/片头文案/字幕样式/预检阈值），
不再需要改代码。完整参数见 `python -m pipeline.cli build -h`。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pipeline.cli import main

if __name__ == "__main__":
    sys.exit(main(["build", "--style", "mask_flash_fixed"] + sys.argv[1:]))
