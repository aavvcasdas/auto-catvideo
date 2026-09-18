#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""配音引擎：重试 / 语速 / 磁盘缓存 / 离线估算。

关于语速（1.05 倍）
------------------
`add_media_safe` 建出来的音频片段 source_timerange == target_timerange、speed=1.0。
把 speed 改成 1.05 之后，剪映实际占用时间轴的长度是 `source / speed`，
所以必须同步把 target_timerange 缩到 `source / speed`，两边才对得上。
旧脚本就是这么做的（实测 source=2.5s / speed=1.05 → 时间轴 2380952us，
与 target 完全一致），这里保留该行为并集中到一处，另加一条断言防止以后改坏。

关于缓存
--------
88 个镜头 = 几十次 SAMI 请求，改一次字幕样式就要重跑全部配音，
一次失败（SAMI 偶发抽风 / 断网）就得从头再来。这里按
`md5(文本|音色|语速)` 落盘缓存 ogg，命中直接复用本地文件。
"""

import hashlib
import os
import shutil
import time

from . import text as textmod


def format_time(microseconds):
    """微秒 → 'm:ss.f'。"""
    seconds = (microseconds or 0) / 1_000_000
    return f"{int(seconds // 60)}:{seconds % 60:04.1f}"


def cache_key(text, speaker, speed):
    raw = f"{text}|{speaker}|{speed}".encode("utf-8")
    return hashlib.md5(raw).hexdigest()


class TTSError(RuntimeError):
    pass


class TTSEngine:
    """统一配音入口。

    backend:
        sami      剪映内置 SAMI（默认，需要网络 + 剪映设备号）
        edge      edge-tts 兜底
        estimate  不合成，只按字数估时长（dry-run / 单元测试用）
    """

    def __init__(self, cfg, log=None):
        self.cfg = cfg or {}
        self.log = log or (lambda msg: print(msg))
        self.backend = self.cfg.get("backend", "sami")
        self.speaker = self.cfg.get("speaker", "zh_male_huoli")
        self.speed = float(self.cfg.get("speed", 1.0) or 1.0)
        self.retries = max(1, int(self.cfg.get("retries", 3)))
        self.retry_wait = float(self.cfg.get("retry_wait", 1.0))
        self.cache_enabled = bool(self.cfg.get("cache", True)) and self.backend != "estimate"
        self.cache_dir = self.cfg.get("cache_dir") or os.path.join(os.getcwd(), ".cache", "tts")
        self.track = self.cfg.get("track", "VoiceOver")
        self.allow_fallback = bool(self.cfg.get("allow_fallback", False))
        self.fade_in = int(self.cfg.get("fade_in_us", 0) or 0)
        self.fade_out = int(self.cfg.get("fade_out_us", 0) or 0)
        self.cps = float(self.cfg.get("estimate_chars_per_second", 4.4))
        self.audio_effect = self.cfg.get("audio_effect")
        self.missing_effects = set()
        self.stats = {"requests": 0, "cache_hits": 0, "failures": 0, "estimate": 0}

        if self.cache_enabled:
            os.makedirs(self.cache_dir, exist_ok=True)

    # -------------------------------------------------- 对外主接口
    def synthesize(self, project, text, start_time, track_name=None):
        """合成一段配音并放到时间轴上。返回 (segment, duration_us)。失败返回 (None, 0)。"""
        cleaned = textmod.clean_for_tts(text)
        if not cleaned:
            return None, 0
        track_name = track_name or self.track

        if self.backend == "estimate":
            return self._synthesize_estimate(project, cleaned, start_time, track_name)

        cached = self._cache_path(cleaned)
        if cached and os.path.exists(cached):
            self.stats["cache_hits"] += 1
            segment = project.add_media_safe(
                media_path=cached, start_time=start_time, track_name=track_name
            )
            if segment:
                return self._postprocess(segment)
            self.log(f"      ⚠ 缓存文件无法入轨: {os.path.basename(cached)}")

        for attempt in range(self.retries):
            self.stats["requests"] += 1
            try:
                segment = project.add_tts_intelligent(
                    text=cleaned,
                    speaker=self.speaker,
                    start_time=start_time,
                    track_name=track_name,
                    tts_backend=None if self.backend == "auto" else self.backend,
                    allow_fallback=self.allow_fallback,
                )
            except Exception as exc:  # noqa: BLE001 - TTS 失败必须兜住，不能中断整条流水线
                segment = None
                self.log(f"      重试 {attempt + 1}/{self.retries}（异常: {str(exc)[:40]}）")
            if segment:
                if cached:
                    self._store_cache(segment, cached)
                return self._postprocess(segment)
            if attempt < self.retries - 1:
                time.sleep(self.retry_wait)

        self.stats["failures"] += 1
        self.log(f"      ✗ 配音最终失败（{self.retries} 次重试）: {cleaned[:20]}…")
        return None, 0

    # -------------------------------------------------- 内部实现
    def _synthesize_estimate(self, project, cleaned, start_time, track_name):
        """离线模式：用项目里现成的 audio/coding.WAV 当替身素材，时长仍按真实时长走。

        这样 dry-run 也能产出结构完整、时间轴自洽的草稿，用来验证流水线，
        不需要网络、不烧 TTS 配额。
        """
        duration = textmod.estimate_duration_us(cleaned, self.cps, self.speed)
        self.stats["estimate"] += 1

        standin = self.cfg.get("_standin_audio")
        segment = None
        if standin and os.path.exists(standin):
            segment = project.add_media_safe(
                media_path=standin, start_time=start_time, track_name=track_name
            )
            if segment:
                from pyJianYingDraft.time_util import Timerange

                segment.target_timerange = Timerange(segment.target_timerange.start, duration)
                if segment.source_timerange:
                    segment.source_timerange = Timerange(0, duration)
                segment.speed.speed = 1.0
        return segment, duration

    def _postprocess(self, segment):
        """统一处理语速、特效、淡入淡出，返回 (segment, 时间轴实际时长)。"""
        from pyJianYingDraft.time_util import Timerange

        source_us = segment.source_timerange.duration if segment.source_timerange else 0
        if self.speed and abs(self.speed - 1.0) > 1e-6:
            segment.speed.speed = self.speed
            duration = int(round(source_us / self.speed)) if source_us else int(
                segment.target_timerange.duration / self.speed
            )
            segment.target_timerange = Timerange(segment.target_timerange.start, duration)
        else:
            duration = segment.target_timerange.duration

        # 断言：时间轴占用长度必须等于 source / speed，否则画面和配音会越跑越偏
        if source_us:
            implied = int(round(source_us / (segment.speed.speed or 1.0)))
            if abs(implied - segment.target_timerange.duration) > 2000:
                raise TTSError(
                    f"语速换算不自洽: source={source_us} speed={segment.speed.speed} "
                    f"target={segment.target_timerange.duration} implied={implied}"
                )

        if self.audio_effect:
            self._apply_audio_effect(segment)
        if self.fade_in or self.fade_out:
            try:
                segment.add_fade(self.fade_in, self.fade_out)
            except Exception as exc:  # noqa: BLE001
                self.log(f"      ⚠ 淡入淡出失败: {str(exc)[:40]}")
        return segment, segment.target_timerange.duration

    def _apply_audio_effect(self, segment):
        """人声增强等音频特效。枚举名写错时只警告一次，不再静默吞掉。"""
        try:
            import pyJianYingDraft as draft

            effect = getattr(draft.AudioSceneEffectType, self.audio_effect, None)
            if effect is None:
                if self.audio_effect not in self.missing_effects:
                    self.log(f"      ⚠ 音频特效不存在: {self.audio_effect}（已忽略）")
                    self.missing_effects.add(self.audio_effect)
                return
            segment.add_effect(effect)
        except Exception as exc:  # noqa: BLE001
            self.log(f"      ⚠ 音频特效失败: {str(exc)[:40]}")

    # -------------------------------------------------- 缓存
    def _cache_path(self, cleaned_text):
        if not self.cache_enabled:
            return None
        return os.path.join(self.cache_dir, f"{cache_key(cleaned_text, self.speaker, self.speed)}.ogg")

    def _store_cache(self, segment, target_path):
        """把刚合成的音频拷进缓存目录（素材路径藏在 material_instance 里）。"""
        try:
            material = getattr(segment, "material_instance", None)
            source_path = getattr(material, "path", None)
            if source_path and os.path.exists(source_path):
                shutil.copyfile(source_path, target_path)
        except Exception as exc:  # noqa: BLE001 - 缓存失败不该影响出片
            self.log(f"      ⚠ 缓存写入失败: {str(exc)[:40]}")
