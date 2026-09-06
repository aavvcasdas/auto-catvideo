#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
检查剪映草稿里「蒙版关键帧」的真实 JSON 语法

为什么需要它
------------
本仓库 vendor 的 pyJianYingDraft 里，蒙版关键帧属性（KFTypeMaskCenterY 等）
是**被人手动加进去的，上游官方库里并没有这几个枚举值**。
也就是说这些属性名是猜的，未经剪映验证。

怎么用（在你装了剪映的电脑上跑）
--------------------------------
1. 在剪映里新建一个草稿，随便拖一张图进去
2. 给它加一个「线性蒙版」
3. 在蒙版参数栏的「位置」上打两个关键帧（把蒙版从上移到下）
4. 保存草稿，关掉剪映
5. 运行：  python 检查蒙版关键帧语法.py
   （或指定草稿名：python 检查蒙版关键帧语法.py 草稿名字）

脚本会把剪映**自己写出来**的蒙版关键帧结构打印出来，
我们照着它改代码就一定对。
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "jianying-editor-skill-main", "scripts"))


def find_drafts_root():
    from utils.formatters import get_default_drafts_root
    return get_default_drafts_root()


def load_draft(path):
    for name in ("draft_info.json", "draft_content.json"):
        f = os.path.join(path, name)
        if os.path.exists(f):
            with open(f, "r", encoding="utf-8") as fp:
                return json.load(fp), f
    return None, None


# 本工具生成的草稿名前缀（这些是我们自己写的，不能用来当"剪映的真实写法"）
OUR_OWN_PREFIXES = ("人生副本_",)


def looks_like_ours(name):
    return any(name.startswith(p) for p in OUR_OWN_PREFIXES)


def inspect(draft_dir):
    j, f = load_draft(draft_dir)
    if not j:
        print(f"  跳过（没有草稿文件）: {draft_dir}")
        return False

    masks = {m["id"]: m for m in j.get("materials", {}).get("masks", [])}
    if not masks:
        return False

    name = os.path.basename(draft_dir)
    ours = looks_like_ours(name)

    print("=" * 70)
    if ours:
        print("⚠️  警告：这个草稿是本工具自己生成的，不能作为剪映语法的依据！")
        print("    请在剪映里【手动】做一个蒙版关键帧，再指定那个草稿名运行本脚本。")
        print("-" * 70)
    print(f"草稿: {name}")
    print(f"文件: {os.path.basename(f)}")
    print(f"版本: version={j.get('version')} new_version={j.get('new_version')} "
          f"app={j.get('platform', {}).get('app_version')}")
    print(f"帧率: {j.get('fps')}")
    print("=" * 70)

    print(f"\n【蒙版素材】共 {len(masks)} 个")
    for m in list(masks.values())[:3]:
        print(json.dumps(m, ensure_ascii=False, indent=2)[:800])

    print("\n【片段上与蒙版有关的字段】")
    hit = False
    for t in j.get("tracks", []):
        for s in t.get("segments", []):
            refs = s.get("extra_material_refs", [])
            linked = [r for r in refs if r in masks]
            # 新版剪映可能把蒙版放在 common_masks 里
            common_masks = s.get("common_masks")
            kfs = s.get("common_keyframes", [])
            mask_kfs = [k for k in kfs if "Mask" in k.get("property_type", "")]

            if not (linked or common_masks or mask_kfs):
                continue

            hit = True
            print(f"\n--- 轨道 {t.get('name') or t.get('type')} ---")
            print(f"  extra_material_refs 里关联的蒙版: {linked}")
            print(f"  是否有 common_masks 字段: {'是' if common_masks else '否'}")
            if common_masks:
                print("  common_masks 内容:")
                print(json.dumps(common_masks, ensure_ascii=False, indent=2)[:1500])

            print(f"  common_keyframes 里的属性: "
                  f"{[k.get('property_type') for k in kfs]}")

            if mask_kfs:
                print("\n  ★★★ 找到蒙版关键帧！这就是剪映的真实写法 ★★★")
                for k in mask_kfs:
                    print(json.dumps(k, ensure_ascii=False, indent=2)[:1200])
            else:
                print("  （这个片段的 common_keyframes 里没有 Mask 类关键帧）")

            # 关键帧也可能挂在别处
            for key in ("keyframe_refs", "keyframes"):
                v = s.get(key)
                if v:
                    print(f"  片段.{key} = {json.dumps(v, ensure_ascii=False)[:400]}")

    # 顶层 keyframes 容器
    top = j.get("keyframes")
    if isinstance(top, dict) and any(top.values()):
        print("\n【顶层 keyframes 容器（非空）】")
        print(json.dumps(top, ensure_ascii=False, indent=2)[:1500])

    return hit


def main():
    root = find_drafts_root()
    print(f"草稿目录: {root}\n")
    if not os.path.exists(root):
        print("❌ 草稿目录不存在，请先在剪映里建一个草稿")
        return

    if len(sys.argv) > 1:
        targets = [os.path.join(root, sys.argv[1])]
    else:
        dirs = [os.path.join(root, d) for d in os.listdir(root)
                if os.path.isdir(os.path.join(root, d))]
        dirs.sort(key=os.path.getmtime, reverse=True)
        # 🔥 排除本工具自己生成的草稿，否则只会读到我们自己写进去的值，
        #    形成"自己验证自己"的循环，得不到剪映的真实写法。
        external = [d for d in dirs if not looks_like_ours(os.path.basename(d))]
        targets = external[:10]
        print("未指定草稿名，扫描最近修改的草稿（已排除本工具生成的）...\n")
        if not targets:
            print("❌ 除了本工具生成的草稿外，没有找到其它草稿。")
            print("   请在剪映里手动新建一个草稿，加蒙版+位置关键帧，保存后重跑。")
            print(f"   （已跳过 {len(dirs)} 个本工具生成的草稿）\n")

    found = False
    for d in targets:
        try:
            if inspect(d):
                found = True
                print("\n" + "=" * 70)
                print("👆 把上面这段发给我，我照着剪映的真实结构改代码")
                print("=" * 70)
                break
        except Exception as e:
            print(f"  读取失败 {os.path.basename(d)}: {e}")

    if not found:
        print("\n没有找到带蒙版关键帧的【剪映手工】草稿。")
        print("请按文件开头的说明操作：剪映里手动做一个蒙版位置关键帧，保存后重跑。")
        print("提示：可以直接指定草稿名，例如  python 检查蒙版关键帧语法.py 我的测试草稿")


if __name__ == "__main__":
    main()
