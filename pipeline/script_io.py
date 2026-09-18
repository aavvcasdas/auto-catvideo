#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""对照表解析 + 素材预检。

预检（preflight）是这轮优化里最"便宜但最值钱"的一环：原来缺图要跑到一半
才 `input()` 问你，配音已经烧掉几十次请求；现在开跑前一次性把
「缺图 / 空文案 / 超长镜头 / 总时长越界 / 敏感词」全查出来。
"""

import collections
import hashlib
import os
import re

from . import text as textmod

# 对照表支持的两种行格式
_PATTERN_BAR = r"^\s*(?:分镜)?(\d+)\s*\|\s*([^\|]+)\s*\|\s*(.*)$"
_PATTERN_PLAIN = r"^\s*(\d+)[\s.．、]+(.+)$"

_SKIP_HINTS = ("此后镜头无配套字幕", "无配套字幕")

# 混剪候选池分隔符。
# 为什么不是 "|"：对照表本身就用 | 分字段（01 | shot_01.jpg | 文案）。
# 为什么不是 "/"：文案里会出现 "1/217" 这种，且 "/" 是字幕切分符。
CANDIDATE_SEP = "//"


def split_candidates(field):
    """把 `A//B//C` 拆成候选池；没有分隔符就返回单元素列表。"""
    parts = [p.strip() for p in (field or "").split(CANDIDATE_SEP)]
    parts = [p for p in parts if p]
    return parts or [(field or "").strip()]


def has_candidates(field):
    return CANDIDATE_SEP in (field or "")


def parse_mapping_file(file_path, variant=None, seed=None):
    """解析口播文案对照表。

    支持格式：
        01  文案内容
        02. 文案内容
        01 | shot_01.jpg | 文案内容
        分镜01 | shot_01.jpg | 文案内容
        01 | shot_01.jpg//shot_09.jpg | 文案A//文案B     ← 混剪候选池

    Args:
        variant: 变体序号。None = 每镜都取候选池第 1 个（与旧行为一致）；
                 整数 = 按 sample_variant() 的规则挑，用于批量混剪。
        seed:    变体采样的随机种子（配合 variant 使用）。

    Returns: [(序号, 文案, 图片文件名), ...]
    """
    entries = parse_mapping_entries(file_path)
    if variant is None:
        return [(e["seq"], e["candidates_text"][0], e["candidates_image"][0]) for e in entries]
    return [
        (e["seq"], text, image)
        for e, text, image in zip(
            entries,
            sample_picks([e["candidates_text"] for e in entries], variant, seed),
            sample_picks([e["candidates_image"] for e in entries], variant, seed),
        )
    ]


def parse_mapping_entries(file_path):
    """解析成带候选池的完整条目。字段：seq / candidates_text / candidates_image。"""
    with open(file_path, "r", encoding="utf-8") as handle:
        content = handle.read()

    entries = []
    for line in content.split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("---"):
            continue
        if "格式：" in line or "镜头字幕" in line:
            continue

        match = re.match(_PATTERN_BAR, line)
        if match:
            seq = int(match.group(1))
            raw_image = match.group(2).strip()
            raw_caption = match.group(3).strip()
            if any(hint in raw_caption for hint in _SKIP_HINTS):
                continue
            entries.append(
                {
                    "seq": seq,
                    "candidates_image": split_candidates(raw_image),
                    "candidates_text": split_candidates(raw_caption),
                }
            )
            continue

        match = re.match(_PATTERN_PLAIN, line)
        if match:
            seq = int(match.group(1))
            raw_caption = match.group(2).strip()
            if not raw_caption or len(raw_caption) < 3:
                continue
            entries.append(
                {
                    "seq": seq,
                    "candidates_image": [f"scene_{seq:02d}.png"],
                    "candidates_text": split_candidates(raw_caption),
                }
            )

    return entries


def sample_picks(pools, variant, seed=None):
    """从每个候选池挑一个，返回与 pools 等长的列表。

    对应 MoneyPrinterPlus `services/hunjian/hunjian_service.py` 的
    `random_line_from_text_file()`——它每个场景从文本文件里随机抽一行，
    靠组合数产出「100 条不重复」的混剪视频。

    这里不用纯随机，而是 `md5(seed:variant:镜头号)` 取模：
      · 完全可复现（同 seed + 同 variant 永远得到同一组合，可以重跑）
      · 每一镜独立采样，所以 88 镜 × 2 候选也能铺满 2^88 的组合空间，
        而不是像「按序号轮转」那样只有 2 种结果
    候选池长度为 1 时恒定返回那一个，所以没有候选池的老对照表行为不变。
    想强制"每镜都取第一个候选"，用 variant=None。
    """
    picks = []
    for index, pool in enumerate(pools):
        if not pool:
            picks.append("")
        elif len(pool) == 1:
            picks.append(pool[0])
        else:
            picks.append(pool[_stable_pick(seed, variant, index, len(pool))])
    return picks


def _stable_pick(seed, variant, index, modulo):
    """由 (seed, variant, 镜头号) 推出稳定的候选下标。"""
    digest = hashlib.md5(
        f"{seed or ''}:{variant}:{index}".encode("utf-8")
    ).hexdigest()
    return int(digest[:8], 16) % max(modulo, 1)


def variant_fingerprint(text_picks, image_picks):
    """一个变体的指纹，用来判重。"""
    return tuple(zip(text_picks, image_picks))


def distinct_variants(entries, count, seed=None, max_probe=None):
    """生成 count 个**互不重复**的变体。

    返回 [(variant, text_picks, image_picks), ...]。
    组合空间不够 count 时，返回能拿到的全部并如实说明（不假装凑数）。
    """
    text_pools = [e["candidates_text"] for e in entries]
    image_pools = [e["candidates_image"] for e in entries]
    space = 1
    for pool in text_pools:
        space *= max(len(pool), 1)
    for pool in image_pools:
        space *= max(len(pool), 1)

    max_probe = max_probe or max(count * 40, 400)
    seen, out = set(), []
    for variant in range(max_probe):
        if len(out) >= count:
            break
        texts = sample_picks(text_pools, variant, seed)
        images = sample_picks(image_pools, variant, seed)
        mark = variant_fingerprint(texts, images)
        if mark in seen:
            continue
        seen.add(mark)
        out.append((variant, texts, images))
    return out, space


def count_variants(file_path):
    """这份对照表能出多少种组合（各镜候选数连乘，封顶 10^9 防爆）。"""
    total = 1
    for entry in parse_mapping_entries(file_path):
        total *= max(len(entry["candidates_text"]), 1) * max(len(entry["candidates_image"]), 1)
        if total > 10 ** 9:
            return 10 ** 9
    return total


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
