#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""配置层：默认配置 + YAML 覆盖 + 命令行覆盖。

设计原则（参考 MoneyPrinterPlus 的 config.yaml 思路，但不引入第三方依赖）：
    · 所有"会改一次就要动代码"的东西都进配置：音色、语速、片头文案、字幕样式、
      轨道名、预检阈值。
    · 配置解析器是本文件里 ~80 行的 YAML 子集实现（两层缩进 + 标量 + 列表），
      够用了，且 `pip install` 之外零依赖。

片头预设（OPENING_PRESETS）对应仓库里原来的 4 个脚本：
    mask_flash        = create_video_final.py（先测配音时长，再打蒙版快闪）
    mask_flash_fixed  = create_video_final 蒙版版.py（固定 1.8s 引导语）
    flash9            = create_video_final 快闪版开头.py（9+8 张交替推拉）
    grid              = create_video_final 九宫格开头.py（5 张九宫格入场）
"""

import copy
import os
import re

# 仓库根目录（pipeline/ 的上一级）
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ============================================================
# 默认配置
# ============================================================
DEFAULT_CONFIG = {
    "project": {
        # None = 自动探测剪映草稿目录（可用环境变量 JY_PROJECTS_ROOT 覆盖）
        "drafts_root": None,
        "name_prefix": "人生副本",
        "width": 1920,
        "height": 1080,
    },
    "input": {
        "mapping_file": os.path.join(REPO_ROOT, "剧本", "口播文案_图片序号_对应表.txt"),
        "image_dir": os.path.join(REPO_ROOT, "image"),
        "audio_dir": os.path.join(REPO_ROOT, "audio"),
    },
    "tts": {
        "backend": "sami",          # sami | edge | estimate(离线估算，调试/单测用)
        "allow_fallback": False,    # 关掉 fallback，避免混用不同音色
        "speaker": "ICL_zh_male_momodianying",
        "speaker_candidates": [
            {"id": "ICL_zh_male_momodianying", "label": "默默讲电影（默认）"},
            {"id": "ICL_zh_female_manbo_jianying", "label": "曼波讲电影"},
            {"id": "zh_female_aoyunliuyuxi", "label": "刘语熙"},
            {"id": "BV025_streaming", "label": "台湾女生"},
        ],
        "speed": 1.05,
        "retries": 3,
        "retry_wait": 1.0,
        "cache": True,
        "cache_dir": os.path.join(REPO_ROOT, ".cache", "tts"),
        "track": "VoiceOver",
        # estimate 模式下：中文字 / 秒（语速 1.05 时实测约 4.4）
        "estimate_chars_per_second": 4.4,
        # 配音块：多个连续镜头合并成一次 TTS 请求，语气才连贯。
        # 块越小字幕对齐越准，块越大语气越连贯。
        "group_chars": 180,
        "max_chars": 300,           # 单次请求上限（SAMI 流式接口上限 2000，留足余量）
        "group_gap_us": 0,          # 块间停顿（微秒）。>0 可让段落之间喘口气
        # 人声增强特效名（pyJianYingDraft 的 AudioSceneEffectType 成员）；null = 关闭。
        # 注意：旧脚本写的 draft.Effects.audio_loudness_normalization 在本仓库
        # 的 pyJianYingDraft 里根本不存在，那行一直被 except: pass 吞掉。
        "audio_effect": None,
        "fade_in_us": 0,            # 淡入淡出（防爆音）；0 = 关闭
        "fade_out_us": 0,
    },
    "opening": {
        "style": "mask_flash",
        "lead_text": "今天你要体验的人生副本是外号的保质期只有一届学生",
        "measure_lead_tts": True,   # true: 先合成引导语配音、用真实时长当片头时长
        "lead_fallback_us": 1800000,
        "flash_count": 5,
        "flash_frame_span": 11,     # 每张快闪图的起始间隔（帧）
        "keyframe_frames": 10,      # 关键帧动画长度（帧）
        "mask_size": 0.8,
        "mask_feather_from": 50.0,
        "scale_from": 1.5,
        "scale_to": 1.0,
        "sfx": "ratchet.wav",
        "sfx_fallback": "coding.WAV",
        "random_seed": None,        # 固定后快闪选图可复现
        "lead_style": {"size": 9.0, "letter_spacing": 1, "color": [1.0, 0.976, 0.945]},
        "lead_border": {"alpha": 0.85, "width": 60.0},
    },
    "subtitle": {
        "track": "Subtitles",
        "font": "优设标题黑",
        "size": 5.0,
        "letter_spacing": 1,
        "border_width": 40.0,
        "transform_y": -0.8,
        "background": True,
        "background_alpha": 0.5,
        "background_round": 0.35,
        "first_centered": True,
        "first_size": 6.0,
        "first_color": [1.0, 0.976, 0.945],
        # 字幕时长分配：按加权字数比例（ascii 半宽、数字按读法计权），
        # 比旧脚本的纯字符数更贴近真实语速。
        "align": "weighted_chars",
        # 单句字幕上限（净字数），超过则再切一刀，避免一屏挤太多字
        "max_chars_per_line": 22,
    },
    "body": {
        "video_track": "Opening_Flash_1",   # 正片主轨（与片头快闪共轨，保证图层顺序）
        "ken_burns": True,
        "first_shot_effects": ["幻彩故障", "震动屏闪"],
        "first_shot_effect_us": 1000000,
        "effect_track": "EffectTrack",
    },
    "disclaimer": {
        "enabled": True,
        "text": "虚构创作，与真实人物无关",
        "track": "DisclaimerTrack",
        "size": 3.0,
        "alpha": 0.8,
        "transform_x": -0.8,
        "transform_y": 0.8,
    },
    "bgm": {
        "enabled": False,
        "source": "cloud",        # cloud = 剪映云曲库关键词; file = 本地文件
        "query": "沉重 钢琴",
        "file": None,
        "track": "BGM",
        "volume": 0.15,
    },
    "run": {
        "interactive": True,      # 缺图时是否询问 y/N；批量/CI 请设 false
        "skip_missing_images": True,
        "verbose": True,
        "fail_fast": False,
        "report": True,           # 输出 run_report.json
        "report_dir": os.path.join(REPO_ROOT, "output"),
        "srt": True,              # 同时导出 .srt，便于核对字幕时间轴
    },
    # Arena agent 交接（替代 MoneyPrinterPlus 的 llm.provider + api_key）
    "agent": {
        "provider": "arena-agent",
        "root": None,              # None = <repo>/.agent
        "story_chars": [1200, 2500],   # demo 库《剧本人生》的短片硬指标
        "min_shots": 40,
        "max_shots": 90,
    },
    # 发布元数据（不自动发布，只出清单；对应 MoneyPrinterPlus 的 publisher 段）
    "publish": {
        "enabled": True,
        "title_prefix": "",
        "collection": "人生副本",
        "hashtags": ["人生副本", "剧本人生", "第二人称"],
        "kuaishou_domain": "",     # 例如 "教育/语言教育"
        "platforms": ["douyin", "kuaishou", "xiaohongshu", "shipinhao"],
    },
    "check": {
        "min_shot_chars": 8,
        "max_shot_chars": 80,
        # 预估时长区间（秒）。
        # 注意：demo 库 README 写的是「单条 1200–2500 字 ≈ 1.5–3 分钟」，
        # 但这两个数对不上——中文口播 4.4 字/秒时，1200–2500 字是 273–568 秒
        # （4.5–9.5 分钟）；要真做到 1.5–3 分钟只能写 400–800 字。
        # 这里以「字数」为准（那才是可数的硬指标），时长区间按 4.4 字/秒推出来。
        "target_duration_s": [240, 600],
        "chars_per_second": 4.4,
        "max_repeat_image": 3,
        "banned_words": [],       # 平台敏感词，命中即 FAIL
    },
    "shotlist": {
        "target_chars": 40,       # ASR 原稿转对照表时，每镜目标字数
        "min_chars": 12,
        "max_chars": 60,
        "lead_lines": 3,          # 原稿开头几行视为引导语（片头文案）
        "image_prefix": "shot",
        "prompt_style": "电影感写实风格，冷色调，硬光，浅景深，35mm 胶片质感，第二人称视角",
    },
}


# ============================================================
# 片头预设：对应原来 4 个脚本
# ============================================================
OPENING_PRESETS = {
    "mask_flash": {
        "style": "mask_flash",
        "measure_lead_tts": True,
        "flash_count": 5,
        "flash_frame_span": 11,
        "keyframe_frames": 10,
    },
    "mask_flash_fixed": {
        "style": "mask_flash",
        "measure_lead_tts": False,
        "lead_fallback_us": 1800000,
        "flash_count": 5,
        "flash_frame_span": 11,
        "keyframe_frames": 10,
    },
    "flash9": {
        "style": "flash9",
        "measure_lead_tts": False,
        "lead_fallback_us": 1800000,
        "flash_count": 9,
        "flash_duration_us": 1667000,
        "repeat_first": 8,
    },
    "grid": {
        "style": "grid",
        "measure_lead_tts": True,
        "grid_duration_us": 2000000,
        "flash_count": 4,          # 4 张随机 + 第 5 张用剧本第一张
        "grid_scales": [1.0, 1.02, 1.01, 0.99, 1.03],
        "intro_animation": "九宫格",
        "lead_style": {"size": 8.0, "letter_spacing": 1},
        "lead_border": {"alpha": 1.0, "width": 50.0},
    },
}


def deep_merge(base, override):
    """递归合并 dict，override 里的 None 表示"保持 base"。"""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        elif value is None:
            continue
        else:
            out[key] = copy.deepcopy(value)
    return out


# ============================================================
# 极简 YAML 子集解析（缩进 2 空格 / key: value / - 列表项 / # 注释）
# ============================================================
_SCALAR_TRUE = {"true", "yes", "on"}
_SCALAR_FALSE = {"false", "no", "off"}
_SCALAR_NULL = {"null", "none", "~", ""}


def _coerce_scalar(raw):
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    low = text.lower()
    if low in _SCALAR_NULL:
        return None
    if low in _SCALAR_TRUE:
        return True
    if low in _SCALAR_FALSE:
        return False
    if re.fullmatch(r"[+-]?\d+", text):
        return int(text)
    if re.fullmatch(r"[+-]?(\d+\.\d*|\.\d+)([eE][+-]?\d+)?", text):
        return float(text)
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        return [_coerce_scalar(p) for p in inner.split(",")] if inner else []
    return text


def _strip_value(rest, lineno):
    """剥掉行尾注释，同时正确处理带引号的值。

    之前只在「不以引号开头」时才剥注释，于是
        kuaishou_domain: ""          # 例：教育/语言教育
    会把整行（含注释）当成值。
    """
    if not rest:
        return rest
    if rest[0] in "\"'":
        quote = rest[0]
        end = rest.find(quote, 1)
        if end == -1:
            raise ValueError(f"config 第 {lineno} 行引号没闭合: {rest!r}")
        tail = rest[end + 1:].strip()
        if tail and not tail.startswith("#"):
            raise ValueError(f"config 第 {lineno} 行引号后有多余内容: {tail!r}")
        return rest[: end + 1]
    return re.split(r"\s+#", rest, maxsplit=1)[0].strip()


def _tokenize_yaml(text):
    """把 YAML 子集切成 [(indent, kind, key, raw_value, lineno)]。"""
    tokens = []
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip():
            continue
        stripped = raw_line.strip()
        if stripped.startswith("#"):
            continue
        leading = raw_line[: len(raw_line) - len(raw_line.lstrip())]
        if "\t" in leading:
            raise ValueError(f"config 第 {lineno} 行用了 Tab 缩进，请改成空格")
        indent = len(leading)
        if stripped.startswith("- "):
            tokens.append((indent, "item", None, stripped[2:], lineno))
            continue
        if stripped == "-":
            raise ValueError(f"config 第 {lineno} 行：'-' 后面要跟值")
        if ":" not in stripped:
            raise ValueError(f"config 第 {lineno} 行缺少冒号: {stripped!r}")
        key, _, rest = stripped.partition(":")
        rest = _strip_value(rest.strip(), lineno)
        tokens.append((indent, "key", key.strip(), rest, lineno))
    return tokens


def parse_simple_yaml(text):
    """解析 YAML 子集（嵌套映射 / 标量 / 字符串列表 / # 注释 / 行尾注释）。

    刻意不支持：锚点、多行字符串、列表里嵌对象。config.yaml 用不到，
    需要的话直接换成 pyyaml。
    """
    tokens = _tokenize_yaml(text)
    value, _ = _parse_block(tokens, 0, tokens[0][0] if tokens else 0)
    return value if isinstance(value, dict) else {}


def _parse_block(tokens, i, indent):
    """解析同一缩进层级的一段内容，返回 (value, next_index)。"""
    if i < len(tokens) and tokens[i][1] == "item" and tokens[i][0] == indent:
        items = []
        while i < len(tokens) and tokens[i][1] == "item" and tokens[i][0] == indent:
            items.append(_coerce_scalar(tokens[i][3]))
            i += 1
        return items, i

    node = {}
    while i < len(tokens):
        tok_indent, kind, key, rest, lineno = tokens[i]
        if tok_indent < indent or kind == "item":
            break
        if tok_indent > indent:
            raise ValueError(f"config 第 {lineno} 行缩进异常（多了空格）")
        if rest:
            node[key] = _coerce_scalar(rest)
            i += 1
            continue
        # 空值：看下一行是子块还是空
        if i + 1 < len(tokens) and tokens[i + 1][0] > indent:
            child, i = _parse_block(tokens, i + 1, tokens[i + 1][0])
            node[key] = child
        else:
            node[key] = None
            i += 1
    return node, i


def load_config(path=None):
    """加载配置：默认值 <- 仓库 config.yaml <- path 指定的文件。"""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    candidates = []
    if path:
        candidates.append(path)
    candidates.append(os.path.join(REPO_ROOT, "config.yaml"))

    for candidate in candidates:
        if not candidate or not os.path.exists(candidate):
            continue
        with open(candidate, "r", encoding="utf-8") as handle:
            data = parse_simple_yaml(handle.read())
        cfg = deep_merge(cfg, data)
    return cfg


def apply_cli_overrides(cfg, **kwargs):
    """把命令行参数覆盖进配置（只覆盖显式给出的项）。"""
    mapping = {
        "speaker": ("tts", "speaker"),
        "speed": ("tts", "speed"),
        "backend": ("tts", "backend"),
        "group_chars": ("tts", "group_chars"),
        "style": ("opening", "style"),
        "lead_text": ("opening", "lead_text"),
        "drafts_root": ("project", "drafts_root"),
        "project_name": ("project", "name"),
        "no_cache": ("tts", "_no_cache"),
        "verbose": ("run", "verbose"),
        "no_report": ("run", "_no_report"),
    }
    for key, value in kwargs.items():
        if value is None or key not in mapping:
            continue
        section, name = mapping[key]
        cfg.setdefault(section, {})[name] = value

    if cfg["tts"].pop("_no_cache", False):
        cfg["tts"]["cache"] = False
    if cfg["run"].pop("_no_report", False):
        cfg["run"]["report"] = False
    return cfg


def apply_preset(cfg, preset_name=None):
    """应用片头预设。preset_name 为空则用配置里的 opening.style 反查。

    预设名（mask_flash_fixed）和应用后的 style（mask_flash）不是一回事。
    所以应用过一次就打上 _preset 标记，后续调用（比如 build() 内部再调一次）
    不再用 style 反查覆盖 —— 否则 mask_flash_fixed 会被 mask_flash 的
    measure_lead_tts=True 顶掉，片头时长就不对了。
    """
    opening = cfg.setdefault("opening", {})
    if opening.get("_preset"):
        return cfg
    style = preset_name or opening.get("style")
    if not style or style not in OPENING_PRESETS:
        return cfg
    cfg["opening"] = deep_merge(opening, OPENING_PRESETS[style])
    cfg["opening"]["_preset"] = style
    return cfg


def known_presets():
    return sorted(OPENING_PRESETS.keys())
