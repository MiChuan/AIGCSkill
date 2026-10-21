---
name: text-style-to-prompt
version: 1.0.0
description: |
  给一段文本，逆向出能复刻该文本风格的提示词。不靠读后感——靠把文本压成
  可比较的七轴指纹（句长、段构、标点、人称、用词、引语、版面），再翻成
  逐条带证据和验收判据的风格规格，编译成 short / standard / anchored 三档提示词。
  写完之后强制复刻验收：用提示词重新写一段同风格不同主题的文本，
  和原文本比指纹，过线才交付。
  不是写文案，是把「这段读起来像 X」这件事变成「能传给另一个写作者的规格」。
  Use when asked to 逆向提示词 / 反推提示词 / 提取文本风格 / 风格分析 /
  照着这风格写 / 提炼写作风格 / reverse-engineer prompt from text /
  extract style from writing / mimic this writing style.
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
metadata:
  version: 1.0.0
  license: MIT
  triggers:
    - 逆向提示词
    - 反推提示词
    - 提取文本风格
    - 风格分析
    - 照着这风格写
    - 提炼写作风格
    - 仿照风格
    - 复刻风格
    - reverse-engineer prompt
    - extract style
    - mimic writing style
  requires:
    bins:
      - python    # >= 3.9，scripts/ 下三个脚本需要
    python_pkgs: []   # 纯标准库
---

# 文本风格逆向：从一段文字反推能复刻它的提示词

一句话：**读后感不是规格**。「文笔细腻」交给模型没用，「单句 8–22 字、短句率不低于 25%」才有用。

这个 skill 做的事是：把一段文本压成可比较的七轴指纹 → 把指纹翻成带证据和验收判据的风格规格 → 编译成三档提示词 → 写完用同主题不同的新文本复刻一次，比指纹过线才算数。

## 为什么需要这套流程

AI 仿写最容易翻车的不是写不像，是**写得太像**：把原文的句子直接搬过去。或者翻到另一边，**写得太空**：用「文笔细腻」「富有张力」描述风格，模型照样学不到东西。

这套流程把两件事分开：

- **像不像** —— 交给 `style_probe.py` + `style_compare.py`，客观数字，七轴打分
- **怎么写** —— 交给 `build_prompt.py` 和它的 spec，主观规格，但每条必须带原文证据和可验收判据

读后感是判据的反面。判据的判据是：能不能交给另一个人按字照搬就能产出同样风格的文本。

## 三个模式

每个模式是独立的一步，按用户的话走，不要自作主张合并。

| 模式 | 用户这么说 | 硬门（不过不许往下走） | 产出 |
| --- | --- | --- | --- |
| **1 探** | 「分析这段文字的风格」 | 原文 ≥ 300 字；指纹已落盘；判据写在 spec 里能指回原文 | `out/ref.json` + spec 草稿 |
| **2 编** | 「把它变成提示词」 | spec 通过校验（空话报错、缺证据报错）；三档都已生成 | `prompt-short.txt` / `prompt-standard.md` / `prompt-anchored.md` |
| **3 验** | 「怎么算复刻得像」 | 同主题不同的新文本已生成；总分 ≥ 75 且每轴 ≥ 55 | `cmp.md` + pass / fail |

模式会串联，不是三选一。完整路径是 `1 → 2 → 3`；只想复用风格不写新文本的用户到 `2` 就停。

## 七条通用约束

任何模式都不许破：

1. **每条 spec 必须带 evidence**，evidence 是原文照抄的句子或词组，不是改写。改写过的句子不能佐证原文风格。
2. **每条 spec 必须带 check**，check 是不含糊的可验收判据（「感叹号 0 个」算，「读起来克制」不算）。
3. **空话不许过校验**。「文笔细腻」「富有张力」「行云流水」这类词不带数字也不带对照时，`build_prompt.py --check` 直接退出码 2。
4. **不要改原文本**。原文本是 ground truth。复刻分数低就改 spec，不是改源文本凑分。
5. **复刻文本要换主题**。让模型按规格写同主题等于背诵测试，分数会假高。换主题才能测出规格的可迁移性。
6. **anchored 不抄句子**。语感锚点只学停顿和句式，内容必须换成新主题。模型开始抄原文就退回 standard。
7. **summary 里看到的数字是约束，不是赞美**。`句均 13.2 字` 不是说原文好，是说复刻要落到 11–16 字区间。

## 什么时候不吃这套

这套流程有成本，不要用在不需要风格复制的地方：

- 纯翻译、纠错、格式整理——风格本身不是目的
- 明文规定了输出格式（公文模板、API 返回）的——按模板走
- 原文 < 300 字的——指纹偏噪到不可用，建议先扩样本再分析
- 用户只要一句话提示词 / 不在意复刻精度的——直接出 short 即可，跳过 spec

但**任何对外交付都要走模式 3**。给客户的 spec 没经过复刻验收，等于没验收。

## 目录

```
text-style-to-prompt/
├── SKILL.md
├── references/
│   ├── 01-axes.md           六轴：风格是什么、spec 怎么落
│   ├── 02-anti-empty.md     反空话：判据不能只有形容词
│   ├── 03-replay.md          复刻协议：写完之后怎么算数
│   └── 04-protocol.md        提示词结构：三档产物怎么用
├── scripts/
│   ├── style_probe.py        文本 → 七轴指纹 JSON
│   ├── build_prompt.py       spec → 三档提示词（严格校验）
│   └── style_compare.py      两份指纹 → 七轴偏差 + 总分 + exit code
├── specs/
│   └── demo-cold-tech.json   冷峻技术评论：完整 spec 示例
├── tests/
│   └── test_scripts.py       回归测试（无外部依赖）
├── examples/
│   ├── sample-input.md       原文本（冷峻技术评论风格）
│   ├── sample-repro.md       按规格复刻的新文本（换主题）
│   └── README.md             端到端示例复现说明
└── assets/
    └── README.txt
```

## 三个脚本

```bash
# 模式 1：文本 → 指纹
python scripts/style_probe.py --text examples/sample-input.md --out out/ref.json --summary
python scripts/style_probe.py --text examples/sample-input.md --pretty          # 终端打印
cat 原.txt | python scripts/style_probe.py --stdin --out out/ref.json

# 模式 2：spec → 三档提示词
python scripts/build_prompt.py --spec specs/demo-cold-tech.json --check          # 只校验
python scripts/build_prompt.py --spec specs/demo-cold-tech.json --outdir out/demo
python scripts/build_prompt.py --spec specs/demo-cold-tech.json --fmt short --stdout

# 模式 3：两份指纹 → 七轴偏差
python scripts/style_compare.py --ref out/ref.json --cand out/cand.json
python scripts/style_compare.py --ref out/ref.json --cand out/cand.json --min 80 --axis-min 60
python scripts/style_compare.py --ref out/ref.json --cand out/cand.json --json out/cmp.json
```

### style_probe.py：文本 → 指纹

要点：

- 中文 unit=char、西文 unit=word，自动判定
- `chars` 指**字母数字字符数**，不含空白与标点
- 短句 / 长句阈值按语种固定（中文 ≤8 / ≥40，西文 ≤4 / ≥20）
- 短样本（< 800 字）会标 `short_sample: true`，下游对比会因此出 warning

### build_prompt.py：spec → 提示词

要点：

- spec 必须 ≥3 条 ≤12 条，否则报名失败
- 每条 rule 含「优美 / 细腻 / 张力 / 高级」类形容词且无数字无对照时 → `SpecError` 退出 2
- 每条 rule 都要 evidence（原文照抄）和 check（可验收判据），缺一报错
- 校验通过但仍可能出 stderr warning：rule 没数字没对照但通过了空话检查——人审一眼

### style_compare.py：两份指纹 → 验收

要点：

- 七轴加权打分（见 `references/03-replay.md` 的权重表）
- 默认阈值 `--min 75 --axis-min 55`，强一点用 80/60
- 退出码：0 过线 / 1 未过线 / 2 输入有问题
- 短样本会出 warning，但**不影响判定**——接受短样本是常态

改完脚本后跑回归测试：

```bash
python tests/test_scripts.py
```