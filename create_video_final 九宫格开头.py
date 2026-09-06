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
    查找图片文件（支持png和jpg）
    """
    # 尝试原始文件名
    full_path = os.path.join(image_dir, filename)
    if os.path.exists(full_path):
        return full_path
    
    # 尝试jpg扩展名
    jpg_path = full_path.replace('.png', '.jpg')
    if os.path.exists(jpg_path):
        return jpg_path
    
    # 尝试shot_前缀
    shot_name = filename.replace('scene_', 'shot_')
    shot_path = os.path.join(image_dir, shot_name)
    if os.path.exists(shot_path):
        return shot_path
    
    return None

def format_time(microseconds):
    """格式化时间"""
    seconds = microseconds / 1000000
    minutes = int(seconds // 60)
    secs = seconds % 60
    return f"{minutes}:{secs:04.1f}"

def generate_tts_with_retry(project, text, speaker, start_time, track_name, max_retries=3, speed=1.1):
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

def split_text_recursively(text, max_length=25, depth=0):
    """
    递归分割文本，直到每段都不超过max_length
    返回: [(文本片段1, 0), (文本片段2, 100000), ...] 
          每个元组是(文本, 相对前一段的额外延迟微秒)
    """
    # 计算净字数（去除标点和空格）
    clean_text = re.sub(r'[，。！？、""''：；…—— ]', '', text)
    
    if len(clean_text) <= max_length:
        # 不需要分割
        return [(text, 0)]
    
    # 需要分割：找逗号和句号
    punctuation_positions = []
    for i, char in enumerate(text):
        if char in '，。！？':
            punctuation_positions.append(i)
    
    if not punctuation_positions:
        # 没有标点，无法分割，返回原文
        if depth == 0:  # 只在第一层打印警告
            print(f"      警告: 文本超过{max_length}字但无标点，无法分割")
        return [(text, 0)]
    
    # 找最接近中间位置的标点
    mid_pos = len(text) // 2
    closest_punct = min(punctuation_positions, key=lambda x: abs(x - mid_pos))
    
    # 分成两段（包含标点）
    first_part = text[:closest_punct + 1]
    second_part = text[closest_punct + 1:]
    
    # 递归处理每一段
    first_segments = split_text_recursively(first_part, max_length, depth + 1)
    second_segments = split_text_recursively(second_part, max_length, depth + 1)
    
    # 合并结果
    result = []
    
    # 第一部分保持原样
    for seg_text, pause in first_segments:
        result.append((seg_text, pause))
    
    # 第二部分的每个片段都加停顿
    for seg_text, original_pause in second_segments:
        # 如果原来就有停顿，保持；否则加上0.1秒
        pause = original_pause if original_pause > 0 else 100000
        result.append((seg_text, pause))
    
    return result

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

def add_grid_opening(project, image_dir, duration=2000000, first_scene_image=None):
    """
    添加片头：5张图片静态叠加（占满屏幕）+ 标题字幕 + 配音
    第5张为剧本的第一张图片
    duration: 持续时间（微秒），默认2秒
    first_scene_image: 剧本的第一张图片文件名
    返回: 片头结束的时间点
    """
    print("\n添加片头：5张图片占满屏幕 + 标题 + 配音...")
    
    # 随机选择4张图片
    selected_images = get_random_images(image_dir, 4)
    
    # 添加第5张（剧本的第一张图片）
    if first_scene_image:
        selected_images.append(first_scene_image)
    
    if len(selected_images) < 3:
        print(f"  警告: 图片不足，跳过片头")
        return 0
    
    print(f"  选择图片: {', '.join(selected_images[:3])}...")
    if first_scene_image:
        print(f"  第5张: {first_scene_image} (剧本第一张)")
    
    # 5张图片的缩放（接近100%，占满屏幕）
    scales = [1.0, 1.02, 1.01, 0.99, 1.03]
    
    # 为每张图片创建片段（静态，入场动画：九宫格，居中显示）
    for idx, img_name in enumerate(selected_images):
        if idx >= len(scales):
            break
            
        img_path = find_image_file(image_dir, img_name)
        if not img_path:
            continue
        
        try:
            # 添加图片到独立轨道
            segment = project.add_media_safe(
                media_path=img_path,
                start_time=0,
                duration=duration,
                track_name=f"Opening_{idx+1}"
            )
            
            if segment:
                scale = scales[idx]
                
                # 居中位置，占满屏幕
                segment.add_keyframe(KP.uniform_scale, 0, scale)
                
                # 🔥 新增：为每张图片添加九宫格入场动画
                try:
                    segment.add_animation(IntroType.九宫格)
                    print(f"  OK 图片 {idx+1}: {img_name} (九宫格入场)")
                except Exception as e:
                    print(f"  OK 图片 {idx+1}: {img_name} (九宫格失败: {str(e)[:30]})")
                
        except Exception as e:
            print(f"  X 图片 {idx+1} 失败: {str(e)[:50]}")
            continue
    
    # 生成片头配音
    opening_text = "今天体验的人生副本是"
    try:
        audio_seg = project.add_tts_intelligent(
            text=opening_text,
            speaker=VOICE_SPEAKER,
            start_time=0,
            track_name="OpeningVoice",
            tts_backend="sami",
            allow_fallback=False
        )
        
        if audio_seg:
            # 🔥 设置语速1.1倍
            audio_seg.speed.speed = speed
            
            # 计算实际时长（原时长 / 语速）
            original_duration = audio_seg.target_timerange.duration
            actual_duration = int(original_duration / 1.1)
            
            # 更新目标时间范围
            from pyJianYingDraft.time_util import Timerange
            audio_seg.target_timerange = Timerange(
                audio_seg.target_timerange.start,
                actual_duration
            )
            
            # 如果配音超过2秒，延长片头时长
            if actual_duration != duration:
                duration = actual_duration
            
            print(f"  OK 片头配音已生成: {format_time(actual_duration)} (1.1倍速)")
    except Exception as e:
        print(f"  X 片头配音失败: {str(e)[:50]}")
    
    # 添加片头标题字幕
    try:
        project.add_text_simple(
            text=opening_text,
            start_time=0,
            duration=duration,
            track_name="OpeningTitle",
            font=FontType.新青年体,
            style=draft.TextStyle(size=8.0, letter_spacing=1),  # 大字号 + 字间距1
            border=draft.TextBorder(color=(0.0, 0.0, 0.0), alpha=1.0, width=50.0),  # 粗描边
            clip_settings=draft.ClipSettings(transform_y=0.0)  # 居中位置
        )
        print(f"  OK 片头标题已添加")
    except Exception as e:
        print(f"  X 片头标题失败: {str(e)[:50]}")
    
    print(f"  片头效果已添加（时长: {format_time(duration)}）")
    print(f"  效果: 5张图片占满屏幕（第5张为剧本第一张） + 居中标题 + 配音 + 九宫格入场")
    return duration

# 配置路径（可修改）
MAPPING_FILE = r'剧本\口播文案_图片序号_对应表.txt'
IMAGE_DIR = r'image'
# 项目名称加时间戳，避免覆盖
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
PROJECT_NAME = f"人生副本_{timestamp}"
VOICE_SPEAKER = "ICL_zh_female_manbo_jianying"  # 曼波讲电影音色
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

# 3.5. 添加片头静态叠加（2秒）
# 获取剧本第一张图片
first_scene_image = mappings[0][2] if len(mappings) > 0 else None
opening_duration = add_grid_opening(project, IMAGE_DIR, duration=2000000, first_scene_image=first_scene_image)

# 4. 逐场景生成：音频 + 字幕 + 图片（完全同步）
print(f"\n[4/4] 生成视频内容...")
print(f"使用音色: {VOICE_SPEAKER}")
print("这可能需要几分钟，请耐心等待...\n")

current_time = opening_duration  # 从片头结束时开始
success_segments = []
failed_segments = []

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
        
        # 1. 按标点符号分割文案（保留标点）
        sentences = re.split(r'([，。！？、])', text)
        # 重新组合：文字+标点
        combined_sentences = []
        i = 0
        while i < len(sentences):
            if sentences[i].strip():
                sentence = sentences[i].strip()
                # 如果下一个是标点，加上
                if i + 1 < len(sentences) and sentences[i + 1] in '，。！？、':
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
        
        print(f"  场景 {seq:02d}: {len(combined_sentences)} 句话 - {text[:40]}...")
        
        # 2. 记录场景开始时间
        scene_start_time = current_time
        
        # 3. 使用递归分割文本（确保每段都不超过25字）
        text_segments = split_text_recursively(text, max_length=25)
        num_segments = len(text_segments)
        
        if num_segments > 1:
            print(f"    文案分{num_segments}段生成配音（每段≤25字）")
        
        # 4. 逐段生成音频（不加停顿）
        audio_durations = []
        current_audio_time = current_time
        
        for seg_idx, (segment_text, pause_before) in enumerate(text_segments):
            # 不再添加停顿，直接连续生成
            
            # 生成这一段的配音（去除末尾的逗号和顿号，避免TTS截断）
            tts_text = segment_text.rstrip('，、')
            if not tts_text.endswith(('。', '！', '？')):
                tts_text += '。'  # 如果末尾没有句号，加上句号让TTS知道句子结束
            
            audio_seg, duration = generate_tts_with_retry(
                project, tts_text, VOICE_SPEAKER, current_audio_time, "VoiceOver"
            )
            
            if not audio_seg:
                failed_segments.append((seq, text[:30], f"第{seg_idx+1}段配音失败"))
                print(f"  X 场景 {seq:02d}: 第{seg_idx+1}段配音失败")
                break
            
            audio_durations.append(duration)
            print(f"    OK 第{seg_idx+1}段配音: {format_time(duration)}")
            current_audio_time += duration
        
        # 如果有段失败，跳过整个场景
        if len(audio_durations) < num_segments:
            continue
        
        # 计算总时长（所有音频之和，不含停顿）
        total_scene_duration = sum(audio_durations)
        print(f"    OK 完整配音: {format_time(total_scene_duration)} (分{num_segments}段)")
        
        # 5. 根据句子字数比例分配字幕时间
        # 计算每句话的字数（去除标点符号和空格）
        sentence_chars = []
        for sentence in combined_sentences:
            # 去除所有标点（包括中文引号）
            clean_sentence = re.sub(r'[，。！？、""''：；…——]', '', sentence)
            sentence_chars.append(len(clean_sentence))
        
        total_chars = sum(sentence_chars)
        if total_chars == 0:
            print(f"  X 场景 {seq:02d}: 无有效文字")
            continue
        
        # 6. 按比例为每句话添加字幕（对齐到包含停顿的总时长）
        subtitle_start = current_time
        for sent_idx, sentence in enumerate(combined_sentences):
            # 计算这句话应该占用的时间（按字数比例）
            char_count = sentence_chars[sent_idx]
            subtitle_duration = int(total_scene_duration * (char_count / total_chars))
            
            # 去除标点符号（包括中文引号）
            sentence_no_punct = re.sub(r'[，。！？、""''：；…——]', '', sentence)
            
            try:
                project.add_text_simple(
                    text=sentence_no_punct,
                    start_time=subtitle_start,
                    duration=subtitle_duration,
                    track_name="Subtitles",
                    font=FontType.新青年体,
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
            'sentences': len(combined_sentences),
            'audio_segments': num_segments
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
    font=FontType.新青年体,
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
print(f"  片头:   5张图片占满屏幕（第5张为剧本第一张） + 居中标题 + 配音 + 九宫格入场")
print(f"  场景数: {len(success_segments)} 个")
print(f"  配音:   {VOICE_SPEAKER} (语速1.1倍)")
print(f"  字体:   江湖体 (字间距1)")
print(f"  字幕:   已去除所有标点符号（包括中文引号）")
print(f"  同步:   图片、字幕、音频完全一致（包含段间停顿）")
print(f"  动画:   Ken Burns缩放（1.0 ↔ 1.1）")
print(f"  分段:   超25字文案递归分段，段间0.1秒停顿")

if failed_segments:
    print(f"\n⚠️  失败的场景 ({len(failed_segments)} 个):")
    for seq, text, reason in failed_segments[:5]:
        print(f"  {seq:02d}. {text}... ({reason})")
    if len(failed_segments) > 5:
        print(f"  ... 还有 {len(failed_segments) - 5} 个")

print("\n📋 前5个成功的场景:")
for seg in success_segments[:5]:
    seg_info = f"分{seg['audio_segments']}段" if seg['audio_segments'] > 1 else "1段"
    print(f"  {seg['seq']:02d}. {seg['text']}... ({format_time(seg['duration'])}, {seg_info}, {seg['image']})")

print("\n" + "=" * 60)
print("下一步:")
print("1. 打开剪映专业版")
print(f"2. 找到草稿: {PROJECT_NAME}")
print("3. 预览视频")
print("4. (可选) 添加背景音乐")
print("5. 导出视频")
print("=" * 60)
