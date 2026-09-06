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

def add_advanced_opening(project, image_dir, mappings, duration_lead=1800000, duration_flash=1667000):
    """
    添加高级片头：引导语 + 快闪（同时开始）+ 音效
    参考 life-sim-video-pipeline 的 build_opening.py 和 build_flash.py
    
    duration_lead: 引导语持续时间（微秒），默认1.8秒
    duration_flash: 快闪持续时间（微秒），默认1.667秒
    返回: 片头总时长
    """
    print("\n添加高级片头：引导语 + 快闪 + 音效（同时从0秒开始）...")
    
    # ========== 引导语文字卡（纯文字，无背景图，从0秒开始）==========
    lead_text = "今天你要体验的人生副本是"
    print(f"\n[片头·引导语] 文字卡 ({format_time(duration_lead)}，从0秒开始)")
    
    try:
        # 添加引导语字幕（居中，大字号）
        # 参考 build_opening.py: fontcolor=0xFFF9F1, fontsize=leadFontSize
        project.add_text_simple(
            text=lead_text,
            start_time=0,
            duration=duration_lead,
            track_name="Opening_Lead_Text",
            font=FontType.江湖体,
            style=draft.TextStyle(
                size=9.0,
                letter_spacing=1,
                color=(1.0, 0.976, 0.945)  # 0xFFF9F1 暖白色
            ),
            border=draft.TextBorder(color=(0.0, 0.0, 0.0), alpha=0.85, width=60.0),
            clip_settings=draft.ClipSettings(transform_y=0.0)  # 居中
        )
        
        # 添加引导语配音（从0秒开始，与快闪同步）
        audio_seg = project.add_tts_intelligent(
            text=lead_text,
            speaker=VOICE_SPEAKER,
            start_time=0,
            track_name="Opening_Lead_Voice",
            tts_backend="sami",
            allow_fallback=True
        )
        
        if audio_seg:
            # 设置语速1.1倍
            audio_seg.speed.speed = 1.1
            actual_duration = int(audio_seg.target_timerange.duration / 1.1)
            from pyJianYingDraft.time_util import Timerange
            audio_seg.target_timerange = Timerange(audio_seg.target_timerange.start, actual_duration)
            print(f"  ✓ 引导语配音: {format_time(actual_duration)}")
        
        print(f"  ✓ 引导语文字卡已添加（纯文字，无背景图）")
    except Exception as e:
        print(f"  ✗ 引导语失败: {str(e)[:50]}")
    
    # ========== 添加快闪音效（棘轮音效，从0秒开始）==========
    audio_dir = r"c:\Users\29471\Desktop\create video\jianying\audio"
    ratchet_sfx = os.path.join(audio_dir, "ratchet.wav")
    
    # 如果没有 ratchet.wav，使用 coding.WAV
    if not os.path.exists(ratchet_sfx):
        coding_sfx = os.path.join(audio_dir, "coding.WAV")
        if os.path.exists(coding_sfx):
            ratchet_sfx = coding_sfx
            print(f"\n[片头·音效] 使用 coding.WAV 作为快闪音效")
        else:
            ratchet_sfx = None
            print(f"\n[片头·音效] 音效文件缺失，跳过")
    else:
        print(f"\n[片头·音效] 使用 ratchet.wav")
    
    if ratchet_sfx:
        try:
            # 添加快闪音效（从0秒开始，与画面同步）
            sfx_seg = project.add_media_safe(
                media_path=ratchet_sfx,
                start_time=0,
                duration=duration_flash,  # 持续整个快闪时长
                track_name="Opening_Flash_SFX"
            )
            
            if sfx_seg:
                print(f"  ✓ 快闪音效已添加: {os.path.basename(ratchet_sfx)}")
                print(f"  ✓ 音效与画面同步（0秒开始，持续{format_time(duration_flash)}）")
        except Exception as e:
            print(f"  ✗ 音效添加失败: {str(e)[:50]}")
    
    # ========== 快闪效果（从0秒开始，与引导语并行）==========
    flash_start = 0  # 从0秒开始
    flash_cuts = 9  # 快闪切换次数
    frame_duration = duration_flash // flash_cuts
    
    print(f"\n[片头·快闪] 快闪效果 ({format_time(duration_flash)}，{flash_cuts}次切换，从0秒开始)")
    print(f"  叠加效果：前8张图片持续到第9张结束，关键帧打在下一张开始位置")
    
    # 随机选择9张图片用于快闪
    flash_images = get_random_images(image_dir, flash_cuts)
    
    # 计算第9张结束的时间点
    ninth_end_time = frame_duration * 9
    
    # 第一轮：第1-9张图片
    for i, img_name in enumerate(flash_images):
        img_path = find_image_file(image_dir, img_name)
        if not img_path:
            continue
        
        try:
            seg_start = flash_start + i * frame_duration
            zoom_in = (i % 2 == 0)  # 交替推拉
            
            # 前8张图片持续到第9张结束
            if i < 8:
                seg_duration = ninth_end_time - seg_start
            else:
                # 第9张图片正常持续时间
                seg_duration = frame_duration
            
            # 添加图片
            segment = project.add_media_safe(
                media_path=img_path,
                start_time=seg_start,
                duration=seg_duration,
                track_name=f"Opening_Flash_{i+1}"
            )
            
            if segment:
                # 🔥 关键帧的结束时间：打在下一张图片的开始位置
                if i < flash_cuts - 1:
                    # 关键帧结束时间 = 下一张图片的开始时间 - 当前图片开始时间
                    keyframe_end_time = frame_duration  # 下一张开始位置
                else:
                    # 最后一张：关键帧结束时间 = 自己的持续时间
                    keyframe_end_time = seg_duration
                
                # 快速推拉镜头效果（关键帧打到下一张开始位置）
                if zoom_in:
                    # 推镜头：从小到大
                    segment.add_keyframe(KP.uniform_scale, 0, 1.0)
                    segment.add_keyframe(KP.uniform_scale, keyframe_end_time, 1.34)
                    # 反向漂移
                    segment.add_keyframe(KP.position_x, 0, -0.03)
                    segment.add_keyframe(KP.position_x, keyframe_end_time, 0.03)
                else:
                    # 拉镜头：从大到小
                    segment.add_keyframe(KP.uniform_scale, 0, 1.34)
                    segment.add_keyframe(KP.uniform_scale, keyframe_end_time, 1.0)
                    # 反向漂移
                    segment.add_keyframe(KP.position_x, 0, 0.03)
                    segment.add_keyframe(KP.position_x, keyframe_end_time, -0.03)
                
                # 尝试添加视频特效（快闪冲击感）
                try:
                    if i % 2 == 0:
                        # 白闪：荧光爆闪
                        segment.add_effect(VideoSceneEffectType.荧光爆闪)
                    else:
                        # 黑闪：幻彩故障
                        segment.add_effect(VideoSceneEffectType.幻彩故障)
                except:
                    pass
                
                if i < 8:
                    print(f"  ✓ 快闪{i+1}/{flash_cuts} {'推镜' if zoom_in else '拉镜'} {img_name} (持续到第9张结束，关键帧→下一张)")
                else:
                    print(f"  ✓ 快闪{i+1}/{flash_cuts} {'推镜' if zoom_in else '拉镜'} {img_name}")
        
        except Exception as e:
            print(f"  ✗ 快闪{i+1}失败: {str(e)[:30]}")
            continue
    
    # 第二轮：在第9张结束后，重复第1-8张图片
    second_round_start = ninth_end_time
    print(f"\n  [重复轮] 在第9张结束后，重复第1-8张图片")
    
    for i in range(8):  # 只重复前8张
        img_name = flash_images[i]
        img_path = find_image_file(image_dir, img_name)
        if not img_path:
            continue
        
        try:
            seg_start = second_round_start + i * frame_duration
            zoom_in = (i % 2 == 0)  # 交替推拉
            
            # 重复的图片也持续到原第9张结束的时间点
            seg_duration = ninth_end_time - i * frame_duration
            
            # 添加图片
            segment = project.add_media_safe(
                media_path=img_path,
                start_time=seg_start,
                duration=seg_duration,
                track_name=f"Opening_Flash_R{i+1}"
            )
            
            if segment:
                # 🔥 关键帧的结束时间：打在下一张图片的开始位置
                if i < 7:  # 前7张（重复的第1-7张）
                    keyframe_end_time = frame_duration
                else:
                    # 最后一张（重复的第8张）
                    keyframe_end_time = seg_duration
                
                # 快速推拉镜头效果（关键帧打到下一张开始位置）
                if zoom_in:
                    # 推镜头：从小到大
                    segment.add_keyframe(KP.uniform_scale, 0, 1.0)
                    segment.add_keyframe(KP.uniform_scale, keyframe_end_time, 1.34)
                    # 反向漂移
                    segment.add_keyframe(KP.position_x, 0, -0.03)
                    segment.add_keyframe(KP.position_x, keyframe_end_time, 0.03)
                else:
                    # 拉镜头：从大到小
                    segment.add_keyframe(KP.uniform_scale, 0, 1.34)
                    segment.add_keyframe(KP.uniform_scale, keyframe_end_time, 1.0)
                    # 反向漂移
                    segment.add_keyframe(KP.position_x, 0, 0.03)
                    segment.add_keyframe(KP.position_x, keyframe_end_time, -0.03)
                
                # 尝试添加视频特效（快闪冲击感）
                try:
                    if i % 2 == 0:
                        # 白闪：荧光爆闪
                        segment.add_effect(VideoSceneEffectType.荧光爆闪)
                    else:
                        # 黑闪：幻彩故障
                        segment.add_effect(VideoSceneEffectType.幻彩故障)
                except:
                    pass
                
                print(f"  ✓ 重复{i+1}/8 {'推镜' if zoom_in else '拉镜'} {img_name} (持续到原第9张结束，关键帧→下一张)")
        
        except Exception as e:
            print(f"  ✗ 重复{i+1}失败: {str(e)[:30]}")
            continue
    
    # 计算总时长（快闪现在是：9张 + 重复8张 = 17张的时间）
    total_flash_duration = ninth_end_time + (frame_duration * 8)
    total_opening_duration = max(duration_lead, total_flash_duration)
    
    print(f"\n✓ 片头效果已添加（总时长: {format_time(total_opening_duration)}）")
    print(f"  · 引导语: {format_time(duration_lead)} (纯文字+配音，从0秒开始)")
    print(f"  · 快闪第一轮: {format_time(ninth_end_time)} (9张，前8张叠加到第9张结束)")
    print(f"  · 快闪第二轮: {format_time(frame_duration * 8)} (重复前8张，叠加效果)")
    print(f"  · 快闪总时长: {format_time(total_flash_duration)} (关键帧打在下一张开始)")
    print(f"  · 音效: 棘轮音效与快闪同步")
    print(f"  · 三者并行播放，无背景图片")
    
    return total_opening_duration

# 配置路径（可修改）
MAPPING_FILE = r'剧本\口播文案_图片序号_对应表.txt'
IMAGE_DIR = r'image'
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
                # 🔥 第一个场景使用特殊样式（参考 build_flash.py）
                if len(success_segments) == 0 and sent_idx == 0:
                    # 第一个场景的第一句字幕：居中 + 暖白色 + 半透明黑色背景
                    project.add_text_simple(
                        text=sentence_no_punct,
                        start_time=subtitle_start,
                        duration=subtitle_duration,
                        track_name="Subtitles",
                        font=FontType.江湖体,
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
                        font=FontType.江湖体,
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
    font=FontType.江湖体,
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
print(f"  片头:   引导语 + 快闪叠加（关键帧优化）+ 音效")
print(f"          · 引导语: 1.8秒 (文字+配音，无背景图)")
print(f"          · 快闪: ~3秒 (17张叠加，关键帧打在下一张开始)")
print(f"          · 音效: 棘轮音效覆盖整个快闪")
print(f"  场景数: {len(success_segments)} 个")
print(f"  配音:   {VOICE_SPEAKER} (语速1.1倍)")
print(f"  字体:   江湖体 (字间距1)")
print(f"  字幕:   已去除所有标点符号（包括中文引号）")
print(f"          第一个场景第一句：居中+暖白色 (参考 build_flash.py)")
print(f"          其他字幕：常规样式（底部）")
print(f"  同步:   图片、字幕、音频完全一致（包含段间停顿）")
print(f"  动画:   Ken Burns缩放（1.0 ↔ 1.1）")
print(f"  分段:   超25字文案递归分段，段间0.1秒停顿")
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
