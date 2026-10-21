# code — 逆向工具类 Skill

本目录收录**逆向**方向的 skill：给定成品，反推出能复刻它的提示词。每个子目录是一个独立可用的 skill（含 `SKILL.md`）。

| Skill | 从什么反推 | 量化什么 | 产物 | 依赖 | 自测 |
| --- | --- | --- | --- | --- | --- |
| [image-prompt-reverse](image-prompt-reverse/SKILL.md) | 一张图 | 光位 / 光比 / 色板 hex / 景深 / 噪声 / 暗角 / 主体候选位置 / 水印嫌疑 | `probe.json/md/png`、四档提示词（short / natural / mj / sd）、verify 评分报告 | Pillow | 50 项 |
| [audio-prompt-reverse](audio-prompt-reverse/SKILL.md) | 一段音频 | 速度 / 调性 / 明暗 / 纹理 / 密度 / 动态 / 空间 / 低频占比 | `features.json`、八槽追溯表式 `prompt.md`、`targets/{suno,udio,stable-audio,elevenlabs-sfx}`、复述得分报告 | 纯标准库（非 wav 走 ffmpeg） | 94 项（selftest）+ 30 项（CLI 回归） |
| [text-style-to-prompt](text-style-to-prompt/SKILL.md) | 一段文本 | 七轴指纹：句长 / 段构 / 标点 / 人称 / 用词 / 引语 / 版面 | `ref.json`、三档提示词（short / standard / anchored）、`cmp.json` + pass / fail | 纯标准库 | 25 组用例 / 55 项断言 |

## 三者为什么放在一起

它们不是「三种生成」，是**同一个动作的三种输入**：生成类 skill 从提示词出成品，这三个从成品回提示词。所以三者共用一条纪律，也是这类任务唯一能防住糊弄的做法：

```
先量化  →  再翻译  →  必须校验打分
```

1. **先量化**：脚本把「看着像 / 听着像 / 读着像」换成可复现的数字。这一步不许跳——人眼给的第一印象（「暖调」「有节奏感」「文笔细腻」）恰恰是最容易错、且无法复核的部分。
2. **再翻译**：数字按固定阈值翻成标签或规格。**阈值是公开的**，所以每一句提示词都能指回「哪个读数、过了哪条阈值」。`audio-prompt-reverse` 把它做成一张八槽追溯表，一格一出处。
3. **必须校验**:拿产出的提示词重新生成一遍，量回来跟原素材比，出分数。图片 ≥ 80、音频 ≥ 80、文本总分 ≥ 75 且每轴 ≥ 55——**没过线不许交付**，按扣分项改对应字段再来。

反面是空泛词。三个 skill 都有拦截：`image-prompt-reverse` 遇到「好看 / 电影感 / 高级感」报 W201；`text-style-to-prompt` 遇到「文笔细腻 / 富有张力 / 行云流水」且不带数字也不带对照时，`build_prompt.py` 直接退出码 2；`audio-prompt-reverse` 的 R2 要求追溯表每格都要有「字段 / 读数 / 阈值」，填不出来就是 R3——明说「机器听不出来」，不要编。

还有一条三者都守的：**原素材是 ground truth**。复刻分数低就改 spec，不许改源文本凑分；文本类还要求复刻时**换主题**，同主题等于背诵测试，分数会假高。

## 怎么用

**方式一：装进 WorkBuddy 技能目录**

```bash
# Windows (Git Bash)
cp -r image-prompt-reverse ~/.workbuddy/skills/
cp -r audio-prompt-reverse ~/.workbuddy/skills/
cp -r text-style-to-prompt ~/.workbuddy/skills/
```

装好后说「反推这张图的提示词」「这张图怎么生成的」「看图写 prompt」「复刻这张图」「这段音乐怎么写 prompt」「Suno 提示词」「照着这风格写」「提炼写作风格」就会命中。

**方式二：命令行**

```bash
# 图 → 提示词
cd image-prompt-reverse
python scripts/make_demo.py --out assets/demo-ref.png --seed 7     # 没素材时合成一张
python scripts/probe.py assets/demo-ref.png --outdir out/probe     # 量测
python scripts/build_prompt.py --spec specs/dune-silhouette.json \
    --outdir out/dune --probe out/probe/probe.json                 # 装配四档
python scripts/verify.py --ref assets/demo-ref.png \
    --gen out/dune/raw/gen.png --outdir out/dune/verify            # 重出校验
python tests/test_scripts.py                                       # 50 项

# 音频 → 提示词
cd ../audio-prompt-reverse
python scripts/analyze_audio.py track.mp3                          # 摘要到 stdout
python scripts/analyze_audio.py track.wav --out out/ref --json     # 落盘 + JSON
python scripts/build_prompt.py out/ref/features.json \
    --kind sfx --spec specs/demo-sfx.json --outdir out/sfx         # 追溯表 + 四目标成品
python scripts/compare_features.py out/ref/features.json out/new/features.json --gate 80
python scripts/selftest.py                                         # 94 项，不需要外部素材
python tests/test_scripts.py                                       # CLI 层回归，30 项

# 文本 → 风格提示词
cd ../text-style-to-prompt
python scripts/style_probe.py --text examples/sample-input.md --out out/ref.json --summary
python scripts/build_prompt.py --spec specs/demo-cold-tech.json --check          # 空话直接退出 2
python scripts/build_prompt.py --spec specs/demo-cold-tech.json --outdir out/demo
python scripts/style_probe.py --text examples/sample-repro.md --out out/cand.json
python scripts/style_compare.py --ref out/ref.json --cand out/cand.json --min 75 --axis-min 55
python tests/test_scripts.py                                       # 25 组用例
```

退出码约定（三个 skill 一致）：0 正常 / 1 未过线 / 2 输入有问题；`analyze_audio.py` 额外用 3 表示近乎静音。

## 已知局限

- `image-prompt-reverse`：量测给出的是**客观项**（光位、色温、色板、景深），主体的语义判断（「这是一个人」还是「这是一尊像」）仍要人填进 spec，脚本不猜。
- `audio-prompt-reverse`：机器测不出「情绪」「叙事意图」，这些槽位标待答，由用户回答后填；硬编会污染追溯表。
- `text-style-to-prompt`：原文本 < 300 字时指纹偏噪到不可用，建议先扩样本；短样本（< 800 字）对比会出 warning，但不影响判定。

## 约定

```
<skill-name>/
├── SKILL.md      # frontmatter 必填 name / description / version；正文是流水线 + 硬门 + 验收清单
├── references/   # 判据库：每个阈值为什么是这个数、什么时候失效
├── scripts/      # 可独立运行的 CLI，零依赖或单一轻依赖（Pillow）
├── specs/        # 结构化输入（JSON），脚本的入口参数
├── data/         # 结构化知识表（可选，如 audio-prompt-reverse 的标签词典）
├── tests/        # 回归测试，python tests/test_scripts.py 直接跑
├── examples/     # 一个完整可复现的案例（含成品）
├── assets/       # 本地素材；大文件不入库，只留 README.txt
└── out/          # 产物，已在 .gitignore 忽略
```

新增同类 skill 照这个骨架铺：视频（看片 → 分镜提示词）、3D（模型 → 建模参数）、游戏（关卡 → 生成规则）都是「量化 → 翻译 → 校验打分」三个模块换一套量测脚本。
