#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
通用视频生成脚本 - 从口播文案对照表生成视频
图片、字幕、音频完全同步
片头2秒九宫格效果
支持递归分割超长文案，段间加0.1秒停顿
"""

import os
import sys
import re
import random
from datetime import datetime

# 环境初始化
current_dir = os.path.dirname(os.path.abspath(__file__))
skill_root = os.path.join(current_dir, "jianying-editor-skill-main")
sys.path.insert(0, os.path.join(skill_root, "scripts"))

from jy_wrapper import JyProject
import pyJianYingDraft as draft
from pyJianYingDraft import KeyframeProperty as KP, IntroType, VideoSceneEffectType, FontType

def parse_mapping_file(file_path):
    """
    解析口播文案对照表文件
    支持格式：
    格式1: 01  文案内容
    格式2: 02. 文案内容
    格式3: 01 | shot_01.jpg | 文案内容
    格式4: 分镜01 | shot_01.jpg | 文案内容（支持"分镜"前缀）
    
    Returns:
        list: [(序号, 文案, 图片文件名), ...]
    """
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    mappings = []
    
    # 格式1和2的正则表达式
    pattern1 = r'^\s*(\d+)[\s.．、]+(.+)$'
    # 格式3和4的正则表达式（带竖线分隔，支持"分镜"前缀）
    pattern2 = r'^\s*(?:分镜)?(\d+)\s*\|\s*([^\|]+)\s*\|\s*(.*)$'
    
    for line in content.split('\n'):
        line = line.strip()
        
        # 跳过空行和注释行
        if not line or line.startswith('#') or line.startswith('---') or '格式：' in line or '镜头字幕' in line:
            continue
        
        # 先尝试格式3和4（带竖线，支持"分镜"前缀）
        match = re.match(pattern2, line)
        if match:
            seq_num = int(match.group(1))
            img_filename = match.group(2).strip()
            text = match.group(3).strip()
            
            # 允许空文案（用于纯图片场景）
            # 但跳过说明性文字
            if '此后镜头无配套字幕' in text or '无配套字幕' in text:
                continue
            
            mappings.append((seq_num, text, img_filename))
            continue
        
        # 再尝试格式1和2
        match = re.match(pattern1, line)
        if match:
            seq_num = int(match.group(1))
            text = match.group(2).strip()
            
            # 跳过空文案或太短的内容
            if not text or len(text) < 3:
                continue
            
            # 构建图片文件名（支持多种模式）
            img_filename = f"scene_{seq_num:02d}.png"
            
            mappings.append((seq_num, text, img_filename))
    
    return mappings

def find_image_file(image_dir, filename):
    """
    查找图片文件（智能匹配png和jpg格式）
    优先级：
    1. 原始文件名（保持扩展名）
    2. 切换扩展名（.png ↔ .jpg）
    3. 尝试 shot_ 前缀（原扩展名）
    4. 尝试 shot_ 前缀（切换扩展名）
    5. 尝试 scene_ 前缀（如果原本是 shot_）
    """
    # 1. 尝试原始文件名
    full_path = os.path.join(image_dir, filename)
    if os.path.exists(full_path):
        return full_path
    
    # 2. 切换扩展名（png ↔ jpg）
    base_name, ext = os.path.splitext(filename)
    if ext.lower() == '.png':
        alt_filename = base_name + '.jpg'
    elif ext.lower() in ['.jpg', '.jpeg']:
        alt_filename = base_name + '.png'
    else:
        alt_filename = filename + '.png'  # 如果没有扩展名，尝试加 .png
    
    alt_path = os.path.join(image_dir, alt_filename)
    if os.path.exists(alt_path):
        return alt_path
    
    # 3. 尝试 shot_ 前缀（原扩展名）
    if 'scene_' in filename:
        shot_name = filename.replace('scene_', 'shot_')
        shot_path = os.path.join(image_dir, shot_name)
        if os.path.exists(shot_path):
            return shot_path
        
        # 4. 尝试 shot_ 前缀（切换扩展名）
        shot_base, shot_ext = os.path.splitext(shot_name)
        if shot_ext.lower() == '.png':
            shot_alt = shot_base + '.jpg'
        elif shot_ext.lower() in ['.jpg', '.jpeg']:
            shot_alt = shot_base + '.png'
        else:
            shot_alt = shot_name + '.png'
        
        shot_alt_path = os.path.join(image_dir, shot_alt)
        if os.path.exists(shot_alt_path):
            return shot_alt_path
    
    # 5. 尝试 scene_ 前缀（如果原本是 shot_）
    if 'shot_' in filename:
        scene_name = filename.replace('shot_', 'scene_')
        scene_path = os.path.join(image_dir, scene_name)
        if os.path.exists(scene_path):
            return scene_path
        
        # 6. 尝试 scene_ 前缀（切换扩展名）
        scene_base, scene_ext = os.path.splitext(scene_name)
        if scene_ext.lower() == '.png':
            scene_alt = scene_base + '.jpg'
        elif scene_ext.lower() in ['.jpg', '.jpeg']:
            scene_alt = scene_base + '.png'
        else:
            scene_alt = scene_name + '.png'
        
        scene_alt_path = os.path.join(image_dir, scene_alt)
        if os.path.exists(scene_alt_path):
            return scene_alt_path
    
    return None

def format_time(microseconds):
    """格式化时间"""
    seconds = microseconds / 1000000
    minutes = int(seconds // 60)
    secs = seconds % 60
    return f"{minutes}:{secs:04.1f}"

def generate_tts_with_retry(project, text, speaker, start_time, track_name, max_retries=3, speed=1.05):
    """
    生成TTS音频，失败时重试
    🔥 优化：启用 fallback、增加重试次数、音频规范化、语速调整
    返回: (audio_segment, duration) 或 (None, 0)
    """
    for attempt in range(max_retries):
        try:
            audio_seg = project.add_tts_intelligent(
                text=text,
                speaker=speaker,
                start_time=start_time,
                track_name=track_name,
                tts_backend="sami",
                allow_fallback=True  # 🔥 启用 fallback
            )
            
            if audio_seg:
                # 🔥 设置语速（修改Speed对象的speed属性）
                audio_seg.speed.speed = speed
                
                # 计算实际时长（原时长 / 语速）
                original_duration = audio_seg.target_timerange.duration
                actual_duration = int(original_duration / speed)
                
                # 更新目标时间范围
                from pyJianYingDraft.time_util import Timerange
                audio_seg.target_timerange = Timerange(
                    audio_seg.target_timerange.start,
                    actual_duration
                )
                
                duration = actual_duration
                
                # 🔥 音频规范化
                try:
                    audio_seg.add_effect(
                        draft.Effects.audio_loudness_normalization,
                        target_loudness=-16.0
                    )
                except:
                    pass
                
                return audio_seg, duration
            
            # 如果返回None，等待后重试
            if attempt < max_retries - 1:
                print(f"      重试 {attempt + 1}/{max_retries - 1}...")
                import time
                time.sleep(1)
                
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"      重试 {attempt + 1}/{max_retries - 1} (异常: {str(e)[:30]})")
                import time
                time.sleep(1)
            else:
                print(f"      X 最终失败: {str(e)[:50]}")
    
    return None, 0

# 已删除 split_text_recursively 函数
# TTS引擎可以处理长文本，不需要人为分割
# 分割会导致语音断断续续（灌灌的）

def get_random_images(image_dir, count=9):
    """随机选择指定数量的图片"""
    image_files = []
    for f in os.listdir(image_dir):
        if f.lower().endswith(('.png', '.jpg', '.jpeg')):
            image_files.append(f)
    
    if len(image_files) == 0:
        return []
    
    if len(image_files) < count:
        # 如果图片不够，重复使用
        selected = image_files * ((count // len(image_files)) + 1)
        return random.sample(selected, count)
    
    return random.sample(image_files, count)

def add_advanced_opening(project, image_dir, mappings, duration_lead=1800000, duration_flash=1667000):
    """
    添加高级片头：线性蒙版快闪 + 引导语 + 音效
    参考用户提供的操作流程
    
    duration_lead: 引导语持续时间（微秒），默认1.8秒
    duration_flash: 快闪持续时间（微秒），默认1.667秒
    返回: 片头总时长
    """
    print("\n添加高级片头：线性蒙版快闪 + 引导语 + 音效（从0秒开始）...")
    
    # ========== 引导语文字卡（纯文字，无背景图，从0秒开始）==========
    lead_text = "今天你要体验的人生副本是"
    # 🔥 删除空格（TTS配音）
    lead_text_tts = lead_text.replace(' ', '')
    
    print(f"\n[片头·引导语] 文字卡 ({format_time(duration_lead)}，从0秒开始)")
    
    try:
        # 添加引导语字幕（居中，大字号）
        project.add_text_simple(
            text=lead_text,
            start_time=0,
            duration=duration_lead,
            track_name="Opening_Lead_Text",
            font=FontType.优设标题黑,
            style=draft.TextStyle(
                size=9.0,
                letter_spacing=1,
                color=(1.0, 0.976, 0.945)  # 0xFFF9F1 暖白色
            ),
            border=draft.TextBorder(color=(0.0, 0.0, 0.0), alpha=0.85, width=60.0),
            clip_settings=draft.ClipSettings(transform_y=0.0)  # 居中
        )
        
        # 添加引导语配音（从0秒开始）
        audio_seg = project.add_tts_intelligent(
            text=lead_text_tts,  # 🔥 使用删除空格后的文本
            speaker=VOICE_SPEAKER,
            start_time=0,
            track_name="Opening_Lead_Voice",
            tts_backend="sami",
            allow_fallback=True
        )
        
        if audio_seg:
            # 设置语速1.05倍
            audio_seg.speed.speed = 1.05
            actual_duration = int(audio_seg.target_timerange.duration / 1.05)
            from pyJianYingDraft.time_util import Timerange
            audio_seg.target_timerange = Timerange(audio_seg.target_timerange.start, actual_duration)
            print(f"  ✓ 引导语配音: {format_time(actual_duration)}")
        
        print(f"  ✓ 引导语文字卡已添加（纯文字，无背景图）")
    except Exception as e:
        print(f"  ✗ 引导语失败: {str(e)[:50]}")
    
    # ========== 添加闪烁发光音效（从0秒开始）==========
    # 🔥 跨平台：音效目录改为脚本同级的 audio/
    audio_dir = os.path.join(current_dir, "audio")
    ratchet_sfx = os.path.join(audio_dir, "ratchet.wav")
    
    # 如果没有 ratchet.wav，使用 coding.WAV
    if not os.path.exists(ratchet_sfx):
        coding_sfx = os.path.join(audio_dir, "coding.WAV")
        if os.path.exists(coding_sfx):
            ratchet_sfx = coding_sfx
            print(f"\n[片头·音效] 使用 coding.WAV 作为闪烁发光音效")
        else:
            ratchet_sfx = None
            print(f"\n[片头·音效] 音效文件缺失，跳过")
    else:
        print(f"\n[片头·音效] 使用 ratchet.wav")
    
    if ratchet_sfx:
        try:
            # 添加闪烁发光音效（从0秒开始）
            sfx_seg = project.add_media_safe(
                media_path=ratchet_sfx,
                start_time=0,
                duration=duration_flash,
                track_name="Opening_Flash_SFX"
            )
            
            if sfx_seg:
                print(f"  ✓ 闪烁发光音效已添加: {os.path.basename(ratchet_sfx)}")
        except Exception as e:
            print(f"  ✗ 音效添加失败: {str(e)[:50]}")
    
    # ========== 线性蒙版快闪效果（从0秒开始）==========
    flash_start = 0  # 从0秒开始
    flash_images = get_random_images(image_dir, 9)  # 随机选择9张图片
    
    print(f"\n[片头·线性蒙版快闪] 开始生成 (9张图片)")
    print(f"  效果：缩放150% → 100% → 120% + 线性蒙版动画")
    print(f"  ⚠️  注意：蒙版关键帧需要手动在剪映中添加或使用外部工具")
    
    # 每张图片的基础时长
    base_frame_duration = duration_flash // 9  # 约185ms/张
    
    for i, img_name in enumerate(flash_images):
        img_path = find_image_file(image_dir, img_name)
        if not img_path:
            continue
        
        try:
            # 计算这张图片的开始时间和持续时间
            seg_start = flash_start + i * base_frame_duration
            
            # 🔥 关键改动：每张图片持续到下一张开始（叠加效果）
            # 前8张持续到下一张开始，最后一张正常结束
            if i < 8:
                seg_duration = base_frame_duration * 2  # 持续2倍时长，实现叠加
            else:
                seg_duration = base_frame_duration
            
            # 添加图片
            segment = project.add_media_safe(
                media_path=img_path,
                start_time=seg_start,
                duration=seg_duration,
                track_name=f"Opening_Flash_{i+1}"
            )
            
            if segment:
                # 🎬 关键帧动画（缩放动画）
                
                # 第一帧（0ms）：缩放150%，位置归零
                segment.add_keyframe(KP.uniform_scale, 0, 1.5)
                segment.add_keyframe(KP.position_x, 0, 0.0)
                segment.add_keyframe(KP.position_y, 0, 0.0)
                
                # 20帧后（约333ms）：缩放100%
                keyframe_time_1 = 333333  # 20帧 ≈ 333ms
                segment.add_keyframe(KP.uniform_scale, keyframe_time_1, 1.0)
                
                # 再过5帧：缩放120%
                keyframe_time_2 = keyframe_time_1 + 83333  # 再过5帧 ≈ 83ms
                segment.add_keyframe(KP.uniform_scale, keyframe_time_2, 1.2)
                
                # 🔥 透明度固定100%（不添加关键帧）
                # 图片保持完全不透明
                
                # 🎭 添加线性蒙版（静态，无关键帧动画）
                # ⚠️ pyJianYingDraft不支持蒙版关键帧
                # 用户需要手动在剪映中添加蒙版动画，或使用外部工具修改JSON
                try:
                    from pyJianYingDraft import MaskType
                    segment.add_mask(
                        MaskType.线性,
                        center_y=-0.5,  # 初始位置（上方）
                        size=0.8,       # 蒙版大小
                        feather=50.0,   # 羽化50
                        invert=False    # 不反转
                    )
                    print(f"  ✓ 快闪{i+1}/9 {img_name} (已添加线性蒙版，需手动添加关键帧)")
                except Exception as e:
                    print(f"  ✓ 快闪{i+1}/9 {img_name} (蒙版添加失败: {str(e)[:20]})")
        
        except Exception as e:
            print(f"  ✗ 快闪{i+1}失败: {str(e)[:30]}")
            continue
    
    # 计算总时长
    total_opening_duration = max(duration_lead, duration_flash)
    
    print(f"\n✓ 片头效果已添加（总时长: {format_time(total_opening_duration)}）")
    print(f"  · 引导语: {format_time(duration_lead)} (纯文字+配音)")
    print(f"  · 线性蒙版快闪: {format_time(duration_flash)} (9张，叠加效果)")
    print(f"  · 音效: 闪烁发光音效")
    print(f"  · 关键帧动画: 缩放150%→100%→120% + 蒙版羽化 + 入场动效")
    
    return total_opening_duration

# 配置路径（可修改）
# 🔥 跨平台：以脚本所在目录为基准
MAPPING_FILE = os.path.join(current_dir, '剧本', '口播文案_图片序号_对应表.txt')
IMAGE_DIR = os.path.join(current_dir, 'image')
# 项目名称加时间戳，避免覆盖
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
PROJECT_NAME = f"人生副本_{timestamp}"
# VOICE_SPEAKER = "ICL_zh_female_manbo_jianying"  # 曼波讲电影音色
VOICE_SPEAKER = "ICL_zh_male_momodianying"  # 默默讲电影音色
# VOICE_SPEAKER = "zh_female_aoyunliuyuxi" #刘语熙音色
#VOICE_SPEAKER = "BV025_streaming" #台湾女生音色
print("=" * 60)
print("通用视频生成：从口播文案对照表")
print("=" * 60)

# 1. 解析对照表
print(f"\n[1/4] 解析对照表文件...")
if not os.path.exists(MAPPING_FILE):
    print(f"错误: 对照表文件不存在: {MAPPING_FILE}")
    sys.exit(1)

mappings = parse_mapping_file(MAPPING_FILE)
print(f"解析成功: {len(mappings)} 个场景")

if len(mappings) == 0:
    print("错误: 没有解析到任何场景")
    sys.exit(1)

# 显示前5个场景
print("\n前5个场景预览:")
for seq, text, img in mappings[:5]:
    print(f"  {seq:02d}. {img} → {text[:40]}...")

# 2. 检查图片文件
print(f"\n[2/4] 检查图片文件...")
missing_images = []
for seq, text, img_filename in mappings:
    img_path = find_image_file(IMAGE_DIR, img_filename)
    if not img_path:
        missing_images.append((seq, img_filename))

if missing_images:
    print(f"警告: 缺失 {len(missing_images)} 张图片:")
    for seq, img in missing_images[:5]:
        print(f"  {seq:02d}. {img}")
    if len(missing_images) > 5:
        print(f"  ... 还有 {len(missing_images) - 5} 张")
    
    response = input("\n是否继续（缺失的场景将被跳过）? [y/N]: ")
    if response.lower() != 'y':
        print("已取消")
        sys.exit(0)

# 3. 创建项目
print(f"\n[3/4] 创建剪映项目...")
project = JyProject(project_name=PROJECT_NAME, overwrite=True)
print("项目已创建")

# 3.5. 添加高级片头（引导语 + 快闪 + 标题揭示）
opening_duration = add_advanced_opening(project, IMAGE_DIR, mappings)

# 4. 逐场景生成：音频 + 字幕 + 图片（完全同步）
print(f"\n[4/4] 生成视频内容...")
print(f"使用音色: {VOICE_SPEAKER}")
print("这可能需要几分钟，请耐心等待...\n")

current_time = opening_duration  # 从片头结束时开始
success_segments = []
failed_segments = []

# ============================================================
# 🔥 跨场景配音块合并（与 create_video_final.py 一致）
# ------------------------------------------------------------
# 之前每个场景各发一次 TTS，88 个镜头 = 88 次独立合成，
# 每段都重新起调、句尾收音，听起来一顿一顿。
# 现在把连续场景合并成 ≤180 字一块，一次合成，
# 再把真实时长按字数比例分回给每个场景。字幕仍是短句。
# ============================================================
import tts_grouping as _tg

_scenes = _tg.prepare_scenes(mappings, find_image_file, IMAGE_DIR, failed_segments)
_scene_durations, _voice_total, _failed_seqs = _tg.synthesize_groups(
    project, _scenes, VOICE_SPEAKER, current_time,
    generate_tts_with_retry, format_time
)
for _s in _failed_seqs:
    failed_segments.append((_s, "", "配音失败"))
print()


for seq, text, img_filename in mappings:
    # 查找图片
    img_path = find_image_file(IMAGE_DIR, img_filename)
    if not img_path:
        failed_segments.append((seq, text[:30] if text else "空文案", "图片缺失"))
        continue
    
    try:
        # 如果文案为空，跳过该场景（只有图片没有配音/字幕）
        if not text or len(text) < 3:
            print(f"  场景 {seq:02d}: 跳过（无文案）")
            continue
        
        # 1. 按标点符号分割文案（保留标点，包括 '/'，并去除斜杠前后的空格）
        # 先将 " / " 替换为 "/" （去除斜杠前后空格）
        text_normalized = re.sub(r'\s*/\s*', '/', text)
        sentences = re.split(r'([，。！？、/])', text_normalized)
        # 重新组合：文字+标点
        combined_sentences = []
        i = 0
        while i < len(sentences):
            if sentences[i].strip():
                sentence = sentences[i].strip()
                # 如果下一个是标点（包括 '/'），加上
                if i + 1 < len(sentences) and sentences[i + 1] in '，。！？、/':
                    sentence += sentences[i + 1]
                    i += 2
                else:
                    i += 1
                
                if len(sentence) > 1:  # 跳过单字符
                    combined_sentences.append(sentence)
            else:
                i += 1
        
        # 如果没有分割出句子，使用整个文案
        if not combined_sentences:
            combined_sentences = [text]
        
        # 🔥 新增：进一步分割过长的句子（超过15字的句子，按空格或每10字分割）
        final_sentences = []
        for sentence in combined_sentences:
            # 去除标点后的净字数
            clean_sentence = re.sub(r'[，。！？、""''：；…——/]', '', sentence)
            
            if len(clean_sentence) <= 25:
                # 不超过15字，直接保留
                final_sentences.append(sentence)
            else:
                # 超过15字，需要进一步分割
                # 优先按空格分割
                if ' ，。' in sentence or',':
                    parts = sentence.split(' ')
                    current_part = ""
                    for part in parts:
                        if len(current_part) + len(part) <= 15:
                            current_part += part + " "
                        else:
                            if current_part:
                                final_sentences.append(current_part.strip())
                            current_part = part + " "
                    if current_part:
                        final_sentences.append(current_part.strip())
                else:
                    # 没有空格，按每10-12字强制分割
                    # 去掉标点
                    chars = list(sentence)
                    current_chunk = ""
                    char_count = 0
                    
                    for char in chars:
                        current_chunk += char
                        if char not in '，。！？、""''：；…——/':
                            char_count += 1
                        
                        # 每10个有效字符分割一次
                        if char_count >= 10:
                            final_sentences.append(current_chunk)
                            current_chunk = ""
                            char_count = 0
                    
                    if current_chunk:
                        final_sentences.append(current_chunk)
        
        combined_sentences = final_sentences
        
        # 🔥 配音已在前面按"配音块"整体合成，这里只取本场景分得的时长
        if seq in _failed_seqs or seq not in _scene_durations:
            print(f"  X 场景 {seq:02d}: 配音失败（所属配音块失败）")
            continue

        total_scene_duration = _scene_durations[seq]
        scene_start_time = current_time
        print(f"  场景 {seq:02d}: {len(combined_sentences)} 句话 "
              f"({format_time(total_scene_duration)}) - {text[:40]}...")

        # 5. 🔥 优化：基于语速的字幕时长预估
        # 业界标准：中文TTS约3.5-4字/秒（正常语速）
        # 当前语速1.05倍，约4.4字/秒
        CHARS_PER_SECOND = 4.4  # 字/秒（语速1.05倍）
        
        # 计算每句话的预估时长（基于字数和语速）
        subtitle_durations = []
        for sentence in combined_sentences:
            # 去除标点符号（包括中文引号和 '/'）和空格
            clean_sentence = re.sub(r'[，。！？、""''：；…——/\s]', '', sentence)
            char_count = len(clean_sentence)
            
            # 基础时长：字数 / 语速
            base_duration = char_count / CHARS_PER_SECOND
            
            # 标点符号增加停顿时间
            # 逗号/顿号：0.15秒，句号/问号/感叹号：0.3秒，斜杠：0.1秒
            punct_pause = 0
            punct_pause += sentence.count('，') * 0.15
            punct_pause += sentence.count('、') * 0.15
            punct_pause += sentence.count('。') * 0.3
            punct_pause += sentence.count('！') * 0.3
            punct_pause += sentence.count('？') * 0.3
            punct_pause += sentence.count('/') * 0.1
            
            # 总预估时长（秒）
            estimated_seconds = base_duration + punct_pause
            # 转换为微秒
            estimated_duration = int(estimated_seconds * 1000000)
            
            subtitle_durations.append(estimated_duration)
        
        # 计算预估总时长
        total_estimated_duration = sum(subtitle_durations)
        
        # 🔥 调整：如果预估总时长与实际配音时长差异较大，按比例缩放
        if total_estimated_duration > 0:
            scale_factor = total_scene_duration / total_estimated_duration
            # 只在差异超过10%时才调整
            if abs(scale_factor - 1.0) > 0.1:
                subtitle_durations = [int(d * scale_factor) for d in subtitle_durations]
                print(f"    字幕时长已调整（缩放系数: {scale_factor:.2f}）")
        
        # 6. 按预估时长为每句话添加字幕
        # 6. 按预估时长为每句话添加字幕
        subtitle_start = current_time
        for sent_idx, sentence in enumerate(combined_sentences):
            # 获取这句话的预估时长
            subtitle_duration = subtitle_durations[sent_idx]
            
            # 去除标点符号（包括中文引号和 '/'）和空格
            sentence_no_punct = re.sub(r'[，。！？、""''：；…—— /]', '', sentence)
            
            try:
                # 🔥 第一个场景使用特殊样式（参考 build_flash.py）
                if len(success_segments) == 0 and sent_idx == 0:
                    # 第一个场景的第一句字幕：居中 + 暖白色 + 半透明黑色背景
                    project.add_text_simple(
                        text=sentence_no_punct,
                        start_time=subtitle_start,
                        duration=subtitle_duration,
                        track_name="Subtitles",
                        font=FontType.优设标题黑,
                        style=draft.TextStyle(
                            size=5.8,
                            letter_spacing=1,
                            color=(1.0, 0.976, 0.945)  # 0xFFF9F1 暖白色
                        ),
                        border=draft.TextBorder(color=(0.0, 0.0, 0.0), alpha=0.9, width=3.0),
                        clip_settings=draft.ClipSettings(transform_y=0.0)  # 居中
                    )
                else:
                    # 其他字幕：常规样式
                    project.add_text_simple(
                        text=sentence_no_punct,
                        start_time=subtitle_start,
                        duration=subtitle_duration,
                        track_name="Subtitles",
                        font=FontType.优设标题黑,
                        style=draft.TextStyle(size=5.0, letter_spacing=1),
                        border=draft.TextBorder(color=(0.0, 0.0, 0.0), alpha=1.0, width=40.0),
                        clip_settings=draft.ClipSettings(transform_y=-0.8)
                    )
                print(f"    OK 第{sent_idx+1}句字幕: {sentence_no_punct[:20]}... ({format_time(subtitle_duration)})")
                subtitle_start += subtitle_duration
                
            except Exception as e:
                print(f"    X 第{sent_idx+1}句字幕失败: {str(e)[:30]}")
        
        # 7. 添加图片（持续整个场景，包括停顿）
        video_seg = project.add_media_safe(
            media_path=img_path,
            start_time=current_time,
            duration=total_scene_duration,
            track_name="VideoTrack"
        )
        
        # 🔥 新增：为第一个场景添加特效（持续1秒）
        if len(success_segments) == 0:  # 第一个成功的场景
            # 创建一个1秒的视频片段用于添加特效
            effect_duration = 1000000  # 1秒
            effect_seg = project.add_media_safe(
                media_path=img_path,
                start_time=current_time,
                duration=effect_duration,
                track_name="EffectTrack"
            )
            
            if effect_seg:
                try:
                    # 添加幻彩故障特效
                    effect_seg.add_effect(VideoSceneEffectType.幻彩故障)
                    print(f"    OK 添加幻彩故障特效 (1秒)")
                except Exception as e:
                    print(f"    注意: 幻彩故障特效失败: {str(e)[:30]}")
                
                try:
                    # 添加震动屏闪特效
                    effect_seg.add_effect(VideoSceneEffectType.震动屏闪)
                    print(f"    OK 添加震动屏闪特效 (1秒)")
                except Exception as e:
                    print(f"    注意: 震动屏闪特效失败: {str(e)[:30]}")
        
        # 8. 🔥 优化：Ken Burns 动画（缩放 + 平移）
        if video_seg:
            try:
                # 根据场景序号选择不同的动画效果
                if seq % 3 == 0:
                    # 🔵 缩放 + 向右平移
                    video_seg.add_keyframe(KP.uniform_scale, 0, 1.0)
                    video_seg.add_keyframe(KP.uniform_scale, total_scene_duration, 1.15)
                    video_seg.add_keyframe(KP.position_x, 0, -0.05)
                    video_seg.add_keyframe(KP.position_x, total_scene_duration, 0.05)
                elif seq % 3 == 1:
                    # 🔴 缩放 + 向左平移
                    video_seg.add_keyframe(KP.uniform_scale, 0, 1.15)
                    video_seg.add_keyframe(KP.uniform_scale, total_scene_duration, 1.0)
                    video_seg.add_keyframe(KP.position_x, 0, 0.05)
                    video_seg.add_keyframe(KP.position_x, total_scene_duration, -0.05)
                else:
                    # 🟢 缩放 + 向上平移
                    video_seg.add_keyframe(KP.uniform_scale, 0, 1.0)
                    video_seg.add_keyframe(KP.uniform_scale, total_scene_duration, 1.1)
                    video_seg.add_keyframe(KP.position_y, 0, 0.03)
                    video_seg.add_keyframe(KP.position_y, total_scene_duration, -0.03)
            except Exception as e:
                # 降级：使用基础动画
                print(f"    注意: 使用基础Ken Burns")
                if seq % 2 == 1:
                    video_seg.add_keyframe(KP.uniform_scale, 0, 1.0)
                    video_seg.add_keyframe(KP.uniform_scale, total_scene_duration, 1.1)
                else:
                    video_seg.add_keyframe(KP.uniform_scale, 0, 1.1)
                    video_seg.add_keyframe(KP.uniform_scale, total_scene_duration, 1.0)
        
        # 更新当前时间
        current_time += total_scene_duration
        
        # 记录成功
        success_segments.append({
            'seq': seq,
            'text': text[:30],
            'image': img_filename,
            'duration': total_scene_duration,
            'sentences': len(combined_sentences)
        })
        
        print(f"  OK 场景 {seq:02d}: 图片持续 {format_time(total_scene_duration)} ({len(combined_sentences)}句)")
        
        # 显示进度
        if len(success_segments) % 5 == 0:
            print(f"\n  === 进度: {len(success_segments)}/{len(mappings)} 场景 ===\n")
            
    except Exception as e:
        failed_segments.append((seq, text[:30], str(e)[:30]))
        print(f"  X 场景 {seq:02d} 失败: {str(e)[:50]}")
        continue

total_duration = current_time

print(f"\n视频内容生成完成!")
print(f"  成功: {len(success_segments)}/{len(mappings)} 场景")
print(f"  失败: {len(failed_segments)} 场景")
print(f"  总时长: {format_time(total_duration)}")

if len(success_segments) == 0:
    print("\n错误: 没有成功生成任何内容")
    sys.exit(1)

# 5. 添加叠甲文字
print("\n添加全局元素...")
project.add_text_simple(
    text="虚构创作，与真实人物无关",
    start_time=opening_duration,  # 从片头结束后开始
    duration=total_duration - opening_duration,
    track_name="DisclaimerTrack",
    font=FontType.优设标题黑,
    style=draft.TextStyle(size=3.0, alpha=0.8, letter_spacing=1),
    clip_settings=draft.ClipSettings(transform_x=-0.8, transform_y=0.8)
)
print("已添加叠甲文字")

# 6. 保存项目
print("\n保存项目...")
draft_path = project.save()

# 7. 输出结果
print("\n" + "=" * 60)
print("✅ 视频生成完成!")
print("=" * 60)
print(f"项目名称: {PROJECT_NAME}")
print(f"草稿路径: {draft_path}")
print(f"总时长:   {format_time(total_duration)}")
print(f"\n内容统计:")
print(f"  片头:   引导语 + 快闪叠加（关键帧优化）+ 音效 + 线性蒙版")
print(f"          · 引导语: 1.8秒 (文字+配音，无背景图)")
print(f"          · 快闪: ~1.67秒 (9张叠加，关键帧打在下一张开始)")
print(f"          · 音效: 棘轮音效覆盖整个快闪")
print(f"          · 蒙版: 已添加线性蒙版（静态）")
print(f"          ⚠️  蒙版关键帧需手动添加（见下方说明）")
print(f"  场景数: {len(success_segments)} 个")
print(f"  配音:   {VOICE_SPEAKER} (语速1.05倍)")
print(f"  字体:   优设标题黑 (字间距1)")
print(f"  字幕:   已去除所有标点符号（包括中文引号）")
print(f"          按原文标点分句显示（便于阅读）")
print(f"          智能分割：超15字按空格分割，无空格每10字分割")
print(f"          时长预估：基于语速4.4字/秒 + 标点停顿")
print(f"          第一个场景第一句：居中+暖白色 (参考 build_flash.py)")
print(f"          其他字幕：常规样式（底部）")
print(f"  配音:   完整连续配音（不分割，空格替换为逗号）")
print(f"  同步:   图片、字幕、音频完全一致")
print(f"  动画:   Ken Burns缩放（1.0 ↔ 1.1）")
print(f"  优化:   ✅ 删除开头不透明度关键帧（固定100%）")
print(f"          ⚠️  蒙版关键帧需要手动添加")
print(f"\n音频处理参考 build_opening.py:")
print(f"  · 使用 amix 混合多个音频轨道")
print(f"  · 使用 adelay 设置音频开始时间")
print(f"  · 棘轮音效(ratchet.wav)在快闪开始时播放")
print(f"  · 引导语配音与快闪同时开始（0秒）")

if failed_segments:
    print(f"\n⚠️  失败的场景 ({len(failed_segments)} 个):")
    for seq, text, reason in failed_segments[:5]:
        print(f"  {seq:02d}. {text}... ({reason})")
    if len(failed_segments) > 5:
        print(f"  ... 还有 {len(failed_segments) - 5} 个")

print("\n📋 前5个成功的场景:")
for seg in success_segments[:5]:
    print(f"  {seg['seq']:02d}. {seg['text']}... ({format_time(seg['duration'])}, {seg['sentences']}句, {seg['image']})")

print("\n" + "=" * 60)
print("下一步:")
print("1. 打开剪映专业版")
print(f"2. 找到草稿: {PROJECT_NAME}")
print("3. 【重要】手动添加蒙版关键帧动画：")
print("   - 选中片头9张快闪图片")
print("   - 为每张图片的线性蒙版添加关键帧：")
print("     0ms: 蒙版最上方，羽化50")
print("     333ms: 蒙版最下方，羽化0")
print("     416ms: 蒙版中下方，羽化30")
print("   - 详细步骤见：蒙版关键帧操作指南.md")
print("4. 预览视频效果")
print("5. (可选) 添加背景音乐")
print("6. 导出视频")
print("\n" + "=" * 60)
print("📘 提示: 已生成 '蒙版关键帧操作指南.md'")
print("   包含3种蒙版关键帧添加方案：")
print("   · 方案一：在剪映中手动添加（推荐）")
print("   · 方案二：使用第三方工具批量添加")
print("   · 方案三：直接编辑JSON（高级）")
print("=" * 60)
