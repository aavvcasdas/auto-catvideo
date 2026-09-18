#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""片头风格：mask_flash / flash9 / grid。

对应仓库里原来的 4 个脚本（见 config.OPENING_PRESETS）。
所有魔法数字（帧数、缩放、羽化、轨道名）都从配置读，不再散落在函数体里。

修掉的两个"静默失效"
-------------------
1. `VideoSceneEffectType.荧光爆闪` 在本仓库 vendor 的 pyJianYingDraft 里不存在，
   旧代码用 `except: pass` 吞掉了 AttributeError —— 偶数张快闪图的"白闪"
   其实一次都没加上。这里改成配置里写枚举名，启动时校验一次并打印警告。
2. 旧脚本按 16667us 当一帧（60fps），但草稿实际是 30fps（33333us/帧），
   "10 帧"只走了真实 5 帧。这里统一按 `project.script.fps` 换算。
"""

import os
import random

from .tts import format_time


def pick_images(image_dir, count, seed=None):
    """随机挑选图片；不足时循环补足。"""
    files = sorted(
        f for f in os.listdir(image_dir) if f.lower().endswith((".png", ".jpg", ".jpeg"))
    )
    if not files:
        return []
    rng = random.Random(seed)
    if len(files) < count:
        pool = files * ((count // len(files)) + 1)
        return rng.sample(pool, count)
    return rng.sample(files, count)


def frame_us(project):
    """一帧的微秒数（按草稿真实帧率）。"""
    fps = getattr(project.script, "fps", 30) or 30
    return int(round(1_000_000 / fps))


def _lead_tts(project, cfg, tts_engine, log):
    """合成引导语配音，返回真实时长（失败则用配置的兜底时长）。"""
    opening_cfg = cfg["opening"]
    fallback = int(opening_cfg.get("lead_fallback_us", 1800000))
    if not opening_cfg.get("measure_lead_tts", True):
        return fallback

    lead_text = (opening_cfg.get("lead_text") or "").replace(" ", "")
    if not lead_text:
        return fallback
    segment, duration = tts_engine.synthesize(project, lead_text, 0, "Opening_Lead_Voice")
    if segment:
        log(f"  ✓ 引导语配音: {format_time(duration)}")
        return duration
    log(f"  ⚠ 引导语配音失败，使用兜底时长 {format_time(fallback)}")
    return fallback


def _lead_caption(project, cfg, duration):
    """引导语文字卡。"""
    import pyJianYingDraft as draft
    from pyJianYingDraft import FontType

    opening_cfg = cfg["opening"]
    style_cfg = opening_cfg.get("lead_style", {})
    border_cfg = opening_cfg.get("lead_border", {})
    try:
        project.add_text_simple(
            text=opening_cfg.get("lead_text") or "",
            start_time=0,
            duration=duration,
            track_name="Opening_Lead_Text",
            font=FontType.优设标题黑,
            style=draft.TextStyle(
                size=float(style_cfg.get("size", 9.0)),
                letter_spacing=int(style_cfg.get("letter_spacing", 1)),
                color=tuple(style_cfg.get("color", (1.0, 0.976, 0.945))),
            ),
            border=draft.TextBorder(
                color=(0.0, 0.0, 0.0),
                alpha=float(border_cfg.get("alpha", 0.85)),
                width=float(border_cfg.get("width", 60.0)),
            ),
            clip_settings=draft.ClipSettings(transform_y=0.0),
        )
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ 引导语文字卡失败: {str(exc)[:50]}")
        return False


def _add_sfx(project, cfg, duration, log):
    """片头音效（棘轮音，缺文件时退回 coding.WAV）。"""
    opening_cfg = cfg["opening"]
    audio_dir = cfg["input"]["audio_dir"]
    candidates = [opening_cfg.get("sfx"), opening_cfg.get("sfx_fallback")]
    for name in candidates:
        if not name:
            continue
        path = os.path.join(audio_dir, name)
        if os.path.exists(path):
            segment = project.add_media_safe(
                media_path=path, start_time=0, duration=duration, track_name="Opening_Flash_SFX"
            )
            if segment:
                log(f"  ✓ 音效: {name}（{format_time(duration)}）")
            return segment
    log("  ⚠ 音效文件缺失，跳过快闪音效")
    return None


def _resolve_effect(name, log, warned):
    """把配置里的特效名解析成枚举；不存在则警告一次并返回 None。"""
    from pyJianYingDraft import VideoSceneEffectType

    effect = getattr(VideoSceneEffectType, name, None)
    if effect is None and name not in warned:
        log(f"  ⚠ 视频特效不存在: {name}（已跳过）")
        warned.add(name)
    return effect


# ============================================================
# 片头 1：线性蒙版快闪（原 create_video_final.py / 蒙版版）
# ============================================================
def build_mask_flash(project, cfg, find_image, tts_engine, log):
    from pyJianYingDraft import KeyframeProperty as KP, MaskType

    opening_cfg = cfg["opening"]
    image_dir = cfg["input"]["image_dir"]

    log("\n[片头] 线性蒙版快闪 + 引导语 + 音效")
    lead_duration = _lead_tts(project, cfg, tts_engine, log)
    _lead_caption(project, cfg, lead_duration)

    flash_duration = int(opening_cfg.get("flash_duration_us") or lead_duration)
    _add_sfx(project, cfg, flash_duration, log)

    count = int(opening_cfg.get("flash_count", 5))
    span_frames = int(opening_cfg.get("flash_frame_span", 11))
    kf_frames = int(opening_cfg.get("keyframe_frames", 10))
    one_frame = frame_us(project)

    images = pick_images(image_dir, count, opening_cfg.get("random_seed"))
    log(f"  快闪 {len(images)} 张，每张间隔 {span_frames} 帧，关键帧 {kf_frames} 帧")

    for index, image_name in enumerate(images):
        image_path = find_image(image_dir, image_name)
        if not image_path:
            continue
        start = index * span_frames * one_frame
        duration = max(one_frame, lead_duration - start)
        segment = project.add_media_safe(
            media_path=image_path,
            start_time=start,
            duration=duration,
            track_name=f"Opening_Flash_{index + 1}",
        )
        if not segment:
            continue

        segment.add_keyframe(KP.uniform_scale, 0, float(opening_cfg.get("scale_from", 1.5)))
        segment.add_keyframe(KP.position_x, 0, 0.0)
        segment.add_keyframe(KP.position_y, 0, 0.0)
        segment.add_keyframe(KP.uniform_scale, kf_frames * one_frame, float(opening_cfg.get("scale_to", 1.0)))

        try:
            segment.add_mask(
                MaskType.线性,
                center_y=0.0,
                size=float(opening_cfg.get("mask_size", 0.8)),
                feather=float(opening_cfg.get("mask_feather_from", 50.0)),
                invert=False,
            )
            segment.add_keyframe(KP.mask_feather, 0, float(opening_cfg.get("mask_feather_from", 50.0)) / 100.0)
            segment.add_keyframe(KP.mask_feather, kf_frames * one_frame, 0.0)
            log(
                f"  ✓ 快闪{index + 1}/{count} {image_name} "
                f"(帧{index * span_frames}-{index * span_frames + kf_frames}, @{format_time(start)})"
            )
        except Exception as exc:  # noqa: BLE001
            log(f"  ✗ 快闪{index + 1} 蒙版失败: {str(exc)[:30]}")

    return lead_duration


# ============================================================
# 片头 2：9+8 张交替推拉快闪（原 快闪版开头）
# ============================================================
def build_flash9(project, cfg, find_image, tts_engine, log):
    from pyJianYingDraft import KeyframeProperty as KP

    opening_cfg = cfg["opening"]
    image_dir = cfg["input"]["image_dir"]
    warned = set()

    log("\n[片头] 9 张交替推拉快闪（两轮）+ 引导语 + 音效")
    lead_duration = _lead_tts(project, cfg, tts_engine, log)
    _lead_caption(project, cfg, int(opening_cfg.get("lead_fallback_us", 1800000)))

    flash_duration = int(opening_cfg.get("flash_duration_us", 1667000))
    cuts = int(opening_cfg.get("flash_count", 9))
    repeat_first = int(opening_cfg.get("repeat_first", 8))
    step = flash_duration // max(cuts, 1)

    _add_sfx(project, cfg, flash_duration, log)

    images = pick_images(image_dir, cuts, opening_cfg.get("random_seed"))
    first_round_end = step * cuts
    log(f"  第一轮 {len(images)} 张，每张 {format_time(step)}；前 {repeat_first} 张叠加到第 {cuts} 张结束")

    def _push_pull(segment, zoom_in, keyframe_end):
        if zoom_in:
            segment.add_keyframe(KP.uniform_scale, 0, 1.0)
            segment.add_keyframe(KP.uniform_scale, keyframe_end, 1.34)
            segment.add_keyframe(KP.position_x, 0, -0.03)
            segment.add_keyframe(KP.position_x, keyframe_end, 0.03)
        else:
            segment.add_keyframe(KP.uniform_scale, 0, 1.34)
            segment.add_keyframe(KP.uniform_scale, keyframe_end, 1.0)
            segment.add_keyframe(KP.position_x, 0, 0.03)
            segment.add_keyframe(KP.position_x, keyframe_end, -0.03)

    for index, image_name in enumerate(images):
        image_path = find_image(image_dir, image_name)
        if not image_path:
            continue
        start = index * step
        duration = (first_round_end - start) if index < cuts - 1 else step
        segment = project.add_media_safe(
            media_path=image_path,
            start_time=start,
            duration=duration,
            track_name=f"Opening_Flash_{index + 1}",
        )
        if not segment:
            continue
        _push_pull(segment, index % 2 == 0, step if index < cuts - 1 else duration)
        effect_name = (opening_cfg.get("flash_effects") or ["泛光爆闪", "幻彩故障"])[index % 2]
        effect = _resolve_effect(effect_name, log, warned)
        if effect:
            try:
                segment.add_effect(effect)
            except Exception as exc:  # noqa: BLE001
                log(f"  ⚠ 快闪{index + 1} 特效失败: {str(exc)[:30]}")
        log(f"  ✓ 快闪{index + 1}/{cuts} {'推镜' if index % 2 == 0 else '拉镜'} {image_name}")

    for index in range(min(repeat_first, len(images))):
        image_path = find_image(image_dir, images[index])
        if not image_path:
            continue
        start = first_round_end + index * step
        duration = first_round_end - index * step
        segment = project.add_media_safe(
            media_path=image_path,
            start_time=start,
            duration=duration,
            track_name=f"Opening_Flash_R{index + 1}",
        )
        if not segment:
            continue
        _push_pull(segment, index % 2 == 0, step if index < repeat_first - 1 else duration)
        log(f"  ✓ 重复{index + 1}/{repeat_first} {images[index]}")

    total = first_round_end + step * repeat_first
    return max(lead_duration, total)


# ============================================================
# 片头 3：九宫格入场（原 九宫格开头）
# ============================================================
def build_grid(project, cfg, find_image, tts_engine, log, first_scene_image=None):
    from pyJianYingDraft import IntroType, KeyframeProperty as KP

    opening_cfg = cfg["opening"]
    image_dir = cfg["input"]["image_dir"]
    duration = int(opening_cfg.get("grid_duration_us", 2000000))

    log("\n[片头] 九宫格入场 + 标题 + 配音")
    images = pick_images(image_dir, int(opening_cfg.get("flash_count", 4)), opening_cfg.get("random_seed"))
    if first_scene_image:
        images.append(first_scene_image)
    if len(images) < 3:
        log("  ⚠ 图片不足，跳过片头")
        return 0

    scales = opening_cfg.get("grid_scales") or [1.0, 1.02, 1.01, 0.99, 1.03]
    animation = opening_cfg.get("intro_animation", "九宫格")

    for index, image_name in enumerate(images[: len(scales)]):
        image_path = find_image(image_dir, image_name)
        if not image_path:
            continue
        segment = project.add_media_safe(
            media_path=image_path, start_time=0, duration=duration, track_name=f"Opening_{index + 1}"
        )
        if not segment:
            continue
        segment.add_keyframe(KP.uniform_scale, 0, float(scales[index]))
        intro = getattr(IntroType, animation, None)
        if intro:
            try:
                segment.add_animation(intro)
                log(f"  ✓ 图片 {index + 1}: {image_name}（{animation}入场）")
                continue
            except Exception as exc:  # noqa: BLE001
                log(f"  ⚠ 图片 {index + 1}: {animation} 失败 {str(exc)[:30]}")
        log(f"  ✓ 图片 {index + 1}: {image_name}")

    actual = _lead_tts(project, cfg, tts_engine, log)
    _lead_caption(project, cfg, actual)
    return actual


_BUILDERS = {
    "mask_flash": build_mask_flash,
    "flash9": build_flash9,
    "grid": build_grid,
}


def build_opening(project, cfg, find_image, tts_engine, log, first_scene_image=None):
    """按配置里的 opening.style 生成片头，返回片头时长（微秒）。"""
    style = (cfg.get("opening") or {}).get("style", "mask_flash")
    builder = _BUILDERS.get(style)
    if builder is None:
        raise ValueError(f"未知片头风格: {style}（可选: {', '.join(sorted(_BUILDERS))}）")
    if style == "grid":
        return builder(project, cfg, find_image, tts_engine, log, first_scene_image)
    return builder(project, cfg, find_image, tts_engine, log)


def validate_opening_config(cfg, log):
    """启动时校验片头里用到的枚举名，避免"写了但没生效"。"""
    from pyJianYingDraft import IntroType, VideoSceneEffectType

    opening_cfg = cfg.get("opening") or {}
    names = list(opening_cfg.get("flash_effects") or [])
    if opening_cfg.get("intro_animation"):
        names.append(opening_cfg["intro_animation"])
    for name in names:
        if not (hasattr(VideoSceneEffectType, name) or hasattr(IntroType, name)):
            log(f"⚠ 配置里的特效/动画名不存在: {name}")
    return True
