# 人生副本 · 自动成片流水线

从「口播文案对照表 + 图片」一键生成剪映草稿：片头、配音、字幕、Ken Burns 动画、叠甲文字全部自动打好，
打开剪映就能预览导出。

> **这次重构做了什么**：原来仓库里有 4 份 `create_video_final*.py`（959 / 876 / 857 / 696 行，合计 3388 行）
> 互为拷贝，片头、配音、字幕逻辑各自漂移——修一处漏三处。现在收敛成一套
> [`pipeline/`](pipeline/) 引擎（2244 行，9 个模块）+ 一份 [`config.yaml`](config.yaml)，
> 4 个老脚本保留成 25 行的入口壳子，老命令照旧能跑。原实现归档在 [`legacy/`](legacy/)。

---

## 快速开始

```bash
pip install -r requirements.txt          # 实测只需 pymediainfo + websockets

# 1) 先体检：对照表能不能用、图齐不齐、时长合不合理
python -m pipeline.cli check

# 2) 出片（会写进剪映草稿目录）
python -m pipeline.cli build

# 3) 没网 / 不想烧配音额度时，先跑通流程
python -m pipeline.cli build --backend estimate
```

老命令仍然有效，等价于对应的 `--style`：

| 老脚本 | 等价命令 | 片头 |
| --- | --- | --- |
| `create_video_final.py` | `--style mask_flash` | 线性蒙版快闪，先测引导语配音真实时长 |
| `create_video_final 蒙版版.py` | `--style mask_flash_fixed` | 同上，但引导语固定 1.8 秒 |
| `create_video_final 快闪版开头.py` | `--style flash9` | 9+8 张交替推拉 + 两轮叠加 |
| `create_video_final 九宫格开头.py` | `--style grid` | 5 张九宫格入场 + 居中大标题 |

---

## 命令行

```bash
python -m pipeline.cli build \
    --style grid \                        # 片头预设
    --speaker ICL_zh_female_manbo_jianying \
    --speed 1.05 \
    --group-chars 120 \                   # 配音块字数（越小字幕越准，越大语气越连贯）
    --lead-text "今天你要体验的人生副本是……" \
    --mapping 剧本/对照表_xxx.txt \
    --batch "剧本/对照表_*.txt" \          # 批量：一表一片
    --yes                                 # 非交互（CI / 批量）

python -m pipeline.cli build --variants 5 --seed v1     # 混剪：一次出 5 条不重复
python -m pipeline.cli check                            # 素材预检
python -m pipeline.cli shotlist --raw 作品/55_.../正文.md  # 原稿 → 对照表

# 用 Arena agent 顶替 LLM API（不需要任何 api_key）
python -m pipeline.cli agent-task --topic 外卖员的一生   # 出题 → .agent/pending/
#   … 让 agent 读题、把结果写进 .agent/done/<id>.json …
python -m pipeline.cli agent-apply --id <id>            # 收卷 → 对照表 + 出图提示词
python -m pipeline.cli agent-list                       # 看待办/已答

python -m pipeline.cli presets                          # 列出片头预设
```

所有可调项都在 [`config.yaml`](config.yaml) 里（音色、语速、片头文案、字幕样式、
叠甲文字、预检阈值、BGM）。命令行参数优先级高于配置文件。

---

## 目录

```
pipeline/
  config.py     配置层：默认值 ← config.yaml ← 命令行；4 个片头预设
  text.py       文案：配音块切分、字幕短句、加权时长分配、ASR 原稿转对照表
  script_io.py  对照表解析 + 素材预检
  tts.py        配音引擎：重试 / 语速换算 / 磁盘缓存 / estimate 离线模式
  openings.py   4 种片头风格
  body.py       正片：字幕、图片、Ken Burns、首镜特效、叠甲、BGM
  pipeline.py   编排 + SRT / JSON 报告 / 发布清单导出
  agentio.py    Arena agent 交接协议（出题/收卷/校验），替代 LLM API
  cli.py        命令行入口
.agent/         agent 任务文件（pending 待办 / done 已答），不进版本库
config.yaml     唯一需要改的文件
tests/          53 个单元测试（不联网，estimate 模式跑完整流水线）
剧本/           口播文案对照表
image/          分镜图（shot_01.jpg …）
audio/          片头音效
legacy/         重构前的 4 份原始脚本（归档，勿改）
jianying-editor-skill-main/   剪映自动化 skill（vendored，JyProject / pyJianYingDraft）
```

---

## 这轮修掉的问题

### 1. 四份拷贝 → 一套引擎
`create_video_final.py` 里把 `tts_grouping.py` 的函数全部内联重写了一遍
（`_plain` / `_alloc` / `split_into_subtitles` / `build_groups`），而另外 3 个脚本
`import tts_grouping`。同一个「配音块」概念有两份实现，改一边不生效另一边。

### 2. 两个"写了但从来没生效"的特效
- `VideoSceneEffectType.荧光爆闪` 在 vendored pyJianYingDraft 里**不存在**（1097 个成员里没这个名），
  旧代码用 `except: pass` 把 `AttributeError` 吞了——快闪偶数张图的"白闪"一次都没加上。
  现在特效名走配置，`validate_opening_config()` 启动时报出来。
- `draft.Effects.audio_loudness_normalization`：`pyJianYingDraft` 根本没有 `Effects` 这个属性，
  那行响度归一化同样是死代码。现在用 `audio_effect: 人声增强` 这类真实存在的枚举名。

### 3. 字幕时间轴会飘
旧「蒙版版」先按 4.4 字/秒 + 标点停顿**估算**每句字幕时长，只在估算总长与真实配音
偏差 >10% 时才缩放，否则直接拿估算值当真实值用。偏差 9% 时就悄悄错位了。
现在字幕时长**总是**按加权字数归一化到该镜真实配音时长；
权重上 ASCII 字符按 0.55 计（`1/217` 不再被当成 5 个汉字念）。

### 4. `if ' ，。' in sentence or ','`
恒真条件（非空字符串永远为真），长句切分永远只走一个分支。已按净字数重写。

### 5. 帧长写死 60fps
`16667us` 当一帧，但草稿是 30fps（33333us/帧），"10 帧"实际只走了 5 帧。
现在统一 `1_000_000 / project.script.fps`。

### 6. 跑一半才发现缺图
缺图要等到主循环里才 `input()` 问你，此时配音已经烧掉几十次请求。
现在 `check` 一次性查出：缺图、空文案、编号重复、镜头过长/过短、
预估总时长越界、敏感词、同图重复使用。

### 7. 改一次字幕样式就要重跑全部配音
88 镜 = 十几次 SAMI 请求。现在按 `md5(文本|音色|语速)` 落盘缓存到 `.cache/tts/`，
命中直接复用，重跑只花渲染时间。

---

## 语速换算（容易改坏，这里记一笔）

`add_media_safe()` 建出来的音频片段 `source == target`、`speed=1.0`。
改成 1.05 倍速后，剪映占用时间轴的长度是 `source / speed`，
所以必须同步把 `target_timerange` 缩到 `source / speed`：

```
source 2_500_000us, speed 1.05  →  target 2_380_952us  ✓ 与 source/speed 一致
```

`pipeline/tts.py::_postprocess()` 集中处理这件事，并带一条断言：
两边差超过 2ms 直接抛 `TTSError`，不让音画错位悄悄混进草稿。

---

## 从 MoneyPrinterPlus 借了什么

我实际读了它的 `config/config.example.yml`、`services/llm/llm_service.py`、
`services/hunjian/hunjian_service.py`、`services/video/video_service.py`。
它的大部分体积在**多 provider 适配 + Selenium 自动发布**，这两块对你没用
（你不打算调 API；自动发布要在你本机跑真实浏览器）。真正值钱的是三件事，都搬过来了：

### 1. LLM 提示词 → 改成 agent 交接（`pipeline/agentio.py`）

它的 `services/llm/llm_service.py` 里有三个提示词模板：

```python
self.topic_template   = "请为以下主题扩展为详细的一篇文章,内容在{length}字以内…"
self.keyword_template = "Please analyze the following content in english, and then extract 1-5 short English keywords…"
self.sd_template      = "任务：将以下句子转换成Stable Diffusion图像生成模型能够理解的prompt…"
```

然后 `MyLLMService.generate_content()` 走 HTTP，11 个 `services/llm/*_service.py`
各要一个 api_key。**你要的不是这个**——你要的是那三段提示词。

所以这里把提示词落成文件，交给 Arena agent 填：

```bash
python -m pipeline.cli agent-task --topic 外卖员的一生
# → .agent/pending/<id>.json   含提示词 + 硬指标 + 输出格式要求
#   agent 读它，把结果写进 .agent/done/<id>.json
python -m pipeline.cli agent-apply --id <id>
# → 剧本/对照表_<标题>.txt + 剧本/出图提示词_<标题>.txt
```

提示词按《人生副本》规格改写过：第二人称、数字成链、爽点必须走可验证的公示通道、
合规红线，硬指标直接引 demo 库的「1200–2500 字 / 40–90 镜 / 单镜 12–60 字」。

**收卷时强制校验**（`agentio.validate_answer`），不合格直接退回，不会污染流水线：
镜数越界、单镜字数越界、总字数越界、**没有标点（切不出字幕）**、缺引导语。
实测：我第一版答案 1063 字被拦（要求 ≥1200），补到 1226 字才过。

好处：提示词进版本库可 diff 可回滚；产出是可复查的文本；断网能跑；
换 agent / 换模型不用改一行代码。

### 2. 混剪候选池 → 一次出 N 条不重复（`script_io.py`）

它的 `services/hunjian/hunjian_service.py` 靠 `random_line_from_text_file()`
每个场景随机抽一行，用组合数产出「100 条不重复」。

对照表现在支持候选池，用 `//` 分隔（不能用 `|`，那是字段分隔符；也不能用 `/`，
文案里有 `1/217`）：

```
01 | shot_01.jpg//shot_02.jpg//shot_03.jpg | 军训那天，你顺拐了四十七次。//九月三十八度，你一个人走了三百步。
02 | shot_04.jpg | 只有一条候选也完全没问题
```

```bash
python -m pipeline.cli build --variants 5 --seed v1    # 5 条不重复，草稿名自动带 _v00…_v04
```

没用纯随机，而是 `md5(seed:variant:镜头号)` 取模：**可复现**（同 seed 同 variant
永远同一组合），且每镜独立采样，所以 88 镜 × 2 候选能铺满 2^88 空间——
不像「按序号轮转」那样 2 候选只有 2 种结果。组合空间不够时如实报告，不假装凑数。
没有候选池的老对照表行为完全不变（组合数 = 1）。

### 3. 发布元数据 → 只出清单，不做自动发布（`_export_publish_manifest`）

它的 `publisher` 段有 `title_prefix` / `collection`，快手还有 `domain.level1/level2`，
由 Selenium 驱动真实浏览器上传。浏览器自动化搬不过来，但**元数据 schema 值得留**：
出片时顺手生成 `output/发布清单_<片名>.md`，含标题、话题标签、各平台要点、
引导语（可当简介首句）、自查清单。手动上传照抄即可。

### 没搬的

- **ffmpeg 混流那套**（`video_service.py` 的 `add_background_music` 用
  `amix` + `aloop`）——你出的是剪映草稿，BGM 在剪映里加更好，不重复造。
- **多 TTS provider 适配**（Azure/阿里/腾讯/chatTTS/GPTSoVITS）——剪映内置
  SAMI 免费且音色贴合，加了反而要管一堆 key。
- **Selenium 自动发布**——见上。

## 对接 `aavvcasdas/demo` 剧本库

demo 库（《剧本人生》拆书库）产出的是 `作品/NN_xxx/正文.md`——
**ASR 口播体：一行一个气口、整篇无标点**，不能直接当字幕用。

```bash
python -m pipeline.cli shotlist --raw 作品/55_机修工油转电翻盘/正文.md
# → 剧本/对照表_55_机修工油转电翻盘.txt   （镜头编号 | 图片名 | 字幕）
# → 剧本/出图提示词_55_机修工油转电翻盘.txt（每镜一条，带统一风格前缀）
```

转换时会：
1. 把开头 3 行识别成引导语（片头文案）；
2. 按行末语气词补标点——`说/道/喊/问` 收尾打「：」引出对白，`了/的/吧/啊` 收尾打「。」，其余打「，」；
3. 把句子打包成 40 字左右的镜头，尽量在句号处收口；
4. 每镜生成一条出图提示词，可直接喂 SD / comfyUI。

> ⚠️ 第 2 步是**规则猜的**，不是语言模型。命令会明确提示你出片前人工过一遍对照表。
> 想要更准的断句，用 demo 库里的 `story-short-write` skill 或任意 LLM 先过一道，
> 再喂给 `shotlist`（`looks_unpunctuated()` 会检测到已有标点并跳过提示）。

demo 库的短片规范是「单条 1200–2500 字 ≈ 1.5–3 分钟」。当前 `剧本/` 里的对照表
是 2806 净字数，按 4.4 字/秒估算约 **638 秒（10.6 分钟）**，`check` 会就此给出警告——
要么拆成上下集，要么把 `check.target_duration_s` 调成你实际的目标区间。

---

## 输出物

| 文件 | 内容 |
| --- | --- |
| 剪映草稿 | `<草稿目录>/人生副本_<时间戳>/` |
| `*.srt` | 字幕轨导出，用来核对字幕和配音对不对得上 |
| `output/run_report_*.json` | 每镜起止时间、时长、字幕条数、配音请求/缓存命中/失败次数、预检结果 |

---

## 测试

```bash
python -m unittest discover -s tests -v      # 或 pytest tests/ -q
```

53 个用例，**不联网**（配音走 estimate 模式，素材用临时生成的 wav）：
文案切分与时长分配、对照表解析与预检、原稿转对照表与断句、
语速换算自洽性、4 种片头预设、缺图跳过不中断、
端到端跑完整流水线后**校验时间轴首尾相接、配音轨正好铺到成片结尾**；
新增：混剪候选池的可复现性与变体互不重复、agent 出题/校验/收卷往返、
发布清单内容、YAML 带引号值 + 行尾注释的解析回归。

---

## 还没做（按性价比排序）

1. **词级时间戳对齐**。现在镜头切点是按字数比例估的，块内会有零点几秒偏差。
   剪映有「识别字幕」能给出词级时间戳，接上就能精确对齐——但 SAMI 接口
   是否返回 `tts_subtitle` 我在沙箱里验证不了（`sami.bytedance.com` 连接被重置），
   需要在能访问该域名的机器上抓一次响应确认。
2. **自动发布**。MoneyPrinterPlus 那套 Selenium 发布依赖真实浏览器和已登录会话，
   只能在沙箱外跑。现在只做到「生成发布清单」这一步。真要自动化，
   建议在 `pipeline/` 下加 `publisher.py`，读同一份 `publish:` 配置。
3. **AI 生图接线**。`shotlist` 已经产出每镜提示词，但没接 SD/comfyUI 后端；
   要接的话建议在 `pipeline/` 下加一个 `imagegen.py`，按 `image/shot_NN.jpg` 落盘即可。
4. **Web UI**。现在是 CLI + YAML；要 Streamlit 界面的话，`pipeline.build(cfg=...)`
   已经是纯函数式入口，包一层就行。

---

## 依赖

实测最小可运行集只有两个包（在只装了这两个的干净 venv 里跑通了完整流水线）：

```
pymediainfo>=6.0     # pyJianYingDraft 读媒体时长
websockets>=12.0     # 剪映内置配音（SAMI 流式接口）
```

可选：`requests`（云曲库/云素材）、`edge-tts`（`--backend edge`）。
详见 [`requirements.txt`](requirements.txt)。
