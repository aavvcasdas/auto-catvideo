#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
跨场景配音块合并（所有 create_video_final*.py 共用）

背景
----
剪映内置配音走 SAMI 流式接口（wss://sami.bytedance.com .../ws, namespace=TTS）。
按火山引擎官方文档（错误码 40402003 TTSExceededTextLimit）：
    · 流式 WebSocket 接口   上限 2000 个 UTF-8 字符
    · 非流式 HTTP 接口      上限 1000 个 UTF-8 字符
所以「25 字」从来不是接口限制。

问题
----
对照表平均每镜只有 30 来字，如果按「每个场景各发一次 TTS」，88 个镜头
就是 88 次独立合成。每次合成都会重新起调、句尾收音降调，拼起来听感
一顿一顿。这就是"配音很奇怪"的根源。

做法
----
把**连续的多个场景**合并成一个配音块（默认 ≤180 字）一次性合成，
再把这一块的真实时长按字数比例分回给每个场景、每句字幕。
字幕依然是按标点切出来的短句，逐句上屏，不受影响。
"""

import re

# 单个配音块的目标字数（多个场景合并）。块越大语气越连贯，
# 但画面切换点是按字数估算的，块太大可能出现零点几秒的偏差。
TTS_GROUP_CHARS = 180

# 单次 TTS 请求的字数上限（远低于 2000 的接口上限，留足余量）
TTS_MAX_CHARS = 300

_PUNCT_RE = r'[，。！？、"" \u2018\u2019：；…——/\s]'


def plain(text):
    """去掉标点和空格，只留可发音的字"""
    return re.sub(_PUNCT_RE, '', text or '')


def split_text_for_tts(text, max_chars=TTS_MAX_CHARS):
    """把文案切成尽量少的 TTS 请求块（只有超长时才切，优先切在句末标点）"""
    text = re.sub(r'\s+', '', text or '')
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    units = [u for u in re.split(r'(?<=[。！？；…])', text) if u]

    refined = []
    for u in units:
        if len(u) <= max_chars:
            refined.append(u)
            continue
        for s in [x for x in re.split(r'(?<=[，、,：:])', u) if x]:
            while len(s) > max_chars:
                refined.append(s[:max_chars])
                s = s[max_chars:]
            if s:
                refined.append(s)

    chunks, buf = [], ''
    for u in refined:
        if buf and len(buf) + len(u) > max_chars:
            chunks.append(buf)
            buf = u
        else:
            buf += u
    if buf:
        chunks.append(buf)
    return [c for c in chunks if c.strip()]


def split_into_subtitles(text):
    """把一段文案按标点切成短句字幕（保留短句，便于逐句上屏）"""
    text_normalized = re.sub(r'\s*/\s*', '/', text)
    parts = re.split(r'([，。！？、/])', text_normalized)
    out, i = [], 0
    while i < len(parts):
        if parts[i].strip():
            sentence = parts[i].strip()
            if i + 1 < len(parts) and parts[i + 1] in '，。！？、/':
                sentence += parts[i + 1]
                i += 2
            else:
                i += 1
            if len(sentence) > 1:
                out.append(sentence)
        else:
            i += 1
    return out or [text]


def clean_for_tts(t):
    """送进 TTS 前的文本清理"""
    t = t.replace(' ', '')
    t = re.sub(r'。{2,}', '。', t)
    if not t.endswith(('。', '！', '？', '…')):
        t += '。'
    return t


def alloc(total_duration, weights):
    """按权重分配总时长，最后一份吃掉取整误差（保证求和无损）"""
    s = sum(weights) or 1
    out, used = [], 0
    for i, w in enumerate(weights):
        if i == len(weights) - 1:
            out.append(total_duration - used)
        else:
            d = int(total_duration * (w / s))
            out.append(d)
            used += d
    return out


def build_groups(scenes, group_chars=TTS_GROUP_CHARS):
    """把连续场景合并成配音块。scenes 为 dict 列表，需含 'chars' 键。"""
    groups, cur, cur_chars = [], [], 0
    for sc in scenes:
        if cur and cur_chars + sc['chars'] > group_chars:
            groups.append(cur)
            cur, cur_chars = [], 0
        cur.append(sc)
        cur_chars += sc['chars']
    if cur:
        groups.append(cur)
    return groups


def prepare_scenes(mappings, find_image_file, image_dir, failed_segments=None):
    """预处理场景：找图、算字数、切字幕短句。返回可用场景列表。"""
    scenes = []
    for seq, text, img_filename in mappings:
        img_path = find_image_file(image_dir, img_filename)
        if not img_path:
            if failed_segments is not None:
                failed_segments.append((seq, text[:30] if text else "空文案", "图片缺失"))
            continue
        if not text or len(text) < 3 or not plain(text):
            continue
        scenes.append({
            'seq': seq,
            'text': text,
            'img': img_filename,
            'img_path': img_path,
            'chars': len(plain(text)),
            'subs': split_into_subtitles(text),
        })
    return scenes


def synthesize_groups(project, scenes, speaker, start_time,
                      generate_tts_with_retry, format_time,
                      group_chars=TTS_GROUP_CHARS, track_name="VoiceOver",
                      verbose=True):
    """按配音块合成，并把时长分回每个场景。

    返回 (scene_durations, total_duration, failed_seqs)
      scene_durations: {seq: 该场景分得的时长(微秒)}
    """
    groups = build_groups(scenes, group_chars)
    if verbose:
        print(f"  📦 {len(scenes)} 个场景合并为 {len(groups)} 个配音块"
              f"（目标每块≤{group_chars}字，一次合成保证语气连贯）\n")

    scene_durations = {}
    failed_seqs = []
    cursor_time = start_time

    for gi, group in enumerate(groups):
        group_text = ''.join(sc['text'] for sc in group)
        group_chars_n = sum(sc['chars'] for sc in group)
        rng = (f"{group[0]['seq']:02d}-{group[-1]['seq']:02d}"
               if len(group) > 1 else f"{group[0]['seq']:02d}")
        if verbose:
            print(f"  配音块 {gi+1}/{len(groups)} [场景 {rng}] "
                  f"{len(group)}镜 {group_chars_n}字 - {group_text[:40]}...")

        chunks = split_text_for_tts(group_text, TTS_MAX_CHARS)
        got = []
        block_start = cursor_time
        for chunk in chunks:
            audio_seg, seg_duration = generate_tts_with_retry(
                project, clean_for_tts(chunk), speaker, cursor_time, track_name
            )
            if audio_seg:
                got.append(seg_duration)
                cursor_time += seg_duration
            elif verbose:
                print(f"      X 配音块内某段失败: {chunk[:20]}")

        if not got:
            for sc in group:
                failed_seqs.append(sc['seq'])
            if verbose:
                print(f"  X 配音块 {gi+1} 配音失败，跳过 {len(group)} 个场景")
            continue

        group_duration = cursor_time - block_start
        if verbose:
            print(f"    ✓ 一次合成 {format_time(group_duration)} "
                  f"({len(got)}次请求，{len(group)}个镜头共用)")

        for sc, d in zip(group, alloc(group_duration, [s['chars'] for s in group])):
            scene_durations[sc['seq']] = d

    return scene_durations, cursor_time - start_time, failed_seqs
