#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""[已迁移] 跨场景配音块合并 —— 逻辑已并入 pipeline/text.py。

保留本文件只是为了让 `import tts_grouping` 的老代码不炸。
新代码请直接 `from pipeline import text`。
"""

from pipeline.text import (  # noqa: F401
    DEFAULT_GROUP_CHARS as TTS_GROUP_CHARS,
)
from pipeline.text import (  # noqa: F401
    DEFAULT_MAX_CHARS as TTS_MAX_CHARS,
)
from pipeline.text import (  # noqa: F401
    alloc,
    build_groups,
    clean_for_tts,
    plain,
    split_into_subtitles,
    split_text_for_tts,
)

# 旧函数名兼容（原来叫 prepare_scenes / synthesize_groups，签名不同）
from pipeline.body import prepare_scenes  # noqa: F401

__all__ = [
    "TTS_GROUP_CHARS",
    "TTS_MAX_CHARS",
    "alloc",
    "build_groups",
    "clean_for_tts",
    "plain",
    "prepare_scenes",
    "split_into_subtitles",
    "split_text_for_tts",
]
