#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""正片渲染：配音块 → 场景时长 → 字幕 → 图片 → Ken Burns → 首镜特效。

与旧脚本的差异
--------------
1. 字幕时长按"加权字数"分配，并且**总是**归一化到场景时长。
   旧「蒙版版」先按 4.4 字/秒 + 标点停顿估算，只在偏差 >10% 时才缩放，
   否则直接把估算值当真实值用 —— 字幕轨和配音轨就会越跑越偏。
2. 配音块之间可插入停顿（tts.group_gap_us）。
3. 特效/动画名从配置来，启动时校验，不再 `except: pass` 静默失效。
4. 全程收集结构化结果（每镜起止、字幕条目），可导出 SRT / JSON 报告。
"""

import re

from . import text as textmod
from .tts import format_time

_STRIP_RE = r'[，。！？、""\'\'\u2018\u2019：；…—\-–~/\s]'


def prepare_scenes(mappings, find_image, image_dir, cfg):
    """把对照表条目整理成场景列表；不可用的记进 failed。"""
    max_line = int((cfg.get("subtitle") or {}).get("max_chars_per_line", 22))
    scenes, failed = [], []
    for seq, caption, image_name in mappings:
        image_path = find_image(image_dir, image_name)
        if not image_path:
            failed.append({"seq": seq, "text": (caption or "")[:30], "reason": "图片缺失"})
            continue
        if not caption or len(textmod.plain(caption)) < 3:
            continue
        scenes.append(
            {
                "seq": seq,
                "text": caption,
                "image": image_name,
                "image_path": image_path,
                "chars": textmod.char_weight(caption),
                "subs": textmod.split_into_subtitles(caption, max_line),
            }
        )
    return scenes, failed


def synthesize_voice(project, scenes, tts_engine, cfg, start_time, log):
    """按配音块合成整片配音，把真实时长按加权字数分回每个场景。

    返回 (scene_durations, cursor_time, failed_seqs)
    """
    tts_cfg = cfg["tts"]
    group_chars = int(tts_cfg.get("group_chars", 180))
    max_chars = int(tts_cfg.get("max_chars", 300))
    gap = int(tts_cfg.get("group_gap_us", 0) or 0)

    groups = textmod.build_groups(scenes, group_chars)
    log(
        f"  📦 {len(scenes)} 个镜头合并为 {len(groups)} 个配音块"
        f"（每块≤{group_chars}字，一次合成保证语气连贯）"
    )

    scene_durations, failed_seqs = {}, []
    cursor = start_time

    for gi, group in enumerate(groups):
        group_text = "".join(scene["text"] for scene in group)
        span = (
            f"{group[0]['seq']:02d}-{group[-1]['seq']:02d}"
            if len(group) > 1
            else f"{group[0]['seq']:02d}"
        )
        log(f"  配音块 {gi + 1}/{len(groups)} [镜头 {span}] {len(group)}镜 - {group_text[:32]}…")

        block_start = cursor
        got = 0
        for chunk in textmod.split_text_for_tts(group_text, max_chars):
            segment, duration = tts_engine.synthesize(project, chunk, cursor)
            if segment or duration:
                got += 1
                cursor += duration
        if not got:
            failed_seqs.extend(scene["seq"] for scene in group)
            log(f"  ✗ 配音块 {gi + 1} 全部失败，跳过 {len(group)} 个镜头")
            continue

        block_duration = cursor - block_start
        log(f"    ✓ 合成 {format_time(block_duration)}（{got} 次请求 / {len(group)} 个镜头共用）")
        for scene, duration in zip(group, textmod.alloc(block_duration, [s["chars"] for s in group])):
            scene_durations[scene["seq"]] = duration

        if gap and gi < len(groups) - 1:
            cursor += gap

    _ = group_weight if False else None  # 保留变量名可读性
    return scene_durations, cursor, failed_seqs


def render_body(project, scenes, scene_durations, failed_seqs, cfg, start_time, log):
    """逐场景写字幕、放图片、加动画。返回 (current_time, results)。"""
    sub_cfg = cfg["subtitle"]
    body_cfg = cfg["body"]

    import pyJianYingDraft as draft
    from pyJianYingDraft import FontType, KeyframeProperty as KP, VideoSceneEffectType

    font = getattr(FontType, sub_cfg.get("font", "优设标题黑"), None)
    video_track = body_cfg.get("video_track", "VideoTrack")
    warned_effects = set()

    current_time = start_time
    results = []

    for scene in scenes:
        seq = scene["seq"]
        if seq in failed_seqs or seq not in scene_durations:
            log(f"  ✗ 场景 {seq:02d}: 配音失败，跳过")
            continue

        duration = scene_durations[seq]
        sentences = scene["subs"]
        weights = [max(1.0, textmod.char_weight(s)) for s in sentences]
        sub_durations = textmod.alloc(duration, weights)
        pairs = [(s, d) for s, d in zip(sentences, sub_durations) if d > 0]
        if not pairs:
            pairs = [(scene["text"], duration)]

        subtitle_start = current_time
        for index, (sentence, sub_duration) in enumerate(pairs):
            clean = re.sub(_STRIP_RE, "", sentence)
            is_first = not results and index == 0
            try:
                if is_first and sub_cfg.get("first_centered", True):
                    project.add_text_simple(
                        text=clean,
                        start_time=subtitle_start,
                        duration=sub_duration,
                        track_name=sub_cfg.get("track", "Subtitles"),
                        font=font,
                        style=draft.TextStyle(
                            size=float(sub_cfg.get("first_size", 6.0)),
                            letter_spacing=int(sub_cfg.get("letter_spacing", 1)),
                            color=tuple(sub_cfg.get("first_color", (1.0, 0.976, 0.945))),
                        ),
                        border=draft.TextBorder(color=(0.0, 0.0, 0.0), alpha=0.9, width=3.0),
                        clip_settings=draft.ClipSettings(transform_y=0.0),
                    )
                else:
                    kwargs = dict(
                        text=clean,
                        start_time=subtitle_start,
                        duration=sub_duration,
                        track_name=sub_cfg.get("track", "Subtitles"),
                        font=font,
                        style=draft.TextStyle(
                            size=float(sub_cfg.get("size", 5.0)),
                            letter_spacing=int(sub_cfg.get("letter_spacing", 1)),
                        ),
                        border=draft.TextBorder(
                            color=(0.0, 0.0, 0.0),
                            alpha=1.0,
                            width=float(sub_cfg.get("border_width", 40.0)),
                        ),
                        clip_settings=draft.ClipSettings(
                            transform_y=float(sub_cfg.get("transform_y", -0.8))
                        ),
                    )
                    if sub_cfg.get("background", True):
                        kwargs["background"] = draft.TextBackground(
                            color="#000000",
                            alpha=float(sub_cfg.get("background_alpha", 0.5)),
                            round_radius=float(sub_cfg.get("background_round", 0.35)),
                        )
                    project.add_text_simple(**kwargs)
                subtitle_start += sub_duration
            except Exception as exc:  # noqa: BLE001
                log(f"    ✗ 场景{seq:02d} 第{index + 1}句字幕失败: {str(exc)[:30]}")

        video_segment = project.add_media_safe(
            media_path=scene["image_path"],
            start_time=current_time,
            duration=duration,
            track_name=video_track,
        )

        if not results:
            _add_first_shot_effects(project, scene, cfg, current_time, log, warned_effects)

        if video_segment and body_cfg.get("ken_burns", True):
            _ken_burns(video_segment, seq, duration, KP)

        current_time += duration
        results.append(
            {
                "seq": seq,
                "image": scene["image"],
                "start_us": current_time - duration,
                "duration_us": duration,
                "text": scene["text"],
                "subtitles": [
                    {"text": re.sub(_STRIP_RE, "", s), "duration_us": d} for s, d in pairs
                ],
            }
        )
        log(f"  ✓ 场景 {seq:02d}: {format_time(duration)}（{len(pairs)}句字幕）")

    return current_time, results


def _ken_burns(segment, seq, duration, KP):
    """三套交替的推拉/平移动画。"""
    try:
        if seq % 3 == 0:
            segment.add_keyframe(KP.uniform_scale, 0, 1.0)
            segment.add_keyframe(KP.uniform_scale, duration, 1.15)
            segment.add_keyframe(KP.position_x, 0, -0.05)
            segment.add_keyframe(KP.position_x, duration, 0.05)
        elif seq % 3 == 1:
            segment.add_keyframe(KP.uniform_scale, 0, 1.15)
            segment.add_keyframe(KP.uniform_scale, duration, 1.0)
            segment.add_keyframe(KP.position_x, 0, 0.05)
            segment.add_keyframe(KP.position_x, duration, -0.05)
        else:
            segment.add_keyframe(KP.uniform_scale, 0, 1.0)
            segment.add_keyframe(KP.uniform_scale, duration, 1.1)
            segment.add_keyframe(KP.position_y, 0, 0.03)
            segment.add_keyframe(KP.position_y, duration, -0.03)
    except Exception:  # noqa: BLE001 - 动画失败退回基础缩放
        try:
            segment.add_keyframe(KP.uniform_scale, 0, 1.0)
            segment.add_keyframe(KP.uniform_scale, duration, 1.1)
        except Exception:  # noqa: BLE001
            pass


def _add_first_shot_effects(project, scene, cfg, current_time, log, warned):
    """首镜的冲击特效（幻彩故障 + 震动屏闪），持续 1 秒。"""
    from pyJianYingDraft import VideoSceneEffectType

    body_cfg = cfg["body"]
    names = body_cfg.get("first_shot_effects") or []
    if not names:
        return
    effect_segment = project.add_media_safe(
        media_path=scene["image_path"],
        start_time=current_time,
        duration=int(body_cfg.get("first_shot_effect_us", 1000000)),
        track_name=body_cfg.get("effect_track", "EffectTrack"),
    )
    if not effect_segment:
        return
    for name in names:
        effect = getattr(VideoSceneEffectType, name, None)
        if effect is None:
            if name not in warned:
                log(f"    ⚠ 特效不存在: {name}（已跳过）")
                warned.add(name)
            continue
        try:
            effect_segment.add_effect(effect)
            log(f"    ✓ 首镜特效: {name}")
        except Exception as exc:  # noqa: BLE001
            log(f"    ⚠ 首镜特效 {name} 失败: {str(exc)[:30]}")


def add_disclaimer(project, cfg, start_time, duration, log):
    """右下角叠甲文字。"""
    disclaimer_cfg = cfg.get("disclaimer") or {}
    if not disclaimer_cfg.get("enabled", True) or duration <= 0:
        return None
    import pyJianYingDraft as draft
    from pyJianYingDraft import FontType

    try:
        return project.add_text_simple(
            text=disclaimer_cfg.get("text", "虚构创作，与真实人物无关"),
            start_time=start_time,
            duration=duration,
            track_name=disclaimer_cfg.get("track", "DisclaimerTrack"),
            font=FontType.优设标题黑,
            style=draft.TextStyle(
                size=float(disclaimer_cfg.get("size", 3.0)),
                alpha=float(disclaimer_cfg.get("alpha", 0.8)),
                letter_spacing=1,
            ),
            clip_settings=draft.ClipSettings(
                transform_x=float(disclaimer_cfg.get("transform_x", -0.8)),
                transform_y=float(disclaimer_cfg.get("transform_y", 0.8)),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        log(f"  ⚠ 叠甲文字失败: {str(exc)[:40]}")
        return None


def add_bgm(project, cfg, duration, log):
    """背景音乐：剪映云曲库关键词或本地文件。默认关闭。"""
    bgm_cfg = cfg.get("bgm") or {}
    if not bgm_cfg.get("enabled", False) or duration <= 0:
        return None
    track = bgm_cfg.get("track", "BGM")
    try:
        if bgm_cfg.get("source") == "file" and bgm_cfg.get("file"):
            segment = project.add_media_safe(
                media_path=bgm_cfg["file"], start_time=0, duration=duration, track_name=track
            )
        else:
            segment = project.add_cloud_music(bgm_cfg.get("query", ""), duration_s=duration / 1e6, track_name=track)
        if segment is not None:
            try:
                segment.volume = float(bgm_cfg.get("volume", 0.15))
            except Exception:  # noqa: BLE001
                pass
            log(f"  ✓ BGM 已添加（{bgm_cfg.get('query') or bgm_cfg.get('file')}）")
        return segment
    except Exception as exc:  # noqa: BLE001
        log(f"  ⚠ BGM 添加失败: {str(exc)[:40]}")
        return None
