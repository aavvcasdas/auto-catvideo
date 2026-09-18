#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""编排层：解析 → 预检 → 建草稿 → 片头 → 配音 → 正片 → 保存 → 报告。

用法（等价于原来那 4 个脚本，但一套代码）：

    python -m pipeline.cli build --style mask_flash
    python -m pipeline.cli build --style grid --speaker ICL_zh_female_manbo_jianying
    python -m pipeline.cli build --backend estimate        # 离线跑通全流程
    python -m pipeline.cli check                           # 只做素材预检
    python -m pipeline.cli shotlist --raw 作品正文.md      # demo 库原稿 → 对照表
"""

import json
import os
import sys
from datetime import datetime

from . import body as bodymod
from . import openings, script_io, text as textmod
from .config import apply_preset, load_config
from .tts import TTSEngine, format_time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _bootstrap_skill_path():
    """把 jianying-editor-skill-main/scripts 加进 sys.path（JyProject 在那里）。"""
    skill_scripts = os.path.join(REPO_ROOT, "jianying-editor-skill-main", "scripts")
    vendor = os.path.join(skill_scripts, "vendor")
    for path in (skill_scripts, vendor):
        if path not in sys.path:
            sys.path.insert(0, path)
    return skill_scripts


class RunResult(dict):
    """运行结果（同时可 json.dumps）。"""


def build(cfg=None, log=None, config_path=None, preset=None, overrides=None, variant=None, seed=None):
    """生成一条视频草稿。返回 RunResult。

    variant: 混剪变体序号（None = 每镜取候选池第 1 个，与旧行为一致）。
    """
    log = log or print
    cfg = cfg or load_config(config_path)
    cfg = apply_preset(cfg, preset)
    for key, value in (overrides or {}).items():
        if value is not None:
            section, _, name = key.partition(".")
            cfg.setdefault(section, {})[name] = value

    _bootstrap_skill_path()
    from jy_wrapper import JyProject  # noqa: PLC0415 - 需要先注入 sys.path

    mapping_file = cfg["input"]["mapping_file"]
    image_dir = cfg["input"]["image_dir"]
    run_cfg = cfg["run"]
    verbose = bool(run_cfg.get("verbose", True))
    say = (lambda msg: log(msg)) if verbose else (lambda msg: None)

    log("=" * 60)
    log("人生副本视频生成（统一引擎 v2）")
    log("=" * 60)

    # ---------- 1. 解析对照表 ----------
    if not os.path.exists(mapping_file):
        raise FileNotFoundError(f"对照表不存在: {mapping_file}")
    mappings = script_io.parse_mapping_file(mapping_file, variant=variant, seed=seed)
    combos = script_io.count_variants(mapping_file)
    log(f"[1/5] 对照表: {len(mappings)} 个镜头  ({mapping_file})")
    if combos > 1:
        log(f"      混剪组合空间 {combos} 种"
            + (f"，本次取变体 #{variant}" if variant is not None else "，本次取每镜第 1 候选"))
    if not mappings:
        raise ValueError("对照表没有解析到任何镜头")

    # ---------- 2. 预检 ----------
    check = script_io.preflight(mappings, image_dir, cfg.get("check"))
    log("[2/5] 素材预检")
    for line in script_io.format_preflight(check).splitlines():
        log("      " + line)

    if check["errors"]:
        if not run_cfg.get("skip_missing_images", True):
            raise RuntimeError(f"预检失败 {len(check['errors'])} 条，已中止")
        if run_cfg.get("interactive", True) and sys.stdin and sys.stdin.isatty():
            answer = input("\n存在错误，是否继续（问题镜头将被跳过）? [y/N]: ")
            if answer.strip().lower() != "y":
                log("已取消")
                return RunResult(status="CANCELLED", preflight=check)
        else:
            log("      （非交互模式，自动跳过问题镜头；interactive: true + 终端下会询问）")

    # ---------- 3. 建草稿 ----------
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    project_name = cfg["project"].get("name") or f"{cfg['project']['name_prefix']}_{timestamp}"
    if variant is not None and not cfg["project"].get("name"):
        project_name = f"{project_name}_v{variant:02d}"
    project_kwargs = dict(
        project_name=project_name,
        width=int(cfg["project"].get("width", 1920)),
        height=int(cfg["project"].get("height", 1080)),
        overwrite=True,
    )
    if cfg["project"].get("drafts_root"):
        project_kwargs["drafts_root"] = cfg["project"]["drafts_root"]

    log(f"[3/5] 创建剪映草稿: {project_name}")
    project = JyProject(**project_kwargs)

    tts_engine = TTSEngine(cfg["tts"], log=log)
    if tts_engine.backend == "estimate" and not cfg["tts"].get("_standin_audio"):
        cfg["tts"]["_standin_audio"] = _find_standin_audio(cfg)
        tts_engine.cfg = cfg["tts"]
        tts_engine.cache_enabled = False
    log(
        f"      音色 {tts_engine.speaker} | 语速 {tts_engine.speed} | "
        f"后端 {tts_engine.backend} | 缓存 {'开' if tts_engine.cache_enabled else '关'}"
    )
    openings.validate_opening_config(cfg, log)

    first_image = mappings[0][2] if mappings else None
    opening_duration = openings.build_opening(
        project, cfg, script_io.find_image_file, tts_engine, say, first_scene_image=first_image
    )
    log(f"      片头时长 {format_time(opening_duration)}")

    # ---------- 4. 配音 + 正片 ----------
    log("[4/5] 配音与正片")
    scenes, failed = bodymod.prepare_scenes(mappings, script_io.find_image_file, image_dir, cfg)
    scene_durations, voice_end, failed_seqs = bodymod.synthesize_voice(
        project, scenes, tts_engine, cfg, opening_duration, say
    )
    for seq in failed_seqs:
        failed.append({"seq": seq, "text": "", "reason": "配音失败"})

    current_time, results = bodymod.render_body(
        project, scenes, scene_durations, failed_seqs, cfg, opening_duration, say
    )
    total_duration = current_time

    if not results:
        raise RuntimeError("没有成功生成任何镜头")

    bodymod.add_disclaimer(project, cfg, opening_duration, total_duration - opening_duration, say)
    bodymod.add_bgm(project, cfg, total_duration, say)

    # ---------- 5. 保存 + 报告 ----------
    log("[5/5] 保存草稿")
    save_result = project.save()
    draft_path = save_result.get("draft_path") if isinstance(save_result, dict) else save_result

    result = RunResult(
        status="SUCCESS",
        project_name=project_name,
        draft_path=draft_path,
        opening_us=opening_duration,
        total_us=total_duration,
        total_s=round(total_duration / 1e6, 2),
        shots=len(results),
        failed=failed,
        preflight=check,
        tts=tts_engine.stats,
        results=results,
        style=(cfg.get("opening") or {}).get("style"),
        speaker=tts_engine.speaker,
    )

    if run_cfg.get("srt", True) and draft_path:
        result["srt_path"] = _export_srt(results, opening_duration, cfg, draft_path)
    if run_cfg.get("report", True):
        result["report_path"] = _export_report(result, cfg)
    if (cfg.get("publish") or {}).get("enabled", True):
        result["publish_path"] = _export_publish_manifest(result, cfg, mapping_file)

    log("")
    log("=" * 60)
    log(f"✅ 完成: {project_name}")
    log(f"   草稿: {draft_path}")
    log(f"   时长: {format_time(total_duration)}（片头 {format_time(opening_duration)}）")
    log(f"   镜头: {len(results)} 成功 / {len(failed)} 失败")
    log(
        f"   配音: 请求 {tts_engine.stats['requests']} 次，缓存命中 "
        f"{tts_engine.stats['cache_hits']} 次，失败 {tts_engine.stats['failures']} 次"
    )
    if result.get("srt_path"):
        log(f"   字幕: {result['srt_path']}")
    if result.get("report_path"):
        log(f"   报告: {result['report_path']}")
    if result.get("publish_path"):
        log(f"   发布: {result['publish_path']}")
    log("=" * 60)
    return result


# ============================================================
# 导出
# ============================================================
def _srt_timestamp(microseconds):
    total_ms = int(round((microseconds or 0) / 1000))
    hours, rest = divmod(total_ms, 3600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, ms = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{ms:03d}"


def _export_srt(results, opening_duration, cfg, draft_path):
    """把字幕轨导出成 .srt，方便肉眼核对时间轴是否跟配音对得上。"""
    lines, index, cursor = [], 1, opening_duration
    for shot in results:
        shot_cursor = cursor
        for subtitle in shot["subtitles"]:
            duration = subtitle["duration_us"]
            lines.append(str(index))
            lines.append(f"{_srt_timestamp(shot_cursor)} --> {_srt_timestamp(shot_cursor + duration)}")
            lines.append(subtitle["text"])
            lines.append("")
            index += 1
            shot_cursor += duration
        cursor += shot["duration_us"]

    path = os.path.join(os.path.dirname(draft_path) or ".", f"{os.path.basename(draft_path)}.srt")
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines))
        return path
    except OSError:
        return None


def _export_report(result, cfg):
    report_dir = cfg["run"].get("report_dir") or os.path.join(REPO_ROOT, "output")
    os.makedirs(report_dir, exist_ok=True)
    path = os.path.join(report_dir, f"run_report_{result['project_name']}.json")
    payload = {key: value for key, value in result.items() if key != "results"}
    payload["shots"] = [
        {
            "seq": item["seq"],
            "image": item["image"],
            "start_s": round(item["start_us"] / 1e6, 3),
            "duration_s": round(item["duration_us"] / 1e6, 3),
            "subtitles": len(item["subtitles"]),
            "text": item["text"][:60],
        }
        for item in result["results"]
    ]
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return path


def _find_standin_audio(cfg):
    """estimate 模式的替身音频：优先用项目里的 coding.WAV。"""
    audio_dir = cfg["input"]["audio_dir"]
    for name in ("coding.WAV", "coding.wav", "ratchet.wav"):
        candidate = os.path.join(audio_dir, name)
        if os.path.exists(candidate):
            return candidate
    return None


# ============================================================
# 子命令：素材预检
# ============================================================
def check(cfg=None, config_path=None, log=None):
    log = log or print
    cfg = cfg or load_config(config_path)
    mapping_file = cfg["input"]["mapping_file"]
    mappings = script_io.parse_mapping_file(mapping_file)
    result = script_io.preflight(mappings, cfg["input"]["image_dir"], cfg.get("check"))
    log(f"对照表: {mapping_file}")
    log(script_io.format_preflight(result))
    return result


# ============================================================
# 子命令：demo 库原稿 → 对照表
# ============================================================
def shotlist(raw_path, out_dir=None, cfg=None, config_path=None, log=None):
    """把 aavvcasdas/demo《作品》的 ASR 原稿转成对照表 + 出图提示词。"""
    log = log or print
    cfg = cfg or load_config(config_path)
    shot_cfg = cfg["shotlist"]

    with open(raw_path, "r", encoding="utf-8") as handle:
        raw_text = handle.read()

    lead_text, shots = textmod.build_shotlist(
        raw_text,
        target_chars=int(shot_cfg.get("target_chars", 40)),
        min_chars=int(shot_cfg.get("min_chars", 12)),
        max_chars=int(shot_cfg.get("max_chars", 60)),
        lead_lines=int(shot_cfg.get("lead_lines", 3)),
    )
    if not shots:
        raise ValueError(f"没有从 {raw_path} 解析出任何镜头")

    out_dir = out_dir or os.path.join(REPO_ROOT, "剧本")
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(raw_path))[0]
    stem = stem if stem not in ("正文", "main") else os.path.basename(os.path.dirname(raw_path)) or stem

    mapping_path = os.path.join(out_dir, f"对照表_{stem}.txt")
    with open(mapping_path, "w", encoding="utf-8") as handle:
        handle.write(
            textmod.shotlist_to_mapping(
                shots,
                shot_cfg.get("image_prefix", "shot"),
                header=script_io_header(lead_text, shots),
            )
        )

    prompt_path = os.path.join(out_dir, f"出图提示词_{stem}.txt")
    with open(prompt_path, "w", encoding="utf-8") as handle:
        handle.write(
            textmod.shotlist_to_image_prompts(
                shots, shot_cfg.get("prompt_style", ""), shot_cfg.get("image_prefix", "shot")
            )
        )

    log(f"✓ 引导语: {lead_text}")
    log(f"✓ 镜头数: {len(shots)}（平均 {sum(s['chars'] for s in shots) // len(shots)} 字/镜）")
    log(f"✓ 对照表: {mapping_path}")
    log(f"✓ 提示词: {prompt_path}")
    if textmod.looks_unpunctuated(raw_text):
        log(
            "⚠ 原稿是无标点的 ASR 口播体，上面的标点/断句是按行末语气词"
            "规则补的（见 pipeline.text.punctuate_lines）。"
        )
        log("  出片前请人工过一遍对照表，尤其是「说：」引出的对白和长句切分。")
    return {"lead_text": lead_text, "shots": shots, "mapping_path": mapping_path, "prompt_path": prompt_path}


def script_io_header(lead_text, shots):
    return (
        f"镜头字幕对照表（共{len(shots)}镜，横屏16:9，配合口播使用）\n"
        f"引导语（片头文案）：{lead_text}\n"
        "\n格式：镜头编号 | 图片文件名 | 字幕内容（对应口播）\n\n"
    )


# ============================================================
# 发布清单
# ------------------------------------------------------------
# MoneyPrinterPlus 用 Selenium 自动发布（services/publisher/*，config 里
# publisher.douyin/kuaishou/xiaohongshu/shipinhao 各有 title_prefix、collection，
# 快手还有 domain.level1/level2）。那套要真实浏览器 + 已登录会话，只能在
# 你本机跑，所以这里只把它真正有价值的部分搬过来：**发布元数据 schema**。
# 出片时顺手生成一份清单，手动上传时照抄即可，不需要 API 也不需要 Selenium。
# ============================================================
def _read_mapping_meta(mapping_file):
    """从对照表头部读 标题 / 引导语（agent-apply 会写进去）。"""
    meta = {}
    try:
        with open(mapping_file, "r", encoding="utf-8") as handle:
            for line in handle.readlines()[:12]:
                line = line.strip()
                if line.startswith("标题："):
                    meta["title"] = line.split("：", 1)[1].strip()
                elif line.startswith("引导语（片头文案）："):
                    meta["lead_text"] = line.split("：", 1)[1].strip()
    except OSError:
        pass
    return meta


def _export_publish_manifest(result, cfg, mapping_file):
    publish_cfg = cfg.get("publish") or {}
    meta = _read_mapping_meta(mapping_file)
    title_body = meta.get("title") or result["project_name"]
    prefix = publish_cfg.get("title_prefix", "")
    title = f"{prefix}{title_body}" if prefix else title_body

    hashtags = publish_cfg.get("hashtags") or []
    collection = publish_cfg.get("collection", "")
    platforms = publish_cfg.get("platforms") or ["douyin", "kuaishou", "xiaohongshu", "shipinhao"]
    duration_s = result.get("total_s", 0)

    report_dir = cfg["run"].get("report_dir") or os.path.join(REPO_ROOT, "output")
    os.makedirs(report_dir, exist_ok=True)
    path = os.path.join(report_dir, f"发布清单_{result['project_name']}.md")

    lines = [
        f"# 发布清单 · {result['project_name']}",
        "",
        f"- 成片时长：{result.get('total_s', 0)}s（片头 {round(result.get('opening_us', 0) / 1e6, 1)}s）",
        f"- 镜头数：{result.get('shots', 0)}",
        f"- 草稿：`{result.get('draft_path')}`",
        f"- 风格：{result.get('style')} ｜ 音色：{result.get('speaker')}",
        f"- 叠甲：{(cfg.get('disclaimer') or {}).get('text', '')}",
        "",
        "## 标题",
        "",
        f"> {title}",
        "",
        "## 话题标签",
        "",
        (" ".join(f"#{tag}" for tag in hashtags) if hashtags else "（config.yaml 的 publish.hashtags 里配）"),
        "",
        "## 各平台要点",
        "",
        "| 平台 | 合集 | 备注 |",
        "| --- | --- | --- |",
    ]
    notes = {
        "douyin": "标题 ≤ 55 字；合集要在创作中心先建好",
        "kuaishou": "需选领域（config 里 publish.kuaishou_domain: 一级/二级）",
        "xiaohongshu": "标题 ≤ 20 字，正文另写；封面单独出",
        "shipinhao": "可勾选原创声明；标题 ≤ 30 字",
    }
    for platform in platforms:
        extra = ""
        if platform == "kuaishou":
            domain = publish_cfg.get("kuaishou_domain")
            extra = f"（领域：{domain}）" if domain else ""
        lines.append(f"| {platform} | {collection or '—'} | {notes.get(platform, '')}{extra} |")

    lines += [
        "",
        "## 引导语（可用作视频简介第一句）",
        "",
        f"> {meta.get('lead_text') or (cfg.get('opening') or {}).get('lead_text', '')}",
        "",
        "## 自查",
        "",
        "- [ ] 字幕与配音对齐（对照 `.srt`）",
        "- [ ] 叠甲文字在画面内、不挡字幕",
        "- [ ] 无真实人物 / 品牌 / 违禁内容",
        "- [ ] 封面单独导出",
        "",
    ]
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines))
        return path
    except OSError:
        return None
