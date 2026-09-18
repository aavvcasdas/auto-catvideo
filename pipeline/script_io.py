#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""对照表解析 + 素材预检。

预检（preflight）是这轮优化里最"便宜但最值钱"的一环：原来缺图要跑到一半
才 `input()` 问你，配音已经烧掉几十次请求；现在开跑前一次性把
「缺图 / 空文案 / 超长镜头 / 总时长越界 / 敏感词」全查出来。
"""

import collections
import os
import re

from . import text as textmod

# 对照表支持的两种行格式
_PATTERN_BAR = r"^\s*(?:分镜)?(\d+)\s*\|\s*([^\|]+)\s*\|\s*(.*)$"
_PATTERN_PLAIN = r"^\s*(\d+)[\s.．、]+(.+)$"

_SKIP_HINTS = ("此后镜头无配套字幕", "无配套字幕")


def parse_mapping_file(file_path):
    """解析口播文案对照表。

    支持格式：
        01  文案内容
        02. 文案内容
        01 | shot_01.jpg | 文案内容
        分镜01 | shot_01.jpg | 文案内容

    Returns: [(序号, 文案, 图片文件名), ...]
    """
    with open(file_path, "r", encoding="utf-8") as handle:
        content = handle.read()

    mappings = []
    for line in content.split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("---"):
            continue
        if "格式：" in line or "镜头字幕" in line:
            continue

        match = re.match(_PATTERN_BAR, line)
        if match:
            seq = int(match.group(1))
            image = match.group(2).strip()
            caption = match.group(3).strip()
            if any(hint in caption for hint in _SKIP_HINTS):
                continue
            mappings.append((seq, caption, image))
            continue

        match = re.match(_PATTERN_PLAIN, line)
        if match:
            seq = int(match.group(1))
            caption = match.group(2).strip()
            if not caption or len(caption) < 3:
                continue
            mappings.append((seq, caption, f"scene_{seq:02d}.png"))

    return mappings


def find_image_file(image_dir, filename):
    """查找图片（智能匹配 png/jpg、shot_/scene_ 前缀）。找不到返回 None。"""
    if not filename:
        return None
    candidates = [filename]

    base, ext = os.path.splitext(filename)
    ext = ext.lower()
    if ext == ".png":
        candidates.append(base + ".jpg")
    elif ext in (".jpg", ".jpeg"):
        candidates.append(base + ".png")
    else:
        candidates.append(filename + ".png")
        candidates.append(filename + ".jpg")

    for prefix_from, prefix_to in (("scene_", "shot_"), ("shot_", "scene_")):
        if prefix_from in filename:
            swapped = filename.replace(prefix_from, prefix_to)
            swapped_base, swapped_ext = os.path.splitext(swapped)
            candidates.append(swapped)
            candidates.append(swapped_base + (".jpg" if swapped_ext.lower() == ".png" else ".png"))

    for candidate in candidates:
        full = os.path.join(image_dir, candidate)
        if os.path.exists(full):
            return full
    return None


# ============================================================
# 预检
# ============================================================
def preflight(mappings, image_dir, check_cfg=None):
    """开跑前的静态检查。返回 {'errors': [...], 'warnings': [...], 'stats': {...}}。

    errors   → 会让成片不可用（缺图、空文案、敏感词）
    warnings → 值得看一眼但不拦（镜头过长/过短、总时长偏离目标区间、重复用图）
    """
    check_cfg = check_cfg or {}
    min_chars = check_cfg.get("min_shot_chars", 8)
    max_chars = check_cfg.get("max_shot_chars", 80)
    target = check_cfg.get("target_duration_s", [90, 240])
    cps = check_cfg.get("chars_per_second", 4.4)
    max_repeat = check_cfg.get("max_repeat_image", 3)
    banned = check_cfg.get("banned_words", []) or []

    errors, warnings = [], []
    image_usage = collections.Counter()
    total_chars = 0

    for seq, caption, image in mappings:
        if not caption or len(textmod.plain(caption)) < 3:
            errors.append(f"镜头 {seq:02d}: 文案为空")
            continue
        if not find_image_file(image_dir, image):
            errors.append(f"镜头 {seq:02d}: 图片缺失 {image}")
        net_chars = len(textmod.plain(caption))
        total_chars += net_chars
        image_usage[image] += 1
        if net_chars < min_chars:
            warnings.append(f"镜头 {seq:02d}: 仅 {net_chars} 字（<{min_chars}），画面会一闪而过")
        if net_chars > max_chars:
            warnings.append(f"镜头 {seq:02d}: {net_chars} 字（>{max_chars}），建议拆成两镜")
        for word in banned:
            if word and word in caption:
                errors.append(f"镜头 {seq:02d}: 命中敏感词「{word}」")

    for image, count in image_usage.items():
        if count > max_repeat:
            warnings.append(f"图片 {image} 被重复使用 {count} 次")

    est_seconds = total_chars / cps if cps else 0
    if target and est_seconds and not (target[0] <= est_seconds <= target[1]):
        warnings.append(
            f"预估时长 {est_seconds:.0f}s 不在目标区间 {target[0]}–{target[1]}s"
            f"（{total_chars} 字 ÷ {cps} 字/秒）"
        )

    seqs = [seq for seq, _, _ in mappings]
    dup = [seq for seq, count in collections.Counter(seqs).items() if count > 1]
    if dup:
        errors.append(f"镜头编号重复: {sorted(dup)}")

    stats = {
        "shots": len(mappings),
        "chars": total_chars,
        "avg_chars_per_shot": round(total_chars / len(mappings), 1) if mappings else 0,
        "est_duration_s": round(est_seconds, 1),
        "unique_images": len(image_usage),
    }
    return {"errors": errors, "warnings": warnings, "stats": stats}


def format_preflight(result):
    """把预检结果格式化成可读文本。"""
    lines = []
    stats = result["stats"]
    lines.append(
        f"镜头 {stats['shots']} 个 | 净字数 {stats['chars']} | "
        f"平均 {stats['avg_chars_per_shot']} 字/镜 | 预估 {stats['est_duration_s']}s | "
        f"用图 {stats['unique_images']} 张"
    )
    if result["errors"]:
        lines.append(f"✗ 错误 {len(result['errors'])} 条:")
        lines.extend(f"    - {e}" for e in result["errors"])
    if result["warnings"]:
        lines.append(f"⚠ 警告 {len(result['warnings'])} 条:")
        lines.extend(f"    - {w}" for w in result["warnings"])
    if not result["errors"] and not result["warnings"]:
        lines.append("✓ 全部通过")
    return "\n".join(lines)
