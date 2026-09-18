#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""文案处理：配音块切分、字幕短句切分、时长分配、ASR 原稿 → 对照表。

为什么要有"配音块"
------------------
剪映内置配音走 SAMI 流式接口（wss://sami.bytedance.com .../ws, namespace=TTS）。
按火山引擎官方文档（错误码 40402003 TTSExceededTextLimit）：
    · 流式 WebSocket 接口   上限 2000 个 UTF-8 字符
    · 非流式 HTTP 接口      上限 1000 个 UTF-8 字符
所以"25 字"从来不是接口限制。对照表平均每镜只有 30 来字，如果一镜一发 TTS，
88 个镜头就是 88 次独立合成，每次都重新起调、句尾收音降调，拼起来一顿一顿。
把**连续的多个镜头**合并成一个配音块（默认 ≤180 字）一次合成，语气才连贯；
再把这一块的真实时长按加权字数分回每个镜头、每句字幕。
"""

import re

# 标点/空白：切句和"净字数"统计都用它
_PUNCT_RE = r'[，。！？、""\'\'\u2018\u2019：；…—\-–~/、\s]'

# 单个配音块的目标字数（多个镜头合并）
DEFAULT_GROUP_CHARS = 180
# 单次 TTS 请求的字数上限（远低于 2000 的接口上限，留足余量）
DEFAULT_MAX_CHARS = 300
# 中文口播经验语速（1.05 倍速 ≈ 4.4 字/秒）
DEFAULT_CHARS_PER_SECOND = 4.4


# ============================================================
# 基础文本工具
# ============================================================
def plain(text):
    """去掉标点和空格，只留可发音的字。"""
    return re.sub(_PUNCT_RE, "", text or "")


def char_weight(text):
    """字幕时长权重：中文 1 分、ASCII（字母/数字）0.55 分、标点不计。

    旧脚本直接 `len(净字数)`，于是 "1/217" 这种会被当成 5 个字念，
    实际口播只读"二百一十七分之一"——权重估偏，字幕就飘。
    """
    weight = 0.0
    for ch in text or "":
        if re.match(_PUNCT_RE, ch):
            continue
        weight += 0.55 if ord(ch) < 128 else 1.0
    return max(weight, 1.0)


def split_text_for_tts(text, max_chars=DEFAULT_MAX_CHARS):
    """把长文案切成尽量少的 TTS 请求块（只有超长才切，优先切在句末标点）。"""
    text = re.sub(r"\s+", "", text or "")
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    units = [u for u in re.split(r"(?<=[。！？；…])", text) if u]

    refined = []
    for unit in units:
        if len(unit) <= max_chars:
            refined.append(unit)
            continue
        for part in [x for x in re.split(r"(?<=[，、,：:])", unit) if x]:
            while len(part) > max_chars:
                refined.append(part[:max_chars])
                part = part[max_chars:]
            if part:
                refined.append(part)

    chunks, buf = [], ""
    for unit in refined:
        if buf and len(buf) + len(unit) > max_chars:
            chunks.append(buf)
            buf = unit
        else:
            buf += unit
    if buf:
        chunks.append(buf)
    return [c for c in chunks if c.strip()]


def split_into_subtitles(text, max_chars_per_line=22):
    """按标点切成短句字幕；单句净字数超限时再切一刀。

    原「蒙版版」里这段逻辑写成 `if ' ，。' in sentence or ','`——
    一个恒真的条件（非空字符串永远为真），导致所有长句都走同一条分支。
    这里按净字数重写。
    """
    text_normalized = re.sub(r"\s*/\s*", "/", text or "")
    parts = re.split(r"([，。！？、/])", text_normalized)
    out, i = [], 0
    while i < len(parts):
        if parts[i].strip():
            sentence = parts[i].strip()
            if i + 1 < len(parts) and parts[i + 1] in "，。！？、/":
                sentence += parts[i + 1]
                i += 2
            else:
                i += 1
            if len(sentence) > 1:
                out.append(sentence)
        else:
            i += 1
    if not out:
        out = [text]

    if not max_chars_per_line:
        return out

    final = []
    for sentence in out:
        if len(plain(sentence)) <= max_chars_per_line:
            final.append(sentence)
            continue
        final.extend(_hard_split(sentence, max_chars_per_line))
    return final


def _hard_split(sentence, max_chars):
    """按净字数硬切长句（保留标点跟着前一段）。"""
    pieces, buf, count = [], "", 0
    for ch in sentence:
        buf += ch
        if not re.match(_PUNCT_RE, ch):
            count += 1
        if count >= max_chars:
            pieces.append(buf)
            buf, count = "", 0
    if buf.strip():
        pieces.append(buf)
    return pieces or [sentence]


def clean_for_tts(text):
    """送进 TTS 前的清理：去空格、合并连续句末标点、补句末标点。

    旧脚本只合并了连续的「。」，"真的吗？？？" 这种会原样送进 TTS。
    """
    cleaned = re.sub(r"\s+", "", text or "")
    cleaned = re.sub(r"([。！？…])\1+", r"\1", cleaned)
    if cleaned and not cleaned.endswith(("。", "！", "？", "…")):
        cleaned += "。"
    return cleaned


def estimate_duration_us(text, chars_per_second=DEFAULT_CHARS_PER_SECOND, speed=1.0):
    """离线估算时长（微秒）。dry-run / 单测 / TTS 不可用时的兜底。

    含标点停顿：逗号 0.15s、句末标点 0.3s。
    """
    body = plain(text)
    if not body:
        return 0
    weight = sum(0.55 if ord(c) < 128 else 1.0 for c in body)
    seconds = weight / max(chars_per_second, 0.5)
    seconds += (text or "").count("，") * 0.15 + (text or "").count("、") * 0.15
    seconds += sum((text or "").count(p) * 0.3 for p in "。！？")
    return int(seconds / max(speed, 0.5) * 1_000_000)


# ============================================================
# 时长分配
# ============================================================
def alloc(total_duration, weights):
    """按权重分配总时长（微秒），最后一份吃掉取整误差，保证求和无损。"""
    total_weight = sum(weights) or 1
    out, used = [], 0
    for i, weight in enumerate(weights):
        if i == len(weights) - 1:
            out.append(int(total_duration - used))
        else:
            value = int(total_duration * (weight / total_weight))
            out.append(value)
            used += value
    return out


def build_groups(scenes, group_chars=DEFAULT_GROUP_CHARS):
    """把连续场景合并成配音块。scenes 为 dict 列表，需含 'chars' 键。"""
    groups, cur, cur_chars = [], [], 0
    for scene in scenes:
        if cur and cur_chars + scene["chars"] > group_chars:
            groups.append(cur)
            cur, cur_chars = [], 0
        cur.append(scene)
        cur_chars += scene["chars"]
    if cur:
        groups.append(cur)
    return groups


# ============================================================
# ASR 原稿 → 对照表（对接 aavvcasdas/demo 的《作品》产出）
# ============================================================
# 句末语气词：以这些字收尾的行，多半是一句话说完了
_SENTENCE_END_CHARS = u"了的地吧呢啊吗呀嘛过着去来对"
# 以这些字收尾的行，是在引出下面那句话（"他说 / 老板娘道"），该用冒号
_QUOTE_LEAD_CHARS = u"说道喊问答叫"


def punctuate_lines(lines, joiner=u"，"):
    u"""给无标点的 ASR 原稿补标点（规则启发式，不需要模型）。

    demo 库《作品》的正文是 ASR 口播体：**一行一个气口、整篇无标点**。
    规则：
        · 行末是「说/道/喊/问」→ 打「：」，它在引出下一句
        · 行末是语气词（了/的/吧/啊…）→ 打「。」，一句说完
        · 其余 → 打「，」
    这是规则猜出来的，出片前请人工过一眼（shotlist 命令会明确提示）。
    """
    out = []
    for line in lines:
        line = (line or "").strip()
        if not line:
            continue
        if line[-1] in u"。！？…，、；：":
            out.append(line)
        elif line[-1] in _QUOTE_LEAD_CHARS:
            out.append(line + u"：")
        elif line[-1] in _SENTENCE_END_CHARS:
            out.append(line + u"。")
        else:
            out.append(line + joiner)
    return out


def close_sentence(text):
    u"""收尾：镜头/字幕末尾不能挂着逗号，换成句号。"""
    text = (text or "").rstrip()
    if not text:
        return text
    if text[-1] in u"。！？…":
        return text
    if text[-1] in u"，、；：":
        return text[:-1] + u"。"
    return text + u"。"


def looks_unpunctuated(raw_text, sample_lines=40):
    u"""判断原稿是不是 ASR 无标点体（决定要不要提示人工复核断句）。"""
    lines = [ln.strip() for ln in (raw_text or "").splitlines() if ln.strip()]
    if not lines:
        return False
    sample = lines[:sample_lines]
    with_punct = sum(1 for ln in sample if re.search(u"[，。！？、；：]$", ln))
    return with_punct < len(sample) * 0.3


def pack_sentences(sentences, target_chars=40, min_chars=12, max_chars=60):
    u"""把带标点的句子打包成镜头，尽量在句号处收口。"""
    shots, buf = [], ""
    for sentence in sentences:
        candidate = buf + sentence
        ends_sentence = sentence.endswith((u"。", u"！", u"？", u"…"))
        if buf and len(candidate) > max_chars:
            shots.append(buf)
            buf = sentence
        elif len(candidate) >= target_chars and ends_sentence:
            shots.append(candidate)
            buf = ""
        elif len(candidate) >= max_chars:
            shots.append(candidate)
            buf = ""
        else:
            buf = candidate
    if len(plain(buf)) >= min_chars or (buf and not shots):
        shots.append(buf)
    elif buf and shots:
        shots[-1] += buf  # 尾巴太短就并进上一镜，避免出现 1 秒的空镜
    # 被 max_chars 截断的镜头会挂着逗号，统一收成句号
    return [close_sentence(s) for s in shots if plain(s)]


def build_shotlist(raw_text, target_chars=40, min_chars=12, max_chars=60, lead_lines=3):
    u"""把 demo 库《作品》里的 ASR 口播原稿转成对照表条目。

    原稿特征（见 demo 仓库 作品/55_机修工油转电翻盘/正文.md）：
        · 无标点，一行一个气口
        · 开头 2–3 行是引导语（"今天体验的人生副本是 / 18岁辍学的机修工 / …"）
    产出：(lead_text, shots)，shots = [{'seq', 'text', 'image', 'chars'}]
    """
    lines = [ln.strip() for ln in (raw_text or "").splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]
    if not lines:
        return "", []

    lead, body = [], lines
    if lead_lines > 0:
        lead = lines[:lead_lines]
        body = lines[lead_lines:]

    shots = pack_sentences(
        punctuate_lines(body), target_chars, min_chars, max_chars
    )

    return " ".join(lead), [
        {"seq": idx, "text": text, "image": "", "chars": len(plain(text))}
        for idx, text in enumerate(shots, start=1)
    ]


def shotlist_to_mapping(shots, image_prefix="shot", image_ext="jpg", header=None):
    """对照表文本（格式：镜头编号 | 图片文件名 | 字幕内容）。"""
    if header is None:
        header = (
            f"镜头字幕对照表（共{len(shots)}镜，横屏16:9，配合口播使用）\n"
            "\n格式：镜头编号 | 图片文件名 | 字幕内容（对应口播）\n\n"
        )
    rows = []
    for shot in shots:
        image = shot.get("image") or f"{image_prefix}_{shot['seq']:02d}.{image_ext}"
        rows.append(f"{shot['seq']:02d} | {image} | {shot['text']}")
    return header + "\n".join(rows) + "\n"


def shotlist_to_image_prompts(shots, style="", image_prefix="shot", image_ext="jpg"):
    """每镜一条出图提示词（喂给 SD / comfyUI / MoneyPrinterPlus 的 AI 生图）。"""
    rows = ["# 出图提示词（每镜一条）", f"# 风格统一前缀：{style}", ""]
    for shot in shots:
        image = shot.get("image") or f"{image_prefix}_{shot['seq']:02d}.{image_ext}"
        scene = plain(shot["text"])
        rows.append(f"{shot['seq']:02d}\t{image}\t{style}，画面内容：{scene}")
    return "\n".join(rows) + "\n"
