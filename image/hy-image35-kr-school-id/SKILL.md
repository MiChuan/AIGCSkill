---
name: hy-image35-kr-school-id
version: 1.0.0
description: |
  「韩系学院风证件写真」出图流水线：给一张人物照片，用 Hy Image3.5 preview 生成
  制服证件照风格的胸像（深色西装外套 + 藏青 V 领针织背心 + 白衬衫 + 条纹领带 /
  浅灰蓝磨砂背景 / 大面积柔光 / 高调胶片质感），
  锁住原图五官与骨相，产出带精确等宽白边框、底部英文字排版、无水印的 8K 成图。
  内置：13 节提示词装配器（会把「瘦脸/尖下巴」这类改形词自动翻译成打光方案）、
  负面词冲突检测、文字路线自动决策（模型直写 / 后期叠字）、
  水印检测与逐车道插值修补、按像素计算的边框、带字距控制的排版叠字。
  Use when asked to 韩系证件照、学院风证件写真、韩系学院风写真、制服证件照、
  男女团练习生档案照、入学册照片、毕业纪念册照片、ID portrait、preppy school portrait。
allowed-tools:
  - Read
  - Write
  - Bash
  - Glob
metadata:
  version: 1.0.0
  license: Apache-2.0
  triggers:
    - 韩系证件照
    - 学院风证件写真
    - 韩系学院风
    - 制服证件照
    - 练习生档案照
    - 入学册照片
    - 毕业纪念册
    - preppy ID portrait
    - korean school portrait
  requires:
    bins:
      - python    # >= 3.9
    python_pkgs:
      - pillow    # 仅 finalize.py / settype.py 需要；build_id_prompt.py 零依赖
---

# 韩系学院风证件写真

一条固定流水线：**收四件套 → 装配提示词 → 出图 → 收尾（水印 + 8K + 白边框）→ 排版 → 验收**。

这个 skill 和前四个的分工：它不是"换场景"也不是"改调色"，而是**把人放进一个受控的影棚**——
构图、光位、服装、背景、边框、版式全部提前定死，模型的自由只留在脸上。

模型事实（写文案时按这个口径，不要承诺更多）：

| 项目 | 事实 |
| --- | --- |
| 模型 | Hy Image3.5 preview（腾讯混元），文生图 / 图生图 |
| 原生分辨率 | 2K 级；显式 size 可到 4096，单边最大 8192（总面积 ≤ 16,777,216 px） |
| 参考图 | 单次最多 5 张，每张 ≤ 20MB；**是条件生成，不是人脸 ID 注入** |
| 水印 | API 的 `footnote` 就是水印文本，不传 = 不加 |
| 因此 | **8K 是重采样交付尺寸**，不是原生细节。不要对外说「真 8K」 |

## 何时用

- 用户给一张照片，要"韩系""学院风""证件照""制服写真""练习生档案照""入学册那种"。
- 要固定版式：居中胸像、浅灰蓝底、底部一行英文、外面一圈白边。
- 要男女两版同款（同一个模板换人）。

不适用：
- 要生活感、抓拍感 → 走 `hy-image35-nationalgeo`（纪实调色）
- 要纯黑背景 + 硬侧光 → 走 `hy-image35-lowkey-portrait`
- 要人进到某个具体场景 → 走 `hy-image35-cinematic`

## 流水线

### Step 0 — 收四件套

缺一件就问，别猜：

1. **参考图**（本地绝对路径）。用户说"我上传的图"就去找；找不到直接问路径。
2. **性别与形象**：男生 / 女生；发型、是否戴眼镜、有无耳饰。
3. **文字**：底部要排什么（主标题 / 副标题 / 页脚）。用户写"英文小字"这种就归到副标题。
   **歌词、台词、品牌 slogan 有版权，不要直接写进去**——用一句自创的等效英文替代并说明。
4. **画幅**：默认 3:4。要印出来就说清楚，打印尺寸和画幅必须对得上。

**出图会消耗积分（一张约 5–10 积分）。** 开跑前一句话告知，批量先确认张数。

### Step 1 — 装配提示词

```bash
python scripts/build_id_prompt.py --spec specs/male-glasses.json --outdir out/male
python scripts/build_id_prompt.py --spec specs/female-wave.json --outdir out/female
```

产出三个文件：

- `prompt.txt` — 13 节结构化中文，直接喂模型
- `negative.txt` — 去重后的负面词（默认表 27 条 + 用户给的 13 条，合并成 40 条）
- `meta.json` — 出图参数、文字路线、边框像素、质量门结论

打印出来的 `[ERROR]/[WARN]/[INFO]` 要逐条读。`E` 开头是硬错误，`exit=1`；`W` 只是提示。

三条会被脚本自动处理的事，知道它在做什么：

1. **改形词 → 打光方案**。用户写"精瘦脸型 / 下颌线清晰 / 体脂率 15%"时，这句话和"保留原图五官样貌"在字面上是互斥的（一个要求几何不变，一个要求几何改变）。脚本把它翻译成"脸型与参考图一致，用光把轮廓勾出来"，并在 `meta.issues` 里记一条 `I502`。
2. **文字路线**。主标题 ≤ 24 字符、副标题 ≤ 46、页脚 ≤ 46，且不含中文 → `model`（让模型写）；超出或含中文 → `overlay`（模型只留白，后期叠字）。中文小字模型必糊，这不是保守。
3. **边框**。提示词里明确禁止模型画框，只要求四周留 3% 安全边。边框由 `finalize.py` 按像素加。

### Step 2 — 出图（参数照抄）

| 参数 | 值 | 为什么 |
| --- | --- | --- |
| `prompt` | `prompt.txt` 全文 | 分节提示的响应明显更好 |
| `image1` | 人物参考图绝对路径 | 身份来源，必须有 |
| `input_fidelity` | `high` | 保脸第一开关 |
| `revise` | `false` | **必须关**。改写会删掉保脸条款和"不要画框"的约束 |
| `footnote` | **不传** | 该字段就是水印文本 |
| `quality` | `high` | |
| `size` | `1536x2048`（3:4）/ `1600x2000`（4:5）/ `2048x2048`（1:1） | 取 `meta.size` |
| `output_dir` | `out/<name>/raw` | 原图留档 |

一次出一张，看完再决定下一张。

### Step 3 — 收尾：水印 + 8K + 白边框

```bash
python scripts/finalize.py out/male/raw/x.png --out out/male/x_8k.jpg --target 8k --border 0.015
```

处理顺序是硬约束，写死在脚本里：**trim → 水印 → 重采样 → 边框**。

- 水印必须在加框之前：水印可能正好落在要被白边盖住的那一圈上，先加框会把水印留在框内。
- 边框必须重采样之后：边框宽度按交付尺寸算，先加框再放大会让框跟着放大、边缘还多一圈插值糊边。

三种情况要额外处理：

```bash
# 模型自己画了框（四边发白，脚本会提示）
python scripts/finalize.py raw.png --out x.jpg --trim 0.025 --border 0.015

# 自动检测漏了水印（浅灰底上的淡水印容易漏）
python scripts/finalize.py raw.png --out x.jpg --watermark box:1180,1980,420,90

# 不要边框
python scripts/finalize.py raw.png --out x.jpg --border off
```

尾部 JSON 里的 `border.verify.measured_px` 是**从成图上量回来的四边宽度**，四个数必须相等。
不等就说明画面边缘本身接近框色，发丝线被算进了框里——按实测值交付即可。

### Step 4 — 排版叠字（`route=overlay` 时）

```bash
python scripts/settype.py out/male/x_8k.jpg --out out/male/x_final.jpg --spec specs/male-glasses.json
```

- 自动跳过底部白边框（`--skip-bottom` 可手动指定）。
- 自动选字色：底色浅用深灰，底色深用近白；然后算对比度，低于 3:1 直接报警。
- 字距由 `--scale` 之外的三档固定比例控制（主标题 14%、副标题 26%、页脚 22% 的字号），**不靠模型的手感**。

顺序必须是 `finalize` → `settype`：先加边框，文字才知道该躲到哪里。

### Step 5 — 验收（逐条过，不许"看起来还行"）

**脸**
- [ ] 并排比对参考图：脸的长宽比、三庭、眉眼间距、鼻翼宽度、唇形、单双眼皮形态一致
- [ ] 没有被"瘦脸"（下颌角被削、下巴变尖、额头被收窄）
- [ ] 戴眼镜时：瞳孔、双眼皮、眼裂两端透过镜片看得清；镜片不反白
- [ ] 眼神光是两块形状相同、位置对称的柔光斑

**服装与背景**
- [ ] 三件套齐：外套 / V 领背心 / 白衬衫；领带条纹方向和宽度对
- [ ] 背景是**浅灰蓝的磨砂渐变**，不是纯平色、不是影棚布纹、不是墙面接缝
- [ ] 人物边缘有轻微景深柔化，不是硬抠图

**边框与版式**
- [ ] 白边框四边等宽（看 `border.verify.measured_px`，也要用眼看）
- [ ] 没有双框（模型画的 + 后期加的）
- [ ] 文字在底部留白带内，不压衣领、不压脸、没排到白边上
- [ ] 逐字核对拼写：字母数、词间距、有没有多出来的字母
- [ ] 字距是均匀加宽，不是"用空格拉开"

**文件**
- [ ] 无水印、无 logo、无签名
- [ ] 交付尺寸 = 5760×7680（3:4 的 8K）；比例没被边框改掉
- [ ] 放大到 100% 看：睫毛、发丝、针织罗纹、西装斜纹都在

**不达标就改，不要交付。** 保脸失败先查 `references/face-lock.md` 的排查表——八成是脸部占比太小、被眼镜挡住、或者提示词里混进了改形词。

## 常用命令

```bash
# 只校验 spec，不出图
python scripts/build_id_prompt.py --spec specs/female-wave.json --check

# 全量自检（39 项断言，用合成图，不依赖外部照片）
python scripts/selftest.py -v

# 从已有成图补做边框
python scripts/finalize.py in.png --out out.jpg --target 8k --border 0.02 --watermark off

# 只叠字，不改图
python scripts/settype.py in.jpg --out out.jpg --headline KARINA --sub "STILL CHASING THE LIGHT"

# 端到端演示（合成素材）
python out/demo/make_demo.py
```

## 参考文件

- `references/prompt-formula.md` — 13 节的作用与顺序、每节的关键词和陷阱、负面词设计、画幅对照表
- `references/face-lock.md` — 保脸原理、为什么改形词必须翻译、眼镜的遮挡代价、失败排查表
- `references/typography.md` — 小字为什么会崩、两段式路线、字距与字号换算、边框为什么必须后期加
- `examples/kr-school-id.md` — 男女两版完整案例：spec、成品要点、实测读数、失败复盘
- `specs/male-glasses.json` / `specs/female-wave.json` — 两套可直接跑的 spec
