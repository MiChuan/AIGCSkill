---
name: hy-image35-cinematic
version: 1.0.0
description: |
  用 Hy Image3.5 preview（混元图像 3.5 预览版，HY Image3.5 preview）出「剧集级电影单帧」。
  给一张人物参考图，锁住五官不动（不换脸、不改脸型），把同一个人放进任意剧本场景
  （御剑云端、宫阙、雪原、市井……），用电影镜头语言描述机位 / 焦段 / 构图四角，
  产出右下角干净无水印、8K 交付尺寸的成图。
  内置：结构化提示词装配器（纯标准库）、保脸检查清单、水印检测与清除、Lanczos 8K 重采样与锐化。
  Use when asked to 用混元出图、Hy Image3.5 出图、参考图保脸生成、剧集画风出图、8K 大图、
  电影分镜单帧、image gen with character reference、生成一张电影感的图。
allowed-tools:
  - Read
  - Write
  - Bash
  - Glob
metadata:
  version: 1.0.0
  license: Apache-2.0
  triggers:
    - hy-image35
    - Hy Image3.5
    - 混元出图
    - 混元图像
    - 参考图保脸
    - 剧集画风
    - 8K 出图
    - 电影单帧
    - cinematic still
  requires:
    bins:
      - python    # >= 3.9
    python_pkgs:
      - pillow    # 仅 finalize.py 需要；build_prompt.py 零依赖
---

# Hy Image3.5 剧集级单帧出图

一条固定流水线：**参考图锁脸 → 结构化提示词 → 出图（不请求水印）→ 清角 + 8K 重采样 → 验收**。

模型事实（不要凭空承诺，写进文案时按这个口径）：

| 项目 | 事实 |
| --- | --- |
| 模型 | Hy Image3.5 preview（腾讯混元，2026-09 发布），文生图 / 图生图 / 多轮编辑 |
| 原生分辨率 | 官方宣传最高 **2K**；API 显式传 size 可到 4096×4096，单边最大 8192（总面积上限 16,777,216 px） |
| 参考图 | 单次最多 5 张（部分接入方 20 张），每张 ≤ 20MB |
| 水印 | API 的 `footnote` 是**自定义水印文本**，不传就不加水印；这是"去水印"的第一道手段 |
| 因此 | **8K 不是原生出图**，是 2K 出图后由 `finalize.py` 重采样到 8K 交付尺寸。要如实告知用户 |

## 何时用

- 用户给一张人物照片 / 已有角色图，要求"用这个人"生成新场景的图。
- 要求电影剧集质感、指定机位与焦段、指定画面四角放什么。
- 要求 8K、无水印、可直接交付的成图。

不适用：纯文生图海报/文字排版类需求（走通用出图）；角色三视图设定集（走 character-refs）。

## 流水线

### Step 0 — 收集四件套

缺一件就先问，不要猜：

1. **参考图路径**（本地绝对路径或 URL）。用户说"我上传的图"就去找；找不到就问路径。
2. **主要求**：人物在做什么 + 穿什么 + 拿什么 + 场景。
3. **镜头**：视角（俯拍/平视/仰拍）、方位角、焦段、景别。用户没给就按 `references/prompt-formula.md` 给一套合理化默认值并说明。
4. **风格锚点**：用户写的《参考剧集》《XX》之类，是占位符还是真实片名？真实片名就映射成风格描述词，占位符就问，或用"电影剧集写实质感"兜底。

**出图会消耗积分（一张约 5–10 积分）。** 开跑前一句话告知，批量出图先确认张数。

### Step 1 — 装配提示词

```bash
python scripts/build_prompt.py --spec specs/<name>.json --outdir out/<name>
```

产出 `out/<name>/prompt.txt`（直接喂给模型的中文结构化提示词）、`meta.json`（含推荐的 `imagegen` 参数、镜头信息、检查项）。

装配规则与词库见 `references/prompt-formula.md`；`build_prompt.py` 会把镜头段落和"保脸条款"自动拼好，其余段落从 spec 原样引用。**不要手写绕过它**——它同时做质量门检查（见 Step 4）。

### Step 2 — 出图（关键参数，照抄）

用内置图像生成能力（ImageGen），参数：

| 参数 | 值 | 为什么 |
| --- | --- | --- |
| `prompt` | `prompt.txt` 全文 | 结构化中文，模型对分节提示响应更好 |
| `image1` | 人物参考图绝对路径 | 身份来源，必须有 |
| `image2` | 可选：服装/道具参考 | 只锁人设不锁脸时用 |
| `input_fidelity` | `high` | 图生图贴近原图，保脸的第一开关 |
| `revise` | `false` | **必须关**。提示词改写会删掉保脸条款和机位约束 |
| `footnote` | **不传** | 该字段就是水印文本，不传 = 不加右下角水印 |
| `quality` | `high` | |
| `size` | `2048x1152`（16:9）/ `1152x2048`（9:16）/ `2048x2048`（1:1） | 原生上限附近；报错就降到 `1536x864` |
| `output_dir` | `out/<name>/raw` | 原图留档，别覆盖 |

一次只出一张，看完再决定下一张。**不要一次抛 4 张**——废图会稀释判断。

### Step 3 — 清角 + 8K

```bash
python scripts/finalize.py out/<name>/raw/xxx.png --out out/<name>/<name>_8k.jpg --target 8k --watermark auto
```

它做三件事：检测右下角水印 → 有则用邻近干净区域镜像补片并羽化融合 → Lanczos 重采样到 8K（16:9 为 7680×4320）并做轻锐化。

- 水印没被自动识别（不同接入方水印位置/浓淡不一），用 `--box x,y,w,h` 手动指定，或 `--watermark off` 跳过。
- 需要 4K 就 `--target 4k`；保留无损就 `--format png`。
- 输出尾部会打印一段 JSON 报告：原始尺寸、成图尺寸、是否检测到水印、用了哪个 bbox。

### Step 4 — 验收（逐条过，不许"看起来还行"）

**脸**
- [ ] 与参考图对比：脸型骨相、眉眼间距、鼻头形状、唇形、双眼皮形态、发际线一致
- [ ] 没有美颜磨皮 / 没有"网红脸"化 / 没有变年轻或变老
- [ ] 脸部占比 ≥ 画面高度 8%，五官没被笛子、手、头发、逆光吃掉

**画面**
- [ ] 用户点名的四角元素都在位（右上/左下……）
- [ ] 机位与焦段能看出来：广角透视有没有夸张？俯拍有没有体现纵深？
- [ ] 无多余手指/肢体、无重复人物、无文字乱码
- [ ] 无水印、无 logo、无签名、无画框
- [ ] 8K 文件打开看细节：布料、玉器、发丝、云粒没有糊成一片

**不达标就改，不要交付。** 保脸失败按 `references/character-lock.md` 的排查表处理（八成是脸部占比太小或被遮挡，而不是模型不行）。

## 常用命令

```bash
# 只校验 spec，不出图
python scripts/build_prompt.py --spec specs/yujian-kunlun.json --check

# 给已有成图补做 8K
python scripts/finalize.py in.jpg --out out.png --target 8k --watermark off

# 水印位置写死（x,y,w,h，按原图像素）
python scripts/finalize.py in.png --out out.jpg --watermark box:1620,980,400,150
```

## 参考文件

- `references/prompt-formula.md` — 提示词公式、镜头词库、风格锚点映射表
- `references/character-lock.md` — 保脸原理、参考图准备、失败排查表
- `references/postprocess.md` — 水印清除与 8K 重采样的原理、参数、局限
- `examples/yujian-kunlun.md` — 完整示例：云端御剑 / 昆仑雪山 / 白玉天宫
