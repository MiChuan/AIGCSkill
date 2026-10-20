# AIGCSkill

把「一句话需求」做成**可运行的 skill 包**：每个 skill 一个目录，一份 `SKILL.md` 说清什么时候用它、按什么顺序做、做到什么程度算过，配一套零依赖脚本把最容易出错的那几步变成命令。

覆盖方向：文本、图片、视频、漫画、漫剧、游戏、3D 世界、项目管理、代码生成、法律文书。
当前已落地 **9 个**：文本 / 工程类 2 个，图片类 7 个。

<p>
<img alt="license" src="https://img.shields.io/badge/license-MIT-blue">
<img alt="python" src="https://img.shields.io/badge/python-3.9%2B-3776AB">
<img alt="deps" src="https://img.shields.io/badge/third--party%20deps-none%20%2F%20Pillow-brightgreen">
</p>

---

## 目录

```
AIGCSkill/
├── code/    # 代码生成 / 工程自动化类（规划中，暂空）
├── text/    # 文本与工程类：cantonese-coach、code-discipline
├── image/   # 图片生成类：hy-image35-* 七个流水线
├── LICENSE  # MIT
└── README.md
```

每个子目录是一个**独立可用**的 skill，单独拷走也能跑，互相不依赖。

---

## Skill 索引

### text/ — 文本与工程类

| Skill | 解决什么问题 | 关键产物 | 依赖 |
| --- | --- | --- | --- |
| [cantonese-coach](text/cantonese-coach/SKILL.md) | 粤语一对一教练：六题诊断定级 → 4 周计划 → 100 核心词分层 → 实时对话纠错 → 语法／发音／记忆法／每日 20 分钟 | `plan.md` + `schedule.csv`、分层词表 + Anki CSV + 默写纸、**每句强制「汉字 / 粤拼 / 意思」三行制** | 纯标准库 |
| [code-discipline](text/code-discipline/SKILL.md) | 七条工程纪律，管的是动手之前和动手之后：看清再改、最小 diff、先复现再修、先固化再重构、对抗性测试、只查三类问题的审查、单文件 HTML 报告 | 勘察报告、失败的复现测试、单文件 HTML 审查报告 | 纯标准库 |

两者都是**约束型**：不产出内容，产出「这一步过了没有」的判据。`cantonese-coach` 里最容易自查失败的项是粤拼声调，`code-discipline` 里最容易滑过去的是「改动是否真的最小」——所以各有一个脚本把判断变成 exit code。

### image/ — 图片生成类

七个 skill 共用同一套出图口径（Hy Image3.5 preview、参考图保脸、`revise=false`、`input_fidelity=high`、无水印、8K 交付），差别在**对照片动了什么**：

| Skill | 对照片做什么 | 产物 | 验收指标 |
| --- | --- | --- | --- |
| [hy-image35-cinematic](image/hy-image35-cinematic/SKILL.md) | **人进场景**：保住这个人，换掉周围 | 剧集级电影单帧 8K | 保脸清单、右下角水印检测 |
| [hy-image35-lowkey-portrait](image/hy-image35-lowkey-portrait/SKILL.md) | **人被重新打光**：换掉光与背景 | 伦勃朗单侧硬光 + 纯黑背景人像，3:2 | 背景纯黑校验、水印清除、8K 重采样 |
| [hy-image35-photo-seal](image/hy-image35-photo-seal/SKILL.md) | **照片被压成符号**：细节砍两个数量级 | 朱砂印章，圆形／方形，4K + 透明底 | 印面定位、掩膜净化、单色度／覆盖率 |
| [hy-image35-nationalgeo](image/hy-image35-nationalgeo/SKILL.md) | **照片被重新洗一遍**：内容不动，只换光色关系 | 纪实调色成片 2K–8K | 六轴量化体检（唯一给成图打分的之一） |
| [hy-image35-ink-print](image/hy-image35-ink-print/SKILL.md) | **照片被重排成版面**：解构成几何面，承载换成米白特种纸 | 水墨扁平插画 + 底部两行英文 | 纸色／留白率／重心／字号比／边缘陡度 |
| [hy-image35-kr-school-id](image/hy-image35-kr-school-id/SKILL.md) | **人被放进受控影棚**：脸不动，光位／服装／背景／边框全定死 | 韩系学院风证件写真 8K | 四边严格等宽、水印修补后的边缘能量 |
| [hy-image35-hair-code](image/hy-image35-hair-code/SKILL.md) | **先量人，再改发**：只换发际线以上的形状 | 同一人的 2–4 款试发图 + 对比图 | 三位代码（脸型／最宽处／指宽）+ 发质查表 |

按「改动面积」排：`hair-code` 最小（只动头发）→ `lowkey-portrait` → `kr-school-id` → `cinematic` → `nationalgeo`（不动内容）→ `ink-print` → `photo-seal`（砍到只剩骨架）。**先确定你要哪一种，再动手**，七个混着用会互相打脸。

---

## 快速开始

### 方式一：装进 WorkBuddy 技能目录（推荐）

```bash
# Windows (Git Bash)；macOS / Linux 路径改成 ~/.workbuddy/skills 同理
cp -r text/cantonese-coach     ~/.workbuddy/skills/
cp -r text/code-discipline     ~/.workbuddy/skills/
cp -r image/hy-image35-cinematic ~/.workbuddy/skills/
# ……其余同理
```

装好后直接说需求就会命中：

- **粤语**：「学粤语」「帮我定个 4 周计划」「练一段点餐对话」「粤拼怎么读」
- **工程**：「先别改，看看这个项目怎么实现的」「用最小改动做」「先写个测试复现」「审查这次提交」「不要 Markdown，做成 HTML」
- **图片**：「用这张照片生成一个场景」「做一张伦勃朗光人像」「做成印章」「调成国家地理的色调」「做成水墨留白的平面设计图」「韩系学院风证件照」「我适合什么发型」

### 方式二：当手册 + 命令行工具用

不装也行。读 `SKILL.md` 按步骤走，脚本单独跑：

```bash
# 文本类
cd text/cantonese-coach
python scripts/check_jyutping.py --text "ngo5 sik6 gan2 faan6"     # 粤拼合法性校验
python scripts/build_plan.py   --spec specs/travel-beginner.json --outdir out/plan
python scripts/vocab_deck.py   --deck specs/deck-core-100.json   --outdir out/deck
python tests/test_scripts.py

cd ../code-discipline
python scripts/survey.py --root ../myproj                          # 勘察：省掉翻目录
python scripts/build_report.py --spec examples/review-demo.json --out out/review.html --open
python tests/test_scripts.py

# 图片类，以水墨版面为例
cd ../../image/hy-image35-ink-print
python scripts/extract_palette.py assets/demo-ref.png --outdir out/pal --k 6 --top 0.62
python scripts/build_print_prompt.py --spec specs/ink-print-lake.json --outdir out/demo
python scripts/printcheck.py out/demo/raw/x.png --layout out/demo/meta.json --annotate out/demo/check.png
python scripts/selftest.py                                         # 37 项断言，不需要外部图
```

每个目录的 `README` 之外，详细命令、参数、验收标准都在该 skill 的 `SKILL.md` 里。图片类另外三个带自测的：`kr-school-id` 39 项、`hair-code` 296 项、`ink-print` 37 项，全部不依赖外部素材，改完脚本先跑它。

---

## 通用约定

```
<skill-name>/
├── SKILL.md      # frontmatter 必填 name / description / version；正文是模式索引 + 硬门 + 验收
├── references/   # 判据库：每个模式一篇，写不出判据的段落删掉
├── scripts/      # 可独立运行的 CLI，零依赖或单一轻依赖（Pillow）
├── specs/        # 结构化输入（JSON），脚本的入口参数
├── data/         # 结构化知识表（可选，如 hair-code 的 9×3 款式矩阵）
├── tests/        # 回归测试，python tests/test_scripts.py 直接跑
├── examples/     # 一个完整可复现的案例（含成品）
├── assets/       # 本地素材；图片不入库，只留 README.txt
└── out/          # 产物，已在 .gitignore 忽略
```

三条底线：

1. **能跑才算数**。`scripts/` 里的东西必须脱离 agent 也能用，参数写进 `--help`，示例写进 `examples/`。
2. **只留判据**。`references/` 不是背景知识，是「这一步过没过」的判断依据；解释不了怎么验收的内容不写。
3. **产物不入库**。`out/` 全部忽略，`assets/` 只留说明。

## 新增一个 skill 的检查清单

- [ ] 目录名 = `name` 字段，全小写连字符
- [ ] `description` 里写清「做什么」+「什么时候用（含中英文触发词）」
- [ ] `SKILL.md` 有明确的 Step 顺序和每步的硬门
- [ ] 至少一个脚本能独立跑，`--help` 有示例
- [ ] `tests/` 或 `selftest.py` 能在无外部素材下全绿
- [ ] `examples/` 有一个端到端可复现案例
- [ ] `assets/` 不放二进制素材

## 路线图

已落地：文本教练、工程纪律、图片七条流水线。
待补：视频、漫画 / 漫剧、游戏、3D 世界、项目管理、法律文书（`code/` 预留给代码生成与工程自动化）。

## License

[MIT](LICENSE)
