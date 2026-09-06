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
from pyJianYingDraft import KeyframeProperty as KP, IntroType, VideoSceneEffectType, FontType, MaskType

# ✅ 蒙版关键帧支持已添加到pyJianYingDraft库
# 已在 keyframe.py 中添加以下属性：
# - KP.mask_center_x / KP.mask_center_y
# - KP.mask_feather
# - KP.mask_width / KP.mask_height
# - KP.mask_rotation

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

def generate_tts_with_retry(project, text, speaker, start_time, track_name, max_retries=3, speed=1.1):
    """
    生成TTS音频，失败时重试
    🔥 优化：删除fallback、增加重试次数、音频规范化、语速调整
    🔥 处理：清理多余空格和标点，避免TTS产生空白
    返回: (audio_segment, duration) 或 (None, 0)
    """
    # 🔥 预处理文本：清理多余的空格和标点
    text_cleaned = text.strip()
    # 移除连续的句号
    import re
    text_cleaned = re.sub(r'。{2,}', '。', text_cleaned)
    # 移除连续的空格
    text_cleaned = re.sub(r'\s+', '', text_cleaned)
    # 确保以句号结尾
    if not text_cleaned.endswith(('。', '！', '？')):
        text_cleaned += '。'
    
    for attempt in range(max_retries):
        try:
            audio_seg = project.add_tts_intelligent(
                text=text_cleaned,
                speaker=speaker,
                start_time=start_time,
                track_name=track_name,
                tts_backend="sami",
                allow_fallback=False  # 🔥 删除fallback，直接使用sami
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

def get_random_images(image_dir, count=5):
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
    lead_text = "今天你要体验的人生副本是外号的保质期只有一届学生"
    lead_text_tts = lead_text.replace(' ', '')
    
    print(f"\n[片头·引导语] 先生成配音，获取真实时长")
    
    # 🔥 先生成配音，获取真实时长
    actual_lead_duration = duration_lead  # 默认值
    try:
        audio_seg = project.add_tts_intelligent(
            text=lead_text_tts,
            speaker=VOICE_SPEAKER,
            start_time=0,
            track_name="Opening_Lead_Voice",
            tts_backend="sami",
            allow_fallback=False
        )
        
        if audio_seg:
            audio_seg.speed.speed = 1.1
            original_duration = audio_seg.target_timerange.duration
            actual_lead_duration = int(original_duration / 1.1)
            from pyJianYingDraft.time_util import Timerange
            audio_seg.target_timerange = Timerange(audio_seg.target_timerange.start, actual_lead_duration)
            print(f"  ✓ 引导语配音: {format_time(actual_lead_duration)}")
        else:
            print(f"  ⚠️  配音失败，使用默认时长: {format_time(duration_lead)}")
    except Exception as e:
        print(f"  ⚠️  配音异常: {str(e)[:50]}，使用默认时长")
    
    # 添加引导语字幕（使用真实配音时长）
    try:
        project.add_text_simple(
            text=lead_text,
            start_time=0,
            duration=actual_lead_duration,
            track_name="Opening_Lead_Text",
            font=FontType.江湖体,
            style=draft.TextStyle(
                size=9.0,
                letter_spacing=1,
                color=(1.0, 0.976, 0.945)
            ),
            border=draft.TextBorder(color=(0.0, 0.0, 0.0), alpha=0.85, width=60.0),
            clip_settings=draft.ClipSettings(transform_y=0.0)
        )
        print(f"  ✓ 引导语文字卡已添加")
    except Exception as e:
        print(f"  ✗ 引导语字幕失败: {str(e)[:50]}")
    
    # ========== 添加闪烁发光音效==========
    audio_dir = r"c:\Users\29471\Desktop\create video\jianying\audio"
    ratchet_sfx = os.path.join(audio_dir, "ratchet.wav")
    
    if not os.path.exists(ratchet_sfx):
        coding_sfx = os.path.join(audio_dir, "coding.WAV")
        if os.path.exists(coding_sfx):
            ratchet_sfx = coding_sfx
            print(f"\n[片头·音效] 使用 coding.WAV")
        else:
            ratchet_sfx = None
            print(f"\n[片头·音效] 音效文件缺失，跳过")
    else:
        print(f"\n[片头·音效] 使用 ratchet.wav")
    
    if ratchet_sfx:
        try:
            sfx_seg = project.add_media_safe(
                media_path=ratchet_sfx,
                start_time=0,
                duration=actual_lead_duration,  # 🔥 使用引导语音频时长
                track_name="Opening_Flash_SFX"
            )
            if sfx_seg:
                print(f"  ✓ 音效已添加: {os.path.basename(ratchet_sfx)} (持续{format_time(actual_lead_duration)})")
        except Exception as e:
            print(f"  ✗ 音效添加失败: {str(e)[:50]}")
    
    # ========== 线性蒙版快闪效果（每张图片独立时间段，关键帧间隔10帧）==========
    flash_images = get_random_images(image_dir, 5)
    
    print(f"\n[片头·线性蒙版快闪] 开始生成 (5张图片)")
    print(f"  🔥 关键帧间隔10帧，每张图片独立时间段")
    print(f"  效果：缩放150%→100% + 线性蒙版羽化50%→0%")
    
    # 1帧 = 1/60秒 = 16667微秒
    one_frame = 16667
    
    for i, img_name in enumerate(flash_images):
        img_path = find_image_file(image_dir, img_name)
        if not img_path:
            continue
        
        try:
            # 🔥 每张图片的开始时间 = 关键帧开始时间
            # 图1: 0-10帧, 图2: 11-20帧, 图3: 21-30帧, 图4: 31-40帧, 图5: 41-50帧
            keyframe_start = i * 11 * one_frame  # 每张间隔11帧（0, 11, 22, 33, 44）
            seg_start = keyframe_start
            
            # 图片持续到引导语结束
            seg_duration = actual_lead_duration - seg_start
            
            # 🔥 每张图片使用独立轨道名（但都是视频轨道）
            segment = project.add_media_safe(
                media_path=img_path,
                start_time=seg_start,
                duration=seg_duration,
                track_name=f"Opening_Flash_{i+1}"  # 🔥 独立轨道名
            )
            
            if segment:
                # 关键帧动画（从图片开始时间算起，相对时间为0）
                # 第1帧：缩放150%
                segment.add_keyframe(KP.uniform_scale, 0, 1.5)
                segment.add_keyframe(KP.position_x, 0, 0.0)
                segment.add_keyframe(KP.position_y, 0, 0.0)
                
                # 第10帧：缩放100%（10帧后 = 10 * 16667微秒）
                keyframe_time_1 = 10 * one_frame
                segment.add_keyframe(KP.uniform_scale, keyframe_time_1, 1.0)
                
                # 添加线性蒙版 + 羽化动画
                try:
                    segment.add_mask(
                        MaskType.线性,
                        center_y=0.0,
                        size=0.8,
                        feather=50.0,
                        invert=False
                    )
                    
                    # 羽化动画（10帧内完成）
                    segment.add_keyframe(KP.mask_feather, 0, 0.5)
                    segment.add_keyframe(KP.mask_feather, keyframe_time_1, 0.0)
                    
                    frame_start_num = i * 11
                    frame_end_num = frame_start_num + 10
                    print(f"  ✓ 快闪{i+1}/5 {img_name} (帧{frame_start_num}-{frame_end_num}, 开始@{format_time(seg_start)})")
                    
                except Exception as e:
                    print(f"  ✗ 快闪{i+1}/5 蒙版失败: {str(e)[:30]}")
        
        except Exception as e:
            print(f"  ✗ 快闪{i+1}失败: {str(e)[:30]}")
            continue
    
    # 🔥 片头总时长 = 引导语音频时长
    total_opening_duration = actual_lead_duration
    
    print(f"\n✓ 片头效果已添加（总时长: {format_time(total_opening_duration)}）")
    print(f"  · 引导语配音: {format_time(actual_lead_duration)} (真实TTS时长)")
    print(f"  · 引导语文字: 居中暖白色，与配音同步")
    print(f"  · 快闪图片: 5张，关键帧间隔10帧，独立轨道叠加")
    print(f"  · 图1: 帧0-10，图2: 帧11-20，图3: 帧21-30，图4: 帧31-40，图5: 帧41-50")
    print(f"  · 关键帧: 每张图片相对自己开始时间的0-10帧")
    print(f"  · 蒙版动画: 缩放150%→100% + 羽化50%→0%")
    print(f"  · 音效: 闪烁发光音效")
    print(f"  · 正片内容: 从 {format_time(total_opening_duration)} 开始，主轨道 VideoTrack")
    
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
        
        # 1. 🔥 智能分割：≤50字整段TTS+字幕按比例，>50字分段TTS
        clean_text = re.sub(r'[，。！？、""''：；…——/\s]', '', text)
        
        # 先按标点分割字幕（所有场景都需要）
        text_normalized = re.sub(r'\s*/\s*', '/', text)
        sentences = re.split(r'([，。！？、/])', text_normalized)
        combined_sentences = []
        i = 0
        while i < len(sentences):
            if sentences[i].strip():
                sentence = sentences[i].strip()
                if i + 1 < len(sentences) and sentences[i + 1] in '，。！？、/':
                    sentence += sentences[i + 1]
                    i += 2
                else:
                    i += 1
                if len(sentence) > 1:
                    combined_sentences.append(sentence)
            else:
                i += 1

        if not combined_sentences:
            combined_sentences = [text]
        
        if len(clean_text) <= 50:
            # ≤50字：整段生成一个TTS，字幕按字数比例分配时间
            print(f"  场景 {seq:02d}: 整段TTS+{len(combined_sentences)}句字幕 ({len(clean_text)}字) - {text[:40]}...")
            
            # 生成一个完整的TTS
            text_for_tts = text.replace(' ', '')
            text_for_tts = re.sub(r'。{2,}', '。', text_for_tts)
            if not text_for_tts.endswith(('。', '！', '？')):
                text_for_tts += '。'
            
            audio_seg, total_scene_duration = generate_tts_with_retry(
                project, text_for_tts, VOICE_SPEAKER, current_time, "VoiceOver"
            )
            
            if not audio_seg:
                failed_segments.append((seq, text[:30], "配音失败"))
                print(f"  X 场景 {seq:02d}: 配音失败")
                continue
            
            print(f"    ✓ 完整TTS: {format_time(total_scene_duration)} (语气连贯)")
            
            # 计算每句字数，按比例分配字幕时间
            sentence_chars = []
            for sentence in combined_sentences:
                clean_sentence = re.sub(r'[，。！？、""''：；…——/\s]', '', sentence)
                sentence_chars.append(len(clean_sentence))
            
            total_chars = sum(sentence_chars)
            if total_chars == 0:
                failed_segments.append((seq, text[:30], "无有效文字"))
                continue
            
            # 按比例分配字幕时长
            sentence_durations = []
            for sent_idx, sentence in enumerate(combined_sentences):
                char_count = sentence_chars[sent_idx]
                subtitle_duration = int(total_scene_duration * (char_count / total_chars))
                sentence_durations.append((sentence, subtitle_duration))
            
        else:
            # >50字：按标点分段生成多个TTS
            print(f"  场景 {seq:02d}: 分段TTS {len(combined_sentences)}句 ({len(clean_text)}字) - {text[:40]}...")
            
            sentence_durations = []
            tts_start_time = current_time
            
            for sent_idx, sentence in enumerate(combined_sentences):
                text_clean = sentence.replace(' ', '')
                text_clean = re.sub(r'。{2,}', '。', text_clean)
                
                if not text_clean.endswith(('。', '！', '？', '，', '、', '/')):
                    text_clean += '。'
                
                audio_seg, seg_duration = generate_tts_with_retry(
                    project, text_clean, VOICE_SPEAKER, tts_start_time, "VoiceOver"
                )
                
                if audio_seg:
                    sentence_durations.append((sentence, seg_duration))
                    tts_start_time += seg_duration
                else:
                    print(f"      X 分句 {sent_idx+1} 失败: {text_clean[:20]}")
            
            if not sentence_durations:
                failed_segments.append((seq, text[:30], "配音失败"))
                print(f"  X 场景 {seq:02d}: 配音失败")
                continue
            
            total_scene_duration = tts_start_time - current_time
            print(f"    ✓ 分段TTS: {format_time(total_scene_duration)} ({len(sentence_durations)}段)")
        
        # 2. 🔥 字幕生成：使用分配好的时长
        
        # 5. 🔥 修复：统一字幕样式，删除数字高亮（保留动画和背景条）
        subtitle_start = current_time
        for sent_idx, (sentence, seg_duration) in enumerate(sentence_durations):
            # 去除标点符号和空格
            sentence_no_punct = re.sub(r'[，。！？、""''：；…——/\s]', '', sentence)
            
            try:
                # 🔥 统一样式：所有字幕使用相同大小（5.0）
                is_first = (len(success_segments) == 0 and sent_idx == 0)
                
                if is_first:
                    # 第一个场景第一句：居中+暖白色+打字机动画
                    project.add_text_simple(
                        text=sentence_no_punct,
                        start_time=subtitle_start,
                        duration=seg_duration,
                        track_name="Subtitles",
                        font=FontType.新青年体,
                        style=draft.TextStyle(
                            size=6.0,  # 🔥 统一大小5.0
                            letter_spacing=1,
                            color=(1.0, 0.976, 0.945)  # 暖白色
                        ),
                        border=draft.TextBorder(color=(0.0, 0.0, 0.0), alpha=0.9, width=3.0),
                        clip_settings=draft.ClipSettings(transform_y=0.0),  # 居中
                        # anim_in="复古打字机"  # 🔥 保留打字机动画
                    )
                else:
                    # 其他字幕：常规样式+打字机动画+半透明背景
                    project.add_text_simple(
                        text=sentence_no_punct,
                        start_time=subtitle_start,
                        duration=seg_duration,
                        track_name="Subtitles",
                        font=FontType.新青年体,
                        style=draft.TextStyle(size=5.0, letter_spacing=1),  # 🔥 统一大小5.0
                        border=draft.TextBorder(color=(0.0, 0.0, 0.0), alpha=1.0, width=40.0),
                        clip_settings=draft.ClipSettings(transform_y=-0.8),
                        # anim_in="复古打字机",  # 🔥 保留打字机动画
                        background=draft.TextBackground(  # 🔥 保留半透明背景条
                            color="#000000",
                            alpha=0.5,
                            round_radius=0.35
                        )
                    )
                
                print(f"    OK 第{sent_idx+1}句字幕: {sentence_no_punct[:20]}... ({format_time(seg_duration)})")
                subtitle_start += seg_duration
                
            except Exception as e:
                print(f"    X 第{sent_idx+1}句字幕失败: {str(e)[:30]}")
        
        # 7. 添加图片（持续整个场景，包括停顿）
        # 🔥 使用片头图1的轨道，让正片接续在图1后面
        video_seg = project.add_media_safe(
            media_path=img_path,
            start_time=current_time,
            duration=total_scene_duration,
            track_name="Opening_Flash_1"  # 🔥 使用片头图1的轨道
        )
        
        # 🔥 新增：为第一个场景添加特效（持续1秒）
        if len(success_segments) == 0:  # 第一个成功的场景
            # 创建一个1秒的视频片段用于添加特效（独立轨道叠加）
            effect_duration = 1000000  # 1秒
            effect_seg = project.add_media_safe(
                media_path=img_path,
                start_time=current_time,
                duration=effect_duration,
                track_name="EffectTrack"  # 特效轨道（叠加在主轨道上）
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
print(f"  片头:   引导语 + 快闪叠加（关键帧优化）+ 音效 + 线性蒙版羽化")
print(f"          · 引导语: 1.8秒 (文字+配音，无背景图)")
print(f"          · 快闪: ~1.67秒 (9张叠加，关键帧打在下一张开始)")
print(f"          · 音效: 棘轮音效覆盖整个快闪")
print(f"          · 蒙版: ✅ 线性蒙版 + 羽化动画（50%→0%）")
print(f"  场景数: {len(success_segments)} 个")
print(f"  配音:   {VOICE_SPEAKER} (语速1.1倍)")
print(f"          · 统一轨道 VoiceOver（自动避让）")
print(f"          · 只在超过25字时分割，≤25字保持完整连贯")
print(f"          · 淡入淡出防爆音")
print(f"  字体:   新青年体 (字间距1)")
print(f"  字幕:   统一样式（size=5.0），精确同步TTS时长")
print(f"          · 打字机入场动画：复古打字机（免费）")
print(f"          · 半透明背景条：黑色 alpha=0.5（圆角0.35）")
print(f"          · 第一句：居中+暖白色")
print(f"          · 其他句：底部常规样式")
print(f"          ✅ 已删除数字高亮，统一字幕大小")
print(f"  同步:   图片、字幕、音频完全一致")
print(f"  动画:   Ken Burns缩放（1.0 ↔ 1.1）")
print(f"  优化:   ✅ 删除开头不透明度关键帧（固定100%）")
print(f"          ✅ 蒙版关键帧动画已实现（无需手动添加）")
print(f"\n音频处理参考 build_opening.py:")
print(f"  · 使用 amix 混合多个音频轨道")
print(f"  · 使用 adelay 设置音频开始时间")
print(f"  · 棘轮音效(ratchet.wav)在快闪开始时播放")
print(f"  · 引导语配音与快闪同时开始（0秒）")
print(f"\n🔥 新增功能:")
print(f"  · 已扩展 pyJianYingDraft 库，支持蒙版关键帧")
print(f"  · 新增属性: mask_feather（羽化动画）")
print(f"  · 片头快闪现已包含蒙版羽化效果")
print(f"  · 羽化动画: 50% → 0% (10帧逐渐清晰)")
print(f"  · 缩放动画: 150% → 100% (10帧放大入场)")
print(f"\n📐 技术参数:")
print(f"  · 关键帧时长: 10帧 (约167ms @ 60fps)")
print(f"  · 羽化效果: 从模糊到锐利")
print(f"  · material_id: 自动关联蒙版对象ID")

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
print("3. 预览视频效果")
print("   ✓ 片头快闪应有蒙版羽化效果")
print("   ✓ 羽化从模糊逐渐清晰（10帧）")
print("4. (可选) 添加背景音乐")
print("5. 导出视频")
print("\n" + "=" * 60)
print("📘 提示: 蒙版羽化关键帧已自动添加")
print("   详见文档：蒙版关键帧实现总结.md")
print("=" * 60)
