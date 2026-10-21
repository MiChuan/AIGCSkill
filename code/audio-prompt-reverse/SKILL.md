---
name: audio-prompt-reverse
version: 1.0.0
description: |
  音频逆向成提示词：把上传的一段音频（音乐 / 音效 / 环境声 / 人声）拆成可复核的测量数字
  （速度、调性、明暗、纹理、密度、动态、空间、低频占比），再把数字按固定阈值翻成标签，
  最后组装成带完整追溯表的生成提示词——每一句话都能指回某个读数和它的阈值。
  内置零依赖分析器（wav 原生解码，其余格式走 ffmpeg）、
  提示词组装器（音乐 / 音效 / 环境声 / 人声四种八槽结构，中英双语，
  Suno / Udio / Stable Audio / ElevenLabs 四个目标的成品文件）、
  往返校验器（把生成结果重新量一遍，跟原素材比出 0–100 的复述得分并指出该改哪个槽位）。
  Use when asked to 逆向音频、反推提示词、这段音乐怎么写 prompt、音频反推、
  根据音频生成提示词、这段音效怎么描述、音频逆向分析、reverse audio prompt、
  audio to prompt、reverse engineer audio prompt、describe this audio as a prompt、
  suno 提示词怎么写、音效提示词。
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
metadata:
  version: 1.0.0
  license: Apache-2.0
  triggers:
    - 音频逆向
    - 反推提示词
    - 音频反推
    - 这段音乐怎么写 prompt
    - 根据音频生成提示词
    - 音效怎么描述
    - 音频提示词
    - suno 提示词
    - audio to prompt
    - reverse audio prompt
    - audio prompt engineering
  requires:
    bins:
      - python    # >= 3.9
      - ffmpeg    # 可选：只用于非 wav 格式；缺失时脚本会明确报错并给出安装命令
    python_pkgs: []   # 三个脚本全部零第三方依赖
---

# 音频逆向成提示词

一句话：**不是「听一遍写个印象」，是把音频拆成可以复核的数字，再让每一句提示词都交代得出出处。**

通用做法的失败模式很稳定：听起来像什么就写什么，写出来顺，干活不动。
同一个音频两个人写出两个风格标签，谁都对，谁都没法验证，也没法改。
本 skill 把这件事拆成三段：**测量**、**翻译**、**验证**。
测量不许带形容词，翻译不许有没有依据的词，验证不许省。

---

## 四条不可让的约束

### R1 先测，再写

动笔之前必须有 `features.json`。**不许凭听感直接进第 3 槽（配器）或第 1 槽（类型）。**
自己听过不算是依据——你可以听完之后改机器的判断，但必须说清改的是哪一条、为什么。

```bash
python scripts/analyze_audio.py assets/target.wav --out out/ref
```

### R2 每个短语都要交代得出出处

`prompt.md` 的追溯表里，每一格都必须有「哪个字段、读数多少、阈值多少」。
**没有 trace 的短语要么是用户自己说的（trace 前缀是 `user:`），要么不许进输出。**

这条约束对应 `selftest.py` 里的一条断言：任何一个槽位出现空文本或空 trace 都会失败。

### R3 机器听不出来的，就明说听不出来

这些量永远是猜的，只能给候选，**不许替用户拍板**：

| 量 | 为什么给不出 |
|---|---|
| 乐器种类 | 同样是 1 kHz 的谐波，可能是钢琴也可能是吉他。只给候选，门槛 0.85 |
| 语种 / 人声性别 | `f0_hz` 与性别没有干净的对应关系，语种完全无从推测 |
| 具体声源行为 | 质心 2 kHz + 衰减 300 ms，既可能是抽屉关上，也可能是木头敲击 |
| 用途与场景 | 频谱里没有「用在短视频开头」这个信息 |

这些一律进「只能你答的问题」，或者显示【待填】。

### R4 必须做往返验证

**没有 `compare_features.py` 的分数，就不许说 prompt 写好了。**

```bash
python scripts/analyze_audio.py generated.wav --out out/new
python scripts/compare_features.py out/ref/features.json out/new/features.json --gate 80
```

≥ 80 交付；60–79 按报告改槽位再来一次；< 60 回去填待答问题，别硬调参数。

---

## 模式索引

| 模式 | 什么时候用 | 八槽结构 | 最稳的测量 | 最容易踩空的一槽 |
|---|---|---|---|---|
| **music** | 有明确节拍或 ≥ 3.5 秒的成曲 | 类型 / 情绪 / 配器 / 速度 / 音色 / 人声 / 结构 / 混音 | 速度、质心、频段占比 | 第 1 槽类型（常常多条候选打架） |
| **sfx** | < 3.5 秒且波峰因数高 | 声源动作 / 材质 / 包络 / 频谱 / 空间 / 时长 / 远近 / 用途 | 起振与衰减、亮度 | 第 1 槽声源（必须用户填） |
| **ambience** | 无节拍的长素材 | 空间 / 声源 / 远近 / 时间天气 / 质感 / 动静 / 循环 / 混音 | 尾音长度、起音密度 | 第 1 槽空间（必须用户填） |
| **voice** | 有声帧占比高且没有稳定节拍 | 音色 / 音区 / 表达 / 语速 / 情绪 / 录制 / 时长 / 用途 | 音区、音高抖动 | 第 1 槽音色（必须用户填） |

类型由 `guess_kind()` 自动判定，但**用户可以一句话推翻**（`--kind` 或对话里说「这是个音效」）。
自动判定只是省一轮问答，不是结论。

---

## 操作流程（每步都有硬门）

### Step 0 · 拿素材 —— 门：确认解码成功

```bash
python scripts/analyze_audio.py <音频> --out out/ref
```

- wav 走原生解析（PCM 8/16/24/32 位），零依赖。
- mp3 / m4a / flac / ogg 需要 ffmpeg。没装的话脚本**不会猜**，直接报退出码 2 并给出安装命令。
- **门**：必须看到 `features.json` 落盘，且 `meta.decoder` 与 `meta.seconds_analyzed` 正常。
  > 默认只分析 60 秒（取中段）。超过 3 分钟的素材建议先听一遍再决定用哪一段，别拿副歌骗 benchmark。

### Step 1 · 看读数，尤其看警告 —— 门：读完 `warnings`

输出末尾的「注意」不是在提醒你小心，是在告诉你**哪几个读数不可用**：

| 警告 | 后果 |
|---|---|
| 存在削波 | 第 5、8 槽的读数偏乐观，动态判断打七折 |
| 时长 < 3 秒 | 速度与调性不可靠，按音效路径走 |
| 6–8 kHz 几乎为空 | 亮度标签可能系统性偏低（有损压缩） |
| 双声道完全相同 | 宽度按单声道处理 |
| 调性置信偏低 | `key` 字段不能用，会写成 null |

### Step 2 · 问用户三个问题 —— 门：拿到答案再组装

不问也行，但输出里会【待填】。真正值得问的只有三类：

1. **这东西用在哪**（短视频 BGM？游戏 UI 音？）——决定第 7 / 8 槽；
2. **你听到了哪些乐器 / 声源**——决定第 3 槽，用户说的永远排在机器候选前面；
3. **有人声吗？什么语言？**——决定第 6 槽和整份负向词。

答案写进 `specs/*.json`，带 `user:` 前缀进追溯表。

### Step 3 · 组装提示词 —— 门：追溯表每一行都有依据

```bash
python scripts/build_prompt.py out/ref/features.json --spec specs/my.json --outdir out/prompt
```

产物：`prompt.md`（主交付：追溯表 + 长/短提示词 + 负向 + 参数 + 把握度 + 待答问题）、
`prompt.json`、`negative.txt`、`targets/{suno,udio,stable-audio}.txt`、`targets/elevenlabs-sfx.json`。

**门**：

- 追溯表里没有空的「依据」格；
- 「把握度」里任一项 < 0.5 的，必须标记为待确认，不能当既成事实读进 prompt；
- 待答问题必须原样转达给用户，不许自己替他答。

### Step 4 · 生成 —— 门：一次只改一处

用 `targets/` 下对应目标的那份文件去生成。**每次只改一个槽位**，
改两处以上就没法判断是哪处起了作用，第 5 步也白做。

### Step 5 · 往返验证 —— 门：≥ 80 才许交付

```bash
python scripts/compare_features.py out/ref/features.json out/new/features.json --gate 80
```

分数低于 60 的时候，先分清是「整体都偏」还是「关键维度有硬伤」：
`tempo_bpm`、`centroid_hz`、`voiced_ratio` 这类关键维度得分 < 0.5 时，
总分会被直接压到 59，不管其余维度多好看——这时候改细枝末节没用，得动第 4 / 5 / 6 槽。

**改三轮、分数涨不满 5 分就停手**，回去 Step 2 重新问用户。「再调一遍肯定能成」是最常见的
失败模式，人会一直调到第八轮。

---

## 命令速查

```bash
# 分析
python scripts/analyze_audio.py track.mp3                       # 摘要到 stdout
python scripts/analyze_audio.py track.wav --out out/ref --json  # 落盘 + JSON
python scripts/analyze_audio.py long.wav --max-seconds 90 --window start

# 组装
python scripts/build_prompt.py out/ref/features.json --outdir out/prompt
python scripts/build_prompt.py out/ref/features.json --kind sfx --spec specs/demo-sfx.json --outdir out/sfx
python scripts/build_prompt.py out/ref/features.json --print    # 不落盘

# 校验
python scripts/compare_features.py out/ref/features.json out/new/features.json
python scripts/compare_features.py out/ref/features.json out/new/features.json --out out/cmp --gate 80

# 自测（改完脚本先跑这个，94 项断言，不需要外部素材）
python scripts/selftest.py
python scripts/selftest.py --emit out/probes -v                 # 把探针 wav 留下来听
python tests/test_scripts.py                                    # CLI 层回归，30 项
```

退出码约定：`analyze_audio` 0 正常 / 2 输入或解码问题 / 3 近乎静音；
`build_prompt` 0 正常 / 2 输入问题；`compare_features` 配 `--gate` 时未达标返回 1。

---

## 判据在哪

| 想知道 | 看哪篇 |
|---|---|
| 每个阈值为什么是这个数、什么时候失效 | `references/feature-criteria.md` |
| 四个目标的字段限制、八槽顺序、负向词怎么触发 | `references/prompt-grammar.md` |
| 分数怎么读、哪一维出问题改哪个槽位 | `references/roundtrip.md` |
| 数值到措辞的映射表（改用词改这里，不要改脚本） | `data/lexicon.json` |

## 验收清单

- [ ] `features.json` 里的 `warnings` 全部读过，且每个被降级的读数都对应用户可见的说明
- [ ] `prompt.md` 追溯表没有空依据格
- [ ] 用户填过的字段都带 `user:` 前缀，机器推断的都标了候选与命中率
- [ ] 把握度里 < 0.5 的维度没有被当成结论使用
- [ ] 负向词 ≤ 8 条
- [ ] `compare_features.py` 分数 ≥ 80，或明确记录了「< 80 是因为 XXX」并已告知用户
