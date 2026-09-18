#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""命令行入口。

    python -m pipeline.cli build                      # 用 config.yaml 生成
    python -m pipeline.cli build --style grid         # 换片头风格
    python -m pipeline.cli build --batch 剧本/*.txt    # 批量（一表一片）
    python -m pipeline.cli check                      # 只做素材预检
    python -m pipeline.cli shotlist --raw 正文.md     # demo 库原稿 → 对照表
    python -m pipeline.cli presets                    # 列出片头预设
"""

import argparse
import glob
import os
import sys

from . import script_io
from .config import apply_cli_overrides, apply_preset, known_presets, load_config
from . import agentio
from .pipeline import build, check, shotlist


def _add_common(parser):
    parser.add_argument("--config", help="配置文件路径（默认读仓库根 config.yaml）")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="pipeline", description="人生副本视频流水线（统一引擎）"
    )
    sub = parser.add_subparsers(dest="command")

    build_parser = sub.add_parser("build", help="生成剪映草稿")
    _add_common(build_parser)
    build_parser.add_argument("--style", choices=known_presets(), help="片头预设")
    build_parser.add_argument("--speaker", help="TTS 音色 id")
    build_parser.add_argument("--speed", type=float, help="语速（默认 1.05）")
    build_parser.add_argument("--backend", help="sami | edge | estimate")
    build_parser.add_argument("--group-chars", type=int, dest="group_chars", help="配音块字数")
    build_parser.add_argument("--lead-text", dest="lead_text", help="片头引导语文案")
    build_parser.add_argument("--mapping", help="对照表路径")
    build_parser.add_argument("--image-dir", dest="image_dir", help="图片目录")
    build_parser.add_argument("--drafts-root", dest="drafts_root", help="剪映草稿根目录")
    build_parser.add_argument("--name", dest="project_name", help="草稿名（默认带时间戳）")
    build_parser.add_argument("--no-cache", action="store_true", help="禁用配音缓存")
    build_parser.add_argument("--batch", nargs="*", help="批量：多个对照表（支持通配符）")
    build_parser.add_argument("--quiet", action="store_true", help="只打印关键信息")
    build_parser.add_argument("--yes", action="store_true", help="非交互，跳过所有询问")
    build_parser.add_argument("--variants", type=int, dest="variants",
                              help="混剪：一次出 N 条不重复变体（对照表里要有 // 候选池）")
    build_parser.add_argument("--seed", help="混剪变体的随机种子（固定后可复现）")
    build_parser.add_argument("--variant", type=int, help="只出指定的某一个变体")

    check_parser = sub.add_parser("check", help="对照表 + 素材预检")
    _add_common(check_parser)
    check_parser.add_argument("--mapping", help="对照表路径")
    check_parser.add_argument("--image-dir", dest="image_dir", help="图片目录")

    shot_parser = sub.add_parser("shotlist", help="demo 库 ASR 原稿 → 对照表 + 出图提示词")
    _add_common(shot_parser)
    shot_parser.add_argument("--raw", required=True, help="原稿 .md/.txt 路径")
    shot_parser.add_argument("--out", help="输出目录（默认 剧本/）")

    task_parser = sub.add_parser(
        "agent-task", help="出题：生成 Arena agent 任务文件（替代 LLM API）"
    )
    _add_common(task_parser)
    task_parser.add_argument("--topic", required=True, help="选题，例如 外卖员的一生")
    task_parser.add_argument("--id", dest="task_id", help="任务 id（默认时间戳+主题）")
    task_parser.add_argument("--shots", type=int, help="最少镜头数")

    apply_parser = sub.add_parser("agent-apply", help="收卷：把 agent 产出转成对照表")
    _add_common(apply_parser)
    apply_parser.add_argument("--id", dest="task_id", required=True, help="任务 id")
    apply_parser.add_argument("--out", help="输出目录（默认 剧本/）")

    sub.add_parser("agent-list", help="列出 agent 任务（待办/已答）")

    sub.add_parser("presets", help="列出片头预设")

    args = parser.parse_args(argv)

    if args.command == "presets":
        for name in known_presets():
            print(f"  {name}")
        return 0

    if not args.command:
        parser.print_help()
        return 1

    cfg = load_config(getattr(args, "config", None))

    if args.command == "agent-list":
        agentio.list_tasks(cfg)
        return 0

    if args.command == "agent-task":
        task_id, path = agentio.create_task(
            cfg, args.topic, task_id=args.task_id, shots=args.shots
        )
        print(f"✓ 任务已生成: {path}")
        print(f"  任务 id: {task_id}")
        print("  下一步：让 Arena agent 读这个文件、把结果写进它的 answer_path，然后：")
        print(f"  python -m pipeline.cli agent-apply --id {task_id}")
        return 0

    if args.command == "agent-apply":
        result = agentio.apply_answer(cfg, args.task_id, out_dir=args.out)
        return 0 if result.get("ok") else 1

    if args.command == "shotlist":
        shotlist(args.raw, out_dir=args.out, cfg=cfg)
        return 0

    if args.command == "check":
        if args.mapping:
            cfg["input"]["mapping_file"] = args.mapping
        if args.image_dir:
            cfg["input"]["image_dir"] = args.image_dir
        result = check(cfg=cfg)
        return 1 if result["errors"] else 0

    # ---- build ----
    cfg = apply_cli_overrides(
        cfg,
        speaker=args.speaker,
        speed=args.speed,
        backend=args.backend,
        group_chars=args.group_chars,
        style=args.style,
        lead_text=args.lead_text,
        drafts_root=args.drafts_root,
        project_name=args.project_name,
        no_cache=args.no_cache or None,
        verbose=False if args.quiet else None,
    )
    cfg = apply_preset(cfg, args.style)
    if args.mapping:
        cfg["input"]["mapping_file"] = args.mapping
    if args.image_dir:
        cfg["input"]["image_dir"] = args.image_dir
    if args.yes:
        cfg["run"]["interactive"] = False

    targets = []
    if args.batch:
        for pattern in args.batch:
            matched = sorted(glob.glob(pattern))
            targets.extend(matched or ([pattern] if os.path.exists(pattern) else []))
        targets = list(dict.fromkeys(targets))
    else:
        targets = [cfg["input"]["mapping_file"]]

    if not targets:
        print("没有找到任何对照表")
        return 1

    jobs = []
    for mapping in targets:
        entries = script_io.parse_mapping_entries(mapping)
        if args.variants and args.variants > 1:
            variants, space = script_io.distinct_variants(entries, args.variants, args.seed)
            if len(variants) < args.variants:
                print(
                    f"⚠ {os.path.basename(mapping)} 组合空间只有 {space} 种，"
                    f"只能出 {len(variants)} 条不重复变体（要求 {args.variants}）"
                )
            jobs.extend((mapping, variant) for variant, _, _ in variants)
        elif args.variant is not None:
            jobs.append((mapping, args.variant))
        else:
            jobs.append((mapping, None))

    failures = 0
    for index, (mapping, variant) in enumerate(jobs, start=1):
        if len(jobs) > 1:
            label = f"{mapping}" + (f" [变体 #{variant}]" if variant is not None else "")
            print(f"\n===== [{index}/{len(jobs)}] {label} =====")
        batch_cfg = dict(cfg)
        batch_cfg["input"] = dict(cfg["input"], mapping_file=mapping)
        if len(jobs) > 1:
            batch_cfg["project"] = dict(cfg["project"])
            batch_cfg["project"].pop("name", None)
        try:
            build(cfg=batch_cfg, variant=variant, seed=args.seed)
        except Exception as exc:  # noqa: BLE001 - 批量模式下单条失败不该中断整批
            failures += 1
            print(f"✗ 失败: {mapping} (variant={variant}) -> {type(exc).__name__}: {exc}")
            if cfg["run"].get("fail_fast"):
                return 1

    if len(jobs) > 1:
        print(f"\n批量完成：{len(jobs) - failures}/{len(jobs)} 成功")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
