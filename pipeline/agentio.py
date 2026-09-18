#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Arena Agent 交接协议 —— 用 agent 顶替 MoneyPrinterPlus 的 LLM API 层。

MoneyPrinterPlus 的做法（`services/llm/llm_service.py`）：
    self.topic_template   = "请为以下主题扩展为详细的一篇文章,内容在{length}字以内…"
    self.keyword_template = "Please analyze the following content in english, and then extract 1-5 short English keywords…"
    self.sd_template      = "任务：将以下句子转换成Stable Diffusion图像生成模型能够理解的prompt…"
然后 `MyLLMService.generate_content()` 走 HTTP 打给 Moonshot / DeepSeek / Ollama，
每个 provider 一个 `services/llm/*_service.py`，都要 api_key。

这里**不调任何 API**：把同样的提示词落成文件，交给 Arena agent 填。
    1. `pipeline.cli agent-task --topic 外卖员的一生`
         → 写 .agent/pending/<id>.json（含提示词 + 输出格式要求）
    2. agent 读它、写回 .agent/done/<id>.json
    3. `pipeline.cli agent-apply --id <id>`
         → 校验并转成 剧本/对照表_<slug>.txt + 出图提示词_<slug>.txt

好处：提示词进了版本库（可 diff、可回滚），产出物是可复查的文本文件，
断网也能跑，且换 agent / 换模型不用改代码。
"""

import json
import os
import re
import time

from . import text as textmod

SCHEMA = "life-copy-agent-task/v1"

PENDING_DIR = ".agent/pending"
DONE_DIR = ".agent/done"

# ============================================================
# 提示词（移植自 MoneyPrinterPlus services/llm/llm_service.py，
# 按《人生副本》/ demo 库《剧本人生》的规格改写）
# ============================================================

STORY_PROMPT = """任务：为主题写一条《人生副本》短视频口播文案。

主题：{topic}

硬指标（来自 demo 库《剧本人生》的 genre 规范，不要超）：
  · 第二人称「你」，全程贴地，不用上帝视角评论
  · 总净字数 {min_chars}–{max_chars} 字（成片约 {min_minutes}–{max_minutes} 分钟）
  · 分 {min_shots}–{max_shots} 个镜头，每镜净字数 {shot_min}–{shot_max} 字
  · 每镜一句到三句，句末要有标点（这条要直接当字幕上屏）

写法要求：
  · 开局即判词：前 3 行是引导语（"今天你要体验的人生副本是……"）
  · 数字要具体、要成链（如 47 次 → 300 人 → 1/217 → 差 0.5 分），不要形容词堆砌
  · 至少三处「打脸/碾压」爽点，且爽点必须走可验证的公示通道（公示栏/证书/账单），
    不要靠旁白宣布主角赢了
  · 结尾回收开局的判词，形成闭环
  · 合规红线：不写真实人物、不写具体品牌侵权、不写违法犯罪细节教程、
    不自残自杀细节、不煽动地域/性别对立

输出格式（严格遵守，只输出 JSON，不要解释）：
{{
  "title": "一句话标题，20 字内",
  "lead_text": "片头引导语，25 字内",
  "shots": [
    {{"text": "第 1 镜字幕文案", "image_prompt": "这一镜的画面描述，30 字内"}},
    {{"text": "第 2 镜字幕文案", "image_prompt": "……"}}
  ]
}}"""

KEYWORD_PROMPT = """Please analyze the following Chinese short-video script, and then extract
3-6 short English keywords for stock-footage / image search.
Keywords separated by commas, do not need serial number, return the keywords only.

content: {topic}

（这一条对应 MoneyPrinterPlus 的 keyword_template —— 它用关键词去 Pexels/Pixabay
检索素材。本项目用本地 image/ 目录，所以关键词只用于给素材归档命名。）"""

IMAGE_STYLE_PROMPT = """任务：把每一镜的画面描述转成 Stable Diffusion 能理解的 prompt。

转换指南（沿用 MoneyPrinterPlus 的 sd_template）：
  1. 确定句子中的主要对象和场景
  2. 提取描述性形容词和环境细节
  3. 考虑图像的构图和视角
  4. 用逗号分隔不同元素
  5. 简洁，避免冗余
  6. 用英文输出

统一风格前缀（每条 prompt 都要带上，保证 88 张图是一个片子）：
{style}

期望输出示例：
  "A skinny teenage boy walking alone around a military training square,
   300 students standing and watching, hot September afternoon,
   melting rubber track, cinematic realistic style, cold tone, hard light,
   shallow depth of field, 35mm film grain"

请把下面每一镜的 image_prompt 转成英文 SD prompt，
输出 JSON 数组，长度与输入一致，只输出 JSON：
{shots}"""


def _slug(topic):
    """主题 → 文件名安全的短标识。"""
    slug = re.sub(r"[\s/\\:*?\"<>|]+", "_", (topic or "").strip())
    return slug[:40] or "untitled"


def _now_id():
    return time.strftime("%Y%m%d_%H%M%S")


def task_paths(cfg):
    root = cfg.get("agent", {}).get("root") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".agent"
    )
    pending = os.path.join(root, "pending")
    done = os.path.join(root, "done")
    return root, pending, done


# ============================================================
# 出题
# ============================================================
def create_task(cfg, topic, task_id=None, shots=None):
    """生成一份 agent 任务文件。返回 (task_id, 路径)。"""
    root, pending, done = task_paths(cfg)
    os.makedirs(pending, exist_ok=True)
    os.makedirs(done, exist_ok=True)

    agent_cfg = cfg.get("agent", {})
    shot_cfg = cfg.get("shotlist", {})
    check_cfg = cfg.get("check", {})

    task_id = task_id or f"{_now_id()}_{_slug(topic)}"
    min_chars, max_chars = agent_cfg.get("story_chars", [1200, 2500])
    min_shots = shots or agent_cfg.get("min_shots", 40)
    max_shots = agent_cfg.get("max_shots", 90)
    cps = check_cfg.get("chars_per_second", 4.4)

    payload = {
        "schema": SCHEMA,
        "id": task_id,
        "topic": topic,
        "provider": agent_cfg.get("provider", "arena-agent"),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "answer_path": os.path.join(done, f"{task_id}.json"),
        "tasks": [
            {
                "key": "story",
                "required": True,
                "prompt": STORY_PROMPT.format(
                    topic=topic,
                    min_chars=min_chars,
                    max_chars=max_chars,
                    min_minutes=round(min_chars / cps / 60, 1),
                    max_minutes=round(max_chars / cps / 60, 1),
                    min_shots=min_shots,
                    max_shots=max_shots,
                    shot_min=shot_cfg.get("min_chars", 12),
                    shot_max=shot_cfg.get("max_chars", 60),
                ),
                "answer_key": "story_json",
            },
            {
                "key": "keywords",
                "required": False,
                "prompt": KEYWORD_PROMPT.format(topic=topic),
                "answer_key": "keywords",
            },
        ],
        "constraints": {
            "min_shots": min_shots,
            "max_shots": max_shots,
            "shot_min_chars": shot_cfg.get("min_chars", 12),
            "shot_max_chars": shot_cfg.get("max_chars", 60),
            "story_min_chars": min_chars,
            "story_max_chars": max_chars,
        },
        "howto": [
            "1. 读这个文件里的 tasks[].prompt，逐条完成",
            "2. 把结果写进 answer_path 指向的 JSON 文件，格式见 answer_schema",
            "3. 运行: python -m pipeline.cli agent-apply --id %s" % task_id,
        ],
        "answer_schema": {
            "story_json": {
                "title": "str",
                "lead_text": "str",
                "shots": [{"text": "str", "image_prompt": "str"}],
            },
            "keywords": ["english keyword", "..."],
            "sd_prompts": ["english sd prompt per shot, 可选"],
        },
    }

    path = os.path.join(pending, f"{task_id}.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return task_id, path


# ============================================================
# 收卷 + 校验
# ============================================================
def load_answer(cfg, task_id):
    root, pending, done = task_paths(cfg)
    path = os.path.join(done, f"{task_id}.json")
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"找不到 agent 产出: {path}\n"
            f"请先让 agent 完成 {os.path.join(pending, task_id + '.json')} 里的任务"
        )
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def validate_answer(answer, constraints):
    """校验 agent 产出。返回 (errors, shots, lead_text, title)。"""
    errors = []
    story = answer.get("story_json") or {}
    shots = story.get("shots") or []
    lead_text = (story.get("lead_text") or "").strip()
    title = (story.get("title") or "").strip()

    if not shots:
        errors.append("story_json.shots 为空")
        return errors, [], lead_text, title

    total = 0
    for index, shot in enumerate(shots, start=1):
        caption = (shot.get("text") or "").strip() if isinstance(shot, dict) else ""
        if not caption:
            errors.append(f"第 {index} 镜: text 为空")
            continue
        net = len(textmod.plain(caption))
        total += net
        if net < constraints.get("shot_min_chars", 12):
            errors.append(f"第 {index} 镜: 仅 {net} 字，短于 {constraints['shot_min_chars']}")
        if net > constraints.get("shot_max_chars", 60):
            errors.append(f"第 {index} 镜: {net} 字，超过 {constraints['shot_max_chars']}")
        if not re.search(r"[，。！？、；：]", caption):
            errors.append(f"第 {index} 镜: 没有标点，无法切字幕 → {caption[:20]}…")

    count = len(shots)
    if count < constraints.get("min_shots", 0):
        errors.append(f"只有 {count} 镜，少于要求的 {constraints['min_shots']} 镜")
    if count > constraints.get("max_shots", 10 ** 9):
        errors.append(f"有 {count} 镜，超过要求的 {constraints['max_shots']} 镜")
    if total < constraints.get("story_min_chars", 0):
        errors.append(f"总净字数 {total}，少于要求的 {constraints['story_min_chars']}")
    if total > constraints.get("story_max_chars", 10 ** 9):
        errors.append(f"总净字数 {total}，超过要求的 {constraints['story_max_chars']}")
    if not lead_text:
        errors.append("缺 lead_text（片头引导语）")

    return errors, shots, lead_text, title


def apply_answer(cfg, task_id, out_dir=None, log=None):
    """把 agent 产出转成对照表 + 出图提示词。返回 dict。"""
    log = log or print
    root, pending, done = task_paths(cfg)
    task_path = os.path.join(pending, f"{task_id}.json")
    if not os.path.exists(task_path):
        raise FileNotFoundError(f"找不到任务文件: {task_path}")
    with open(task_path, "r", encoding="utf-8") as handle:
        task = json.load(handle)

    answer = load_answer(cfg, task_id)
    constraints = task.get("constraints", {})
    errors, shots, lead_text, title = validate_answer(answer, constraints)

    if errors:
        log(f"✗ agent 产出未通过校验，{len(errors)} 条：")
        for item in errors[:20]:
            log(f"    - {item}")
        if len(errors) > 20:
            log(f"    … 还有 {len(errors) - 20} 条")
        log("  修好 answer 文件后重跑 agent-apply")
        return {"ok": False, "errors": errors}

    shot_cfg = cfg.get("shotlist", {})
    prefix = shot_cfg.get("image_prefix", "shot")
    ext = "jpg"
    normalized = [
        {
            "seq": index,
            "text": shot["text"].strip(),
            "image": f"{prefix}_{index:02d}.{ext}",
            "chars": len(textmod.plain(shot["text"])),
            "image_prompt": (shot.get("image_prompt") or "").strip(),
        }
        for index, shot in enumerate(shots, start=1)
    ]

    out_dir = out_dir or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "剧本"
    )
    os.makedirs(out_dir, exist_ok=True)
    stem = _slug(title or task.get("topic") or task_id)

    mapping_path = os.path.join(out_dir, f"对照表_{stem}.txt")
    header = (
        f"镜头字幕对照表（共{len(normalized)}镜，横屏16:9，配合口播使用）\n"
        f"标题：{title}\n"
        f"引导语（片头文案）：{lead_text}\n"
        f"来源：arena-agent 交接任务 {task_id}\n"
        "\n格式：镜头编号 | 图片文件名 | 字幕内容（对应口播）\n\n"
    )
    with open(mapping_path, "w", encoding="utf-8") as handle:
        handle.write(textmod.shotlist_to_mapping(normalized, prefix, ext, header=header))

    style = shot_cfg.get("prompt_style", "")
    sd_prompts = answer.get("sd_prompts") or []
    prompt_path = os.path.join(out_dir, f"出图提示词_{stem}.txt")
    with open(prompt_path, "w", encoding="utf-8") as handle:
        handle.write(f"# 出图提示词（共 {len(normalized)} 镜）\n")
        handle.write(f"# 标题：{title}\n")
        handle.write(f"# 统一风格前缀：{style}\n\n")
        for index, shot in enumerate(normalized):
            english = sd_prompts[index] if index < len(sd_prompts) else ""
            handle.write(f"{shot['seq']:02d}\t{shot['image']}\t{shot['image_prompt']}\n")
            if english:
                handle.write(f"\tSD: {style}, {english}\n")

    keywords = answer.get("keywords") or []
    log(f"✓ 标题: {title}")
    log(f"✓ 镜头: {len(normalized)} 个，总净字数 {sum(s['chars'] for s in normalized)}")
    log(f"✓ 对照表: {mapping_path}")
    log(f"✓ 提示词: {prompt_path}")
    if keywords:
        log(f"✓ 素材关键词: {', '.join(keywords)}")
    log(f"  下一步: python -m pipeline.cli build --mapping \"{mapping_path}\"")

    return {
        "ok": True,
        "title": title,
        "lead_text": lead_text,
        "shots": normalized,
        "mapping_path": mapping_path,
        "prompt_path": prompt_path,
        "keywords": keywords,
    }


def list_tasks(cfg, log=None):
    """列出待办/已完成的任务。"""
    log = log or print
    root, pending, done = task_paths(cfg)
    rows = []
    for folder, state in ((pending, "待办"), (done, "已答")):
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if name.endswith(".json"):
                rows.append((state, name[:-5], os.path.join(folder, name)))
    if not rows:
        log("（没有 agent 任务。用 `agent-task --topic 主题` 出题）")
        return rows
    for state, task_id, path in rows:
        log(f"  [{state}] {task_id}")
    return rows
