---
name: image-prompt-reverse
version: 1.0.0
description: |
  从一张上传的图片逆向推出生成它所需的提示词：先跑量测脚本把「看着像」换成
  可复现的数字（光位 / 光比 / 色板 hex / 景深 / 噪声 / 暗角 / 主体候选位置 /
  水印嫌疑），再把人眼判断填进结构化 spec，由装配器产出四档提示词
  （一句话检索版 / 自然语言整段版 / Midjourney 短语版 / SD 标签+负面版），
  最后用还原校验脚本拿重出图打分，低于 80 分就按扣分项改对应字段再出。
  内置空泛词拦截（好看 / 电影感 / 高级感直接报 W201）、
  主体候选连通域检测、四边水印/文字嫌疑检测。
  Use when asked to 反推提示词、逆向提示词、图片转提示词、分析这张图怎么生成的、
  看图写 prompt、image to prompt、reverse prompt、prompt 逆向工程、
  这张图用什么关键词、复刻这张图、img2prompt。
allowed-tools:
  - Read
  - Write
  - Bash
metadata:
  agent_created: true
  triggers:
    - 反推提示词
    - 逆向提示词
    - 图片转提示词
    - 看图写提示词
    - 这张图怎么生成的
    - 复刻这张图
    - image to prompt
    - reverse prompt
    - img2prompt
  requires:
    bins:
      - python    # >= 3.9
    python_pkgs:
      - pillow    # probe / verify / make_demo 需要；build_prompt 零依赖
---

# 图片 → 提示词：逆向工程流水线

一句话：**先量再写，写完必须重出验证；「看着像」不算数，verify 打到 80 才算数。**

和 image/ 下七个生成 skill 的分工相反：它们从提示词出图，本 skill 从图回提示词。
它是它们的逆操作——你给任何一张 AI 图 / 照片，它还你一份能重出这张图的结构化提示词。

## 四条不可让的约束

### 1. 量出来的才算数，看出来的不算

「电影感」「高级感」「氛围感」这类词在提示词里是无效的——模型会按自己的理解乱填。
本 skill 的第一步永远是跑 `probe.py` 把图量成数字：

| 你看着像 | 实际要量的是 | 量出来写成 |
| --- | --- | --- |
| 电影感 | 画幅 2.39:1？青橙分离？高光柔化？ | `aspect 2.39:1` + 色板 + 低反差 |
| 暖调 | R-B 差值 | `暖调（金黄 / 橙）`（R-B 0.100） |
| 虚化背景 | 中心/边缘梯度比 | `浅景深`（比值 1.9） |
| 胶片颗粒 | 平坦区噪声 RMS | `明显颗粒（胶片感）`（RMS 4.2） |

probe 的报告 `probe.md` 第 7 节「转写建议」已经把每条数字翻译成了可以进提示词的句子——
**直接用，别自己再编形容词。**

### 2. 主体判断可以商量，光位色温不行

主体候选是「与边带背景差异大的连通块」，是启发式，亮源 / 深影 / 装饰都可能上榜——
probe 给你最多 3 个候选，**选哪个由你看图决定**（`probe.png` 上黄框是首选）。

但光位、色温、反差、画幅这些是客观量，probe 说什么就是什么，
不要因为「我觉得更像右上方来光」就改掉「水平带状光源」——你看到的是内容，probe 量的是光。

### 3. 空泛词进不了门

`build_prompt.py` 的质量门会拦这些词：好看 / 精美 / 高级感 / 电影感 / 氛围感 /
艺术感 / 唯美 / 质感 / 细节丰富 / masterpiece / best quality / cinematic。

它们出现时报 **W201**，必须换成可执行参数。对照表在 `references/02-vocabulary.md`：
「电影感」= 画幅 + 青橙分离 + 高光柔化；「高级感」= 低饱和 + 大留白 + 单一材质。

### 4. 没有验证就没有交付

反推出来的提示词只是**假设**。必须拿它去重出一张图，然后跑：

```bash
python scripts/verify.py --ref 原图 --gen 重出图 --outdir out/verify
```

- 80+：通过，交付
- 60–79：看扣分项，一次只改一个字段重出
- <60：直接重出，别想着后期救

verify 的每一项扣分都带「改哪个字段」的指引。一次改多处会不知道是哪个生效——**改一处，出一处，验一处。**

## 流水线

### Step 0 — 收图

要清楚原图、原图、原图。二次压缩 / 截图 / 加水印的版本会让噪声和锐度两项失真。

如果图上明显有文字 / 水印 / 角标：先决定它是**内容**（海报标题）还是**叠加物**（平台水印）。
叠加物不进正向提示词，进 `negatives`。`probe.py` 会检测边带与角落的梯度密度给出嫌疑提示，
最终判断在人。

### Step 1 — 量测

```bash
python scripts/probe.py 图片.jpg --outdir out/probe
```

产出：
- `probe.json`：机器读，下一步给 build_prompt 补缺
- `probe.md`：人读，7 节，最后有「转写建议」
- `probe.png`：标注图（九宫格 / 三分线 / 主体候选框 / 底部色卡）

读数要点：
- **光位**：九宫格 + 亮带判定。渐变图会把「中心聚光」误判率拉高——看九宫格数值分布，别只看标签
- **主体候选**：黄框是建议，橙框是第二、三候选；置信度 low 时全部人眼确认
- **暗角**：四角比同半径边中点暗 >10% 才报「疑似」，且注明「内容影响大」——地面在四角会把这项推高
- **文字嫌疑**：底部 / 顶部 / 角落梯度比 > 1.25 才报，报了就要决定它是内容还是水印

### Step 2 — 填 spec

复制 `specs/dune-silhouette.json` 改。八段，每段对应提示词的一段：

| 段 | 必填 | 从哪来 |
| --- | --- | --- |
| subject | what + detail | **人眼**（probe 只给位置，不给「是什么」） |
| action | 动词 | 人眼 |
| environment | place + foreground + background | 人眼 + probe 候选 |
| light | direction / quality / time / temp | probe（merge 时自动填） |
| camera | shot / lens / angle / dof | probe（dof / sharpness）+ 人眼（shot / angle） |
| material | 材质触感 | 人眼 + probe 噪声 |
| style | medium / render / references | 人眼 |
| composition | aspect / subject_position / line_hint | probe（自动填） |
| negatives | 要排除的 | 人眼 + 文字嫌疑判断 |

质量门会拦：空主体（E101）、空镜头（E103）、空光（E104）、空环境（E105）、
空画幅（E107）、负向词进正向段（E108）、不可执行词「大概 / 可能」（E106）。

### Step 3 — 装配

```bash
python scripts/build_prompt.py --spec specs/xxx.json --outdir out/xxx --probe out/probe/probe.json
```

`--probe` 把 spec 的空位自动填上（光位 / 色板 / 画幅 / 景深 / 主体位置），
**不覆盖**人写的判断。产出四档：

- `prompt-short.txt`：一句话，40 词内，检索 / 快速重出用
- `prompt-natural.txt`：六段整句，给吃自然语言的模型（Hy / GPT-image / 豆包 / 即梦）
- `prompt-mj.txt`：逗号短语 + `--ar`，给 Midjourney
- `prompt-sd.txt` + `prompt-sd-negative.txt`：标签 + 负面，给 SD 系

`report.md` 里有质量门结果和每档词数。

### Step 4 — 重出

拿最贴近目标模型的那一档去出图。出图参数按目标模型的默认甜点来；
如果原图本身带参数线索（MJ 的 `--v 6` 水印、SD 的 PNG 元数据），优先用那套。

出完存到 `out/xxx/raw/`。

### Step 5 — 校验

```bash
python scripts/verify.py --ref 原图 --gen out/xxx/raw/1.png --outdir out/xxx/verify
```

九项比对：画幅 / 整体亮度 / 明暗分布 / 色板距离 / 饱和度 / 色温 / 景深方向 / 锐度 / 反差。
每项扣分都带「改哪个字段」。

低于 80：按 `top_fix` 改 spec，重跑 build_prompt，再出再验。
**不要连续改两个字段。**

## 验收清单

**量测**
- [ ] probe 三项硬读数（光位 / 色板 / 画幅）与原图一致
- [ ] 主体候选选对了（标注图上黄框圈的是真主体，不是亮源 / 阴影）
- [ ] 水印 / 文字嫌疑已定性：进正向还是进 negatives

**提示词**
- [ ] 质量门无 E 级
- [ ] 没有空泛词（好看 / 电影感 / 高级感）
- [ ] 主体段含 what + detail（不是「一个人」「一只猫」）
- [ ] 光、镜头、环境三段齐全
- [ ] 色板是 hex 不是「色彩丰富」

**验证**
- [ ] verify ≥ 80
- [ ] 扣分项 < 3 处
- [ ] 改过之后重出再验过

## 已知局限

- probe 的「主体候选」是启发式：强渐变背景（黄昏天空、水面反光）会把大片背景算进候选，
  标注图上框很大时以人眼为准
- 暗角判定受内容影响大：四角本身是暗部（地面、阴影、边框）时会高估，
  报告里已注明「须人眼对照」
- 锐度评分用中值滤波压噪，强颗粒图（胶片）会低估真实锐度
- 原图经过二次压缩 / 截图 / 平台水印时，噪声和锐度读数失真，
  这种情况先找原始文件，找不到就在报告里注明「量测基于压缩图」
- 不支持动图 / 多帧；只取第一帧量测

## 参考文件

- `references/01-observation.md` — 看图顺序与八维判据：先看什么、每项怎么判
- `references/02-vocabulary.md` — 空泛词 → 可执行参数对照表（电影感 / 高级感 / 氛围感…）
- `references/03-grammar.md` — 四档提示词的语法结构与各模型差异
- `references/04-failure-modes.md` — 逆向常见失败模式与改法
- `specs/dune-silhouette.json` — 示例 spec
- `examples/dune-silhouette.md` — 端到端案例：从演示图到 verify 打分的完整命令序列
- `scripts/make_demo.py` — 合成演示图，让 examples 零素材可跑
- `tests/test_scripts.py` — 自测：50 项断言，不需要外部图
