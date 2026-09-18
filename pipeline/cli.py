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

from .config import apply_cli_overrides, apply_preset, known_presets, load_config
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

    check_parser = sub.add_parser("check", help="对照表 + 素材预检")
    _add_common(check_parser)
    check_parser.add_argument("--mapping", help="对照表路径")
    check_parser.add_argument("--image-dir", dest="image_dir", help="图片目录")

    shot_parser = sub.add_parser("shotlist", help="demo 库 ASR 原稿 → 对照表 + 出图提示词")
    _add_common(shot_parser)
    shot_parser.add_argument("--raw", required=True, help="原稿 .md/.txt 路径")
    shot_parser.add_argument("--out", help="输出目录（默认 剧本/）")

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

    failures = 0
    for index, mapping in enumerate(targets, start=1):
        if len(targets) > 1:
            print(f"\n===== [{index}/{len(targets)}] {mapping} =====")
        batch_cfg = dict(cfg)
        batch_cfg["input"] = dict(cfg["input"], mapping_file=mapping)
        if len(targets) > 1:
            batch_cfg["project"] = dict(cfg["project"])
            batch_cfg["project"].pop("name", None)
        try:
            build(cfg=batch_cfg)
        except Exception as exc:  # noqa: BLE001 - 批量模式下单条失败不该中断整批
            failures += 1
            print(f"✗ 失败: {mapping} -> {type(exc).__name__}: {exc}")
            if cfg["run"].get("fail_fast"):
                return 1

    if len(targets) > 1:
        print(f"\n批量完成：{len(targets) - failures}/{len(targets)} 成功")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
