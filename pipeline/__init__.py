#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""人生副本视频流水线（统一引擎）。

原先仓库里有 4 份 create_video_final*.py（合计 ~3600 行）互为拷贝，
片头/配音/字幕逻辑各自漂移。本包把它们收敛成「配置 + 可测试模块」：

    pipeline.config     配置加载（YAML 子集，零第三方依赖）+ 片头预设
    pipeline.text       文案切分（配音块 / 字幕短句 / ASR 原稿转对照表）
    pipeline.script_io  对照表解析 + 素材预检
    pipeline.tts        配音引擎（重试 / 语速 / 缓存 / estimate 模式）
    pipeline.openings   4 种片头风格
    pipeline.body       正片（图片 + 字幕 + Ken Burns + 首镜特效）
    pipeline.pipeline   编排：解析 -> 预检 -> 片头 -> 配音 -> 正片 -> 保存
    pipeline.cli        命令行入口 build / shotlist / check

外部依赖只有 jianying-editor-skill-main 里的 JyProject 与 pyJianYingDraft。
"""

__version__ = "2.0.0"
