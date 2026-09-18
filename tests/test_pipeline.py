# ruff: noqa: E402
"""人生副本流水线单元测试。

运行：
    python -m unittest discover -s tests -v
    （或 python -m pytest tests/ -q）

不依赖网络：TTS 走 estimate 模式，素材用临时生成的 wav。
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
import wave

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL_SCRIPTS = os.path.join(REPO_ROOT, "jianying-editor-skill-main", "scripts")
for _path in (REPO_ROOT, SKILL_SCRIPTS, os.path.join(SKILL_SCRIPTS, "vendor")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from pipeline import script_io, text as textmod  # noqa: E402
from pipeline.config import DEFAULT_CONFIG, apply_preset, load_config, parse_simple_yaml  # noqa: E402


def make_wav(path, seconds=2.0, rate=24000):
    with wave.open(path, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        for i in range(int(rate * seconds)):
            handle.writeframes(b"\x00\x20" if (i // 200) % 2 else b"\x00\xe0")
    return path


class TestTextUtils(unittest.TestCase):
    def test_plain_strips_punct(self):
        self.assertEqual(textmod.plain("军训，你顺拐了。"), "军训你顺拐了")

    def test_char_weight_half_for_ascii(self):
        self.assertLess(textmod.char_weight("1/217"), textmod.char_weight("专业排名第一"))

    def test_split_text_for_tts_short_passthrough(self):
        self.assertEqual(textmod.split_text_for_tts("短句。", 300), ["短句。"])

    def test_split_text_for_tts_prefers_sentence_boundary(self):
        long_text = "第一句话。" * 80  # 400 字
        chunks = textmod.split_text_for_tts(long_text, 300)
        self.assertGreaterEqual(len(chunks), 2)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 300)
        # 断点应落在句末标点后，而不是把句子拦腰截断
        self.assertTrue(all(c.endswith("。") for c in chunks[:-1]))

    def test_split_into_subtitles_keeps_short_sentences(self):
        subs = textmod.split_into_subtitles("军训，你顺拐了四十七次。教官把全系三百人叫停。")
        # "军训，" / "你顺拐了四十七次。" / "教官把全系三百人叫停。"
        self.assertEqual(subs, ["军训，", "你顺拐了四十七次。", "教官把全系三百人叫停。"])

    def test_split_into_subtitles_hard_splits_long_line(self):
        long_line = "这是一个没有任何标点的超长句子" * 5
        subs = textmod.split_into_subtitles(long_line, max_chars_per_line=22)
        self.assertGreater(len(subs), 1)
        for sub in subs:
            self.assertLessEqual(len(textmod.plain(sub)), 22)

    def test_alloc_is_lossless(self):
        weights = [3, 5, 7, 11]
        parts = textmod.alloc(1_000_000, weights)
        self.assertEqual(sum(parts), 1_000_000)
        self.assertEqual(len(parts), 4)

    def test_alloc_proportional(self):
        parts = textmod.alloc(1_000_000, [1, 1, 1, 1])
        self.assertTrue(all(abs(p - 250_000) < 2 for p in parts))

    def test_clean_for_tts(self):
        self.assertEqual(textmod.clean_for_tts("你 好  "), "你好。")
        self.assertEqual(textmod.clean_for_tts("真的吗？？？"), "真的吗？")
        self.assertEqual(textmod.clean_for_tts("结束。。"), "结束。")

    def test_estimate_duration_positive(self):
        self.assertGreater(textmod.estimate_duration_us("军训，你顺拐了四十七次。"), 0)

    def test_build_groups_respects_budget(self):
        scenes = [{"seq": i, "chars": 40} for i in range(10)]
        groups = textmod.build_groups(scenes, group_chars=100)
        self.assertTrue(all(sum(s["chars"] for s in g) <= 120 for g in groups))
        self.assertEqual(sum(len(g) for g in groups), 10)


class TestShotlist(unittest.TestCase):
    RAW = "\n".join(
        [
            "今天体验的人生副本是",
            "18岁辍学的机修工",
            "赶上油车大退潮翻盘的一生",
            "那年你中考落榜",
            "你舅舅蹲在院子里",
            "抽着烟对你爸说",
            "别供他复读了",
            "这孩子我一眼看到底",
            "念书念不进去",
            "将来也就修修车补补轮胎的命",
            "这话你听见了",
            "你没顶嘴",
        ]
    )

    def test_build_shotlist_extracts_lead(self):
        lead, shots = textmod.build_shotlist(self.RAW, lead_lines=3)
        self.assertIn("人生副本", lead)
        self.assertTrue(shots)
        self.assertEqual(shots[0]["seq"], 1)
        self.assertNotIn("今天体验的人生副本是", "".join(s["text"] for s in shots))

    def test_shotlist_roundtrip_parses_back(self):
        lead, shots = textmod.build_shotlist(self.RAW, target_chars=20, min_chars=4)
        mapping_text = textmod.shotlist_to_mapping(shots)
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as handle:
            handle.write(mapping_text)
            path = handle.name
        try:
            parsed = script_io.parse_mapping_file(path)
            self.assertEqual(len(parsed), len(shots))
            self.assertEqual(parsed[0][2], "shot_01.jpg")
            self.assertEqual(parsed[0][1], shots[0]["text"])
        finally:
            os.unlink(path)

    def test_image_prompts_one_per_shot(self):
        _, shots = textmod.build_shotlist(self.RAW, target_chars=20, min_chars=4)
        prompts = textmod.shotlist_to_image_prompts(shots, style="写实")
        rows = [ln for ln in prompts.splitlines() if ln and not ln.startswith("#")]
        self.assertEqual(len(rows), len(shots))
        self.assertTrue(all("写实" in row for row in rows))

    def test_punctuate_marks_quote_lead_with_colon(self):
        out = textmod.punctuate_lines(["抽着烟对你爸说", "别供他复读了", "你没顶嘴"])
        self.assertEqual(out, ["抽着烟对你爸说：", "别供他复读了。", "你没顶嘴，"])

    def test_punctuate_keeps_existing_punctuation(self):
        self.assertEqual(textmod.punctuate_lines(["已经结束了。"]), ["已经结束了。"])

    def test_close_sentence_strips_trailing_comma(self):
        self.assertEqual(textmod.close_sentence("一缠接着干，"), "一缠接着干。")
        self.assertEqual(textmod.close_sentence("结束了。"), "结束了。")
        self.assertEqual(textmod.close_sentence("没标点"), "没标点。")

    def test_no_shot_ends_with_comma(self):
        _, shots = textmod.build_shotlist(self.RAW, target_chars=20, min_chars=4, max_chars=40)
        for shot in shots:
            self.assertNotIn(shot["text"][-1], "，、；：", f"镜头挂在逗号上: {shot['text']}")

    def test_looks_unpunctuated(self):
        self.assertTrue(textmod.looks_unpunctuated(self.RAW))
        self.assertFalse(textmod.looks_unpunctuated("第一句。\n第二句。\n第三句。\n"))


class TestScriptIO(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.img_dir = os.path.join(self.tmp, "image")
        os.makedirs(self.img_dir)
        for i in range(1, 4):
            open(os.path.join(self.img_dir, f"shot_{i:02d}.jpg"), "wb").write(b"\xff\xd8\xff")
        self.mapping = os.path.join(self.tmp, "对照表.txt")
        with open(self.mapping, "w", encoding="utf-8") as handle:
            handle.write(
                "镜头字幕对照表（共3镜）\n\n格式：镜头编号 | 图片文件名 | 字幕内容\n\n"
                "01 | shot_01.jpg | 军训，你顺拐了四十七次。\n"
                "02 | shot_02.jpg | 教官把全系三百人叫停。\n"
                "03 | shot_99.jpg | 这一镜的图片不存在。\n"
            )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_parse_bar_format(self):
        mappings = script_io.parse_mapping_file(self.mapping)
        self.assertEqual(len(mappings), 3)
        self.assertEqual(mappings[0], (1, "军训，你顺拐了四十七次。", "shot_01.jpg"))

    def test_find_image_swaps_extension(self):
        self.assertIsNotNone(script_io.find_image_file(self.img_dir, "shot_01.png"))
        self.assertIsNone(script_io.find_image_file(self.img_dir, "shot_42.png"))

    def test_preflight_flags_missing_image(self):
        result = script_io.preflight(
            script_io.parse_mapping_file(self.mapping), self.img_dir, DEFAULT_CONFIG["check"]
        )
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("shot_99.jpg", result["errors"][0])
        self.assertEqual(result["stats"]["shots"], 3)

    def test_preflight_flags_banned_word(self):
        cfg = dict(DEFAULT_CONFIG["check"], banned_words=["四十七"])
        result = script_io.preflight(
            script_io.parse_mapping_file(self.mapping), self.img_dir, cfg
        )
        self.assertTrue(any("敏感词" in e for e in result["errors"]))


class TestConfig(unittest.TestCase):
    def test_parse_yaml_subset(self):
        data = parse_simple_yaml(
            "tts:\n"
            "  speaker: ICL_zh_male_momodianying\n"
            "  speed: 1.05\n"
            "  cache: true\n"
            "  audio_effect: null\n"
            "body:\n"
            "  first_shot_effects:\n"
            "    - 幻彩故障\n"
            "    - 震动屏闪\n"
        )
        self.assertEqual(data["tts"]["speaker"], "ICL_zh_male_momodianying")
        self.assertEqual(data["tts"]["speed"], 1.05)
        self.assertIs(data["tts"]["cache"], True)
        self.assertIsNone(data["tts"]["audio_effect"])
        self.assertEqual(data["body"]["first_shot_effects"], ["幻彩故障", "震动屏闪"])

    def test_repo_config_loads_and_overrides(self):
        cfg = load_config(os.path.join(REPO_ROOT, "config.yaml"))
        self.assertEqual(cfg["tts"]["speaker"], "ICL_zh_male_momodianying")
        # 默认值里没被 yaml 覆盖的项仍在
        self.assertEqual(cfg["subtitle"]["font"], "优设标题黑")
        self.assertTrue(os.path.exists(cfg["input"]["mapping_file"]))

    def test_preset_grid(self):
        cfg = load_config(os.path.join(REPO_ROOT, "config.yaml"))
        cfg = apply_preset(cfg, "grid")
        self.assertEqual(cfg["opening"]["style"], "grid")
        self.assertEqual(cfg["opening"]["intro_animation"], "九宫格")

    def test_preset_flash9(self):
        cfg = apply_preset(load_config(os.path.join(REPO_ROOT, "config.yaml")), "flash9")
        self.assertEqual(cfg["opening"]["flash_count"], 9)
        self.assertEqual(cfg["opening"]["repeat_first"], 8)

    def test_preset_not_overridden_by_second_apply(self):
        """回归：mask_flash_fixed 不能被 style 反查覆盖回 mask_flash。"""
        cfg = apply_preset(load_config(os.path.join(REPO_ROOT, "config.yaml")), "mask_flash_fixed")
        self.assertFalse(cfg["opening"]["measure_lead_tts"])
        cfg = apply_preset(cfg, None)  # build() 内部会再调一次
        self.assertFalse(cfg["opening"]["measure_lead_tts"])
        self.assertEqual(cfg["opening"]["lead_fallback_us"], 1800000)


class TestTTSSpeedMath(unittest.TestCase):
    """语速换算必须自洽：时间轴占用 == source / speed。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.wav = make_wav(os.path.join(cls.tmp, "voice.wav"), seconds=3.0)
        os.environ["JY_PROJECTS_ROOT"] = os.path.join(cls.tmp, "drafts")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _project(self, name):
        from jy_wrapper import JyProject

        return JyProject(name, drafts_root=os.path.join(self.tmp, "drafts"), overwrite=True)

    def test_speed_105_shrinks_target(self):
        from pipeline.tts import TTSEngine

        cfg = dict(DEFAULT_CONFIG["tts"], backend="estimate", speed=1.05)
        engine = TTSEngine(cfg, log=lambda *_: None)
        project = self._project("SpeedCheck")
        segment = project.add_media_safe(self.wav, 0, track_name="VoiceOver")
        source = segment.source_timerange.duration
        _, duration = engine._postprocess(segment)

        self.assertAlmostEqual(segment.speed.speed, 1.05)
        self.assertEqual(duration, int(round(source / 1.05)))
        self.assertEqual(segment.target_timerange.duration, duration)
        # 剪映实际占用时间轴的长度 = source / speed，必须和 target 一致
        self.assertLess(
            abs(int(round(source / segment.speed.speed)) - segment.target_timerange.duration), 2000
        )

    def test_estimate_backend_needs_no_network(self):
        from pipeline.tts import TTSEngine

        cfg = dict(
            DEFAULT_CONFIG["tts"], backend="estimate", speed=1.0, _standin_audio=self.wav
        )
        engine = TTSEngine(cfg, log=lambda *_: None)
        project = self._project("EstimateCheck")
        segment, duration = engine.synthesize(project, "军训，你顺拐了四十七次。", 0)
        self.assertIsNotNone(segment)
        self.assertGreater(duration, 0)
        self.assertEqual(engine.stats["estimate"], 1)


class TestEndToEnd(unittest.TestCase):
    """estimate 模式跑完整流水线，验证草稿结构自洽。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.img_dir = os.path.join(cls.tmp, "image")
        os.makedirs(cls.img_dir)
        make_wav(os.path.join(cls.tmp, "voice.wav"), seconds=2.0)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _make_image(self, name):
        # 最小合法 JPEG（1x1），pyJianYingDraft 只需要文件存在 + 能读尺寸
        path = os.path.join(self.img_dir, name)
        if not os.path.exists(path):
            shutil.copyfile(os.path.join(REPO_ROOT, "image", "shot_01.jpg"), path)
        return path

    def _run(self, style="mask_flash", shots=4):
        from pipeline.config import deep_merge
        from pipeline.pipeline import build

        mapping = os.path.join(self.tmp, f"对照表_{style}.txt")
        lines = ["镜头字幕对照表（共%d镜）" % shots, "", "格式：镜头编号 | 图片文件名 | 字幕内容", ""]
        for i in range(1, shots + 1):
            image = f"shot_{i:02d}.jpg"
            self._make_image(image)
            lines.append(f"{i:02d} | {image} | 这是第{i}镜的口播文案，用来验证时间轴是否对齐。")
        with open(mapping, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")

        cfg = load_config(os.path.join(REPO_ROOT, "config.yaml"))
        cfg = deep_merge(
            cfg,
            {
                "input": {"mapping_file": mapping, "image_dir": self.img_dir,
                          "audio_dir": self.tmp},
                "tts": {"backend": "estimate", "_standin_audio": os.path.join(self.tmp, "voice.wav"),
                        "cache": False},
                "project": {"drafts_root": os.path.join(self.tmp, f"drafts_{style}")},
                "run": {"interactive": False, "verbose": False,
                        "report_dir": os.path.join(self.tmp, "reports")},
                "opening": {"random_seed": 7},
            },
        )
        return build(cfg=cfg, preset=style, log=lambda *_: None)

    def test_build_mask_flash(self):
        result = self._run("mask_flash")
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["shots"], 4)
        self.assertEqual(result["failed"], [])
        self.assertGreater(result["total_us"], result["opening_us"])
        self.assertTrue(os.path.exists(result["draft_path"]))
        self._assert_timeline_contiguous(result)

    def test_build_flash9(self):
        result = self._run("flash9")
        self.assertEqual(result["status"], "SUCCESS")
        self.assertGreater(result["opening_us"], 0)
        self._assert_timeline_contiguous(result)

    def test_build_grid(self):
        result = self._run("grid")
        self.assertEqual(result["status"], "SUCCESS")
        self._assert_timeline_contiguous(result)

    def test_srt_is_monotonic_and_covers_body(self):
        result = self._run("mask_flash")
        with open(result["srt_path"], "r", encoding="utf-8") as handle:
            content = handle.read()
        stamps = [ln for ln in content.splitlines() if " --> " in ln]
        self.assertGreater(len(stamps), 4)
        starts = []
        for stamp in stamps:
            begin = stamp.split(" --> ")[0]
            starts.append(begin)
        self.assertEqual(starts, sorted(starts))

    def test_missing_image_is_skipped_not_fatal(self):
        from pipeline.pipeline import build

        mapping = os.path.join(self.tmp, "对照表_missing.txt")
        self._make_image("shot_01.jpg")
        with open(mapping, "w", encoding="utf-8") as handle:
            handle.write(
                "01 | shot_01.jpg | 这一镜有图，应该成功。\n"
                "02 | shot_77.jpg | 这一镜没有图，应该被跳过。\n"
            )
        cfg = load_config(os.path.join(REPO_ROOT, "config.yaml"))
        cfg["input"].update(
            mapping_file=mapping, image_dir=self.img_dir, audio_dir=self.tmp
        )
        cfg["tts"].update(
            backend="estimate", _standin_audio=os.path.join(self.tmp, "voice.wav"), cache=False
        )
        cfg["project"]["drafts_root"] = os.path.join(self.tmp, "drafts_missing")
        cfg["run"].update(interactive=False, verbose=False, report=False, srt=False)
        result = build(cfg=cfg, log=lambda *_: None)
        self.assertEqual(result["shots"], 1)
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(result["failed"][0]["reason"], "图片缺失")

    # ---------------- helpers ----------------
    def _assert_timeline_contiguous(self, result):
        """正片主轨 + 配音轨 + 字幕轨都必须首尾相接，不能有缝。"""
        draft_dir = result["draft_path"]
        content_path = os.path.join(draft_dir, "draft_info.json")
        if not os.path.exists(content_path):
            content_path = os.path.join(draft_dir, "draft_content.json")
        with open(content_path, "r", encoding="utf-8") as handle:
            content = json.load(handle)

        opening_us = result["opening_us"]
        total_us = result["total_us"]
        contiguous_tracks = 0
        for track in content["tracks"]:
            segments = sorted(
                track["segments"], key=lambda s: s["target_timerange"]["start"]
            )
            if len(segments) < 2:
                continue
            body_segments = [
                s for s in segments if s["target_timerange"]["start"] >= opening_us - 1
            ]
            if len(body_segments) < 2:
                continue
            contiguous_tracks += 1
            previous_end = None
            for segment in body_segments:
                start = segment["target_timerange"]["start"]
                duration = segment["target_timerange"]["duration"]
                if previous_end is not None:
                    self.assertEqual(
                        start,
                        previous_end,
                        f"{track['type']} 轨道在 {previous_end} → {start} 处有缝隙/重叠",
                    )
                previous_end = start + duration

        self.assertGreaterEqual(
            contiguous_tracks, 2, "至少主视频轨和字幕轨应被检查到（防止断言空转）"
        )

        # 配音轨必须正好覆盖 [片头结束, 成片结束]，否则音画就会越跑越偏
        voice_ends = []
        for track in content["tracks"]:
            if track["type"] != "audio":
                continue
            for segment in track["segments"]:
                if segment["target_timerange"]["start"] >= opening_us - 1:
                    voice_ends.append(
                        segment["target_timerange"]["start"]
                        + segment["target_timerange"]["duration"]
                    )
        self.assertTrue(voice_ends, "没有找到正片配音片段")
        self.assertEqual(max(voice_ends), total_us, "配音轨没有铺到成片结尾")


if __name__ == "__main__":
    unittest.main()


# ============================================================
# 以下为 MoneyPrinterPlus 借鉴部分的测试
# ============================================================
from pipeline import agentio  # noqa: E402


class TestYamlQuotedComment(unittest.TestCase):
    """回归：带引号的值后面跟注释，不能把注释吃进值里。"""

    def test_quoted_value_with_trailing_comment(self):
        data = parse_simple_yaml('a: ""          # 例：教育/语言教育\nb: "x # y"  # 注释\n')
        self.assertEqual(data["a"], "")
        self.assertEqual(data["b"], "x # y")

    def test_unbalanced_quote_raises(self):
        with self.assertRaises(ValueError):
            parse_simple_yaml('a: "没闭合\n')

    def test_repo_config_kuaishou_domain_clean(self):
        cfg = load_config(os.path.join(REPO_ROOT, "config.yaml"))
        self.assertEqual(cfg["publish"]["kuaishou_domain"], "")


class TestCandidatePool(unittest.TestCase):
    """混剪候选池（对应 MoneyPrinterPlus hunjian_service 的随机抽行）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.mapping = os.path.join(self.tmp, "mix.txt")
        with open(self.mapping, "w", encoding="utf-8") as handle:
            handle.write(
                "格式：镜头编号 | 图片文件名 | 字幕内容\n"
                "01 | a.jpg//b.jpg | 文案甲一//文案乙一\n"
                "02 | c.jpg | 只有一条\n"
                "03 | d.jpg//e.jpg//f.jpg | 一//二//三\n"
            )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_split_candidates(self):
        self.assertEqual(script_io.split_candidates("甲//乙//丙"), ["甲", "乙", "丙"])
        self.assertEqual(script_io.split_candidates("只有一条"), ["只有一条"])
        self.assertEqual(script_io.split_candidates("1/217"), ["1/217"])  # 单斜杠不拆

    def test_parse_entries_keeps_pools(self):
        entries = script_io.parse_mapping_entries(self.mapping)
        self.assertEqual(entries[0]["candidates_text"], ["文案甲一", "文案乙一"])
        self.assertEqual(entries[2]["candidates_image"], ["d.jpg", "e.jpg", "f.jpg"])
        self.assertEqual(entries[1]["candidates_text"], ["只有一条"])

    def test_variant_none_is_backwards_compatible(self):
        """没有候选池的老对照表，行为必须完全不变。"""
        legacy = script_io.parse_mapping_file(
            os.path.join(REPO_ROOT, "剧本", "口播文案_图片序号_对应表.txt")
        )
        self.assertEqual(len(legacy), 88)
        self.assertEqual(legacy[0], (1, "军训，你顺拐了四十七次。", "shot_01.jpg"))
        self.assertEqual(
            script_io.count_variants(
                os.path.join(REPO_ROOT, "剧本", "口播文案_图片序号_对应表.txt")
            ),
            1,
        )

    def test_sample_picks_reproducible(self):
        entries = script_io.parse_mapping_entries(self.mapping)
        pools = [e["candidates_text"] for e in entries]
        self.assertEqual(
            script_io.sample_picks(pools, 3, "seed"), script_io.sample_picks(pools, 3, "seed")
        )

    def test_sample_picks_spreads_over_space(self):
        """20 镜 × 2 候选有 2^40 组合，采样要能铺开，而不是只有 2 种结果。"""
        path = os.path.join(self.tmp, "wide.txt")
        with open(path, "w", encoding="utf-8") as handle:
            for i in range(1, 21):
                handle.write(f"{i:02d} | i{i}_a.jpg//i{i}_b.jpg | 甲{i:02d}//乙{i:02d}\n")
        entries = script_io.parse_mapping_entries(path)
        got, space = script_io.distinct_variants(entries, 10, seed="s1")
        self.assertEqual(space, 2 ** 40)
        self.assertEqual(len(got), 10)
        marks = {script_io.variant_fingerprint(t, i) for _, t, i in got}
        self.assertEqual(len(marks), 10, "变体之间必须互不重复")

    def test_distinct_variants_degrades_honestly(self):
        """组合空间不够时，如实返回能拿到的数量，不假装凑数。"""
        path = os.path.join(self.tmp, "tiny.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("01 | a.jpg//b.jpg | 甲//乙\n")
        entries = script_io.parse_mapping_entries(path)
        got, space = script_io.distinct_variants(entries, 50, seed="s")
        self.assertEqual(space, 4)
        self.assertLessEqual(len(got), 4)


class TestAgentHandoff(unittest.TestCase):
    """Arena agent 交接协议（替代 MoneyPrinterPlus 的 LLM API）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = load_config(os.path.join(REPO_ROOT, "config.yaml"))
        self.cfg["agent"]["root"] = os.path.join(self.tmp, ".agent")
        self.cfg["shotlist"] = dict(self.cfg["shotlist"])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _answer(self, shots=42, chars_per_shot=32, punct=True):
        mark = "。" if punct else ""
        body = "字" * max(chars_per_shot - 1, 1)
        return {
            "story_json": {
                "title": "测试标题",
                "lead_text": "今天你要体验的人生副本是测试",
                "shots": [
                    {"text": f"第{i}镜{body}{mark}", "image_prompt": f"画面{i}"}
                    for i in range(1, shots + 1)
                ],
            },
            "keywords": ["test keyword"],
        }

    def test_create_task_writes_prompts_and_constraints(self):
        task_id, path = agentio.create_task(self.cfg, "外卖员的一生", task_id="t1")
        self.assertEqual(task_id, "t1")
        with open(path, "r", encoding="utf-8") as handle:
            task = json.load(handle)
        self.assertEqual(task["schema"], agentio.SCHEMA)
        self.assertTrue(os.path.exists(os.path.dirname(task["answer_path"])))
        prompts = " ".join(t["prompt"] for t in task["tasks"])
        # 三个移植过来的提示词主题都在
        self.assertIn("外卖员的一生", prompts)
        self.assertIn("English keywords", prompts)
        self.assertEqual(task["constraints"]["story_min_chars"], 1200)

    def test_validate_catches_short_story(self):
        constraints = {"min_shots": 40, "max_shots": 90, "shot_min_chars": 12,
                       "shot_max_chars": 60, "story_min_chars": 1200, "story_max_chars": 2500}
        errors, shots, lead, title = agentio.validate_answer(self._answer(chars_per_shot=20), constraints)
        self.assertTrue(any("总净字数" in e for e in errors), errors)

    def test_validate_catches_missing_punctuation(self):
        constraints = {"min_shots": 1, "max_shots": 90, "shot_min_chars": 12,
                       "shot_max_chars": 60, "story_min_chars": 1, "story_max_chars": 99999}
        errors, *_ = agentio.validate_answer(self._answer(shots=2, punct=False), constraints)
        self.assertTrue(any("没有标点" in e for e in errors), errors)

    def test_validate_catches_too_few_shots(self):
        constraints = {"min_shots": 40, "max_shots": 90, "shot_min_chars": 12,
                       "shot_max_chars": 60, "story_min_chars": 1, "story_max_chars": 99999}
        errors, *_ = agentio.validate_answer(self._answer(shots=5), constraints)
        self.assertTrue(any("少于要求的 40 镜" in e for e in errors), errors)

    def test_validate_catches_missing_lead(self):
        constraints = {"min_shots": 1, "max_shots": 90, "shot_min_chars": 12,
                       "shot_max_chars": 60, "story_min_chars": 1, "story_max_chars": 99999}
        answer = self._answer(shots=2)
        answer["story_json"]["lead_text"] = ""
        errors, *_ = agentio.validate_answer(answer, constraints)
        self.assertTrue(any("lead_text" in e for e in errors), errors)

    def test_apply_answer_roundtrip(self):
        """出题 → 答题 → 收卷 → 产出的对照表能被解析器读回来。"""
        agentio.create_task(self.cfg, "测试主题", task_id="t2")
        done_dir = os.path.join(self.tmp, ".agent", "done")
        os.makedirs(done_dir, exist_ok=True)
        with open(os.path.join(done_dir, "t2.json"), "w", encoding="utf-8") as handle:
            json.dump(self._answer(shots=42, chars_per_shot=32), handle, ensure_ascii=False)

        out_dir = os.path.join(self.tmp, "剧本")
        result = agentio.apply_answer(self.cfg, "t2", out_dir=out_dir, log=lambda *_: None)
        self.assertTrue(result["ok"], result.get("errors"))
        self.assertEqual(len(result["shots"]), 42)
        self.assertEqual(result["title"], "测试标题")
        self.assertTrue(os.path.exists(result["mapping_path"]))
        self.assertTrue(os.path.exists(result["prompt_path"]))

        parsed = script_io.parse_mapping_file(result["mapping_path"])
        self.assertEqual(len(parsed), 42)
        self.assertEqual(parsed[0][2], "shot_01.jpg")
        # 标题/引导语写进了头部，发布清单要读它
        with open(result["mapping_path"], "r", encoding="utf-8") as handle:
            head = handle.read(300)
        self.assertIn("标题：测试标题", head)
        self.assertIn("引导语（片头文案）：", head)

    def test_apply_answer_rejects_bad_output(self):
        agentio.create_task(self.cfg, "测试主题", task_id="t3")
        done_dir = os.path.join(self.tmp, ".agent", "done")
        os.makedirs(done_dir, exist_ok=True)
        with open(os.path.join(done_dir, "t3.json"), "w", encoding="utf-8") as handle:
            json.dump(self._answer(shots=3, chars_per_shot=5), handle, ensure_ascii=False)
        result = agentio.apply_answer(self.cfg, "t3", log=lambda *_: None)
        self.assertFalse(result["ok"])
        self.assertTrue(result["errors"])

    def test_missing_answer_gives_actionable_error(self):
        agentio.create_task(self.cfg, "测试主题", task_id="t4")
        with self.assertRaises(FileNotFoundError) as ctx:
            agentio.apply_answer(self.cfg, "t4", log=lambda *_: None)
        message = str(ctx.exception)
        # 报错要指到具体缺哪个文件、该让谁去补
        self.assertIn("t4.json", message)
        self.assertIn("agent", message)


class TestPublishManifest(unittest.TestCase):
    """发布清单（对应 MoneyPrinterPlus 的 publisher 元数据，但不做自动发布）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_manifest_written_with_platform_rows(self):
        from pipeline.pipeline import _export_publish_manifest

        cfg = load_config(os.path.join(REPO_ROOT, "config.yaml"))
        cfg["run"]["report_dir"] = self.tmp
        mapping = os.path.join(self.tmp, "m.txt")
        with open(mapping, "w", encoding="utf-8") as handle:
            handle.write("标题：外卖员的一生\n引导语（片头文案）：今天你要体验的是\n\n")
        result = {
            "project_name": "测试片", "total_s": 299.7, "opening_us": 5500000,
            "shots": 47, "draft_path": "/tmp/x", "style": "mask_flash",
            "speaker": "ICL_zh_male_momodianying",
        }
        path = _export_publish_manifest(result, cfg, mapping)
        self.assertTrue(os.path.exists(path))
        content = open(path, encoding="utf-8").read()
        self.assertIn("外卖员的一生", content)
        for platform in ("douyin", "kuaishou", "xiaohongshu", "shipinhao"):
            self.assertIn(platform, content)
        self.assertIn("今天你要体验的是", content)
        self.assertIn("#人生副本", content)
