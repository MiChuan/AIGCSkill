# text — 文本 / 工程类 Skill

本目录按主题收录文本与工程方向的可复用 skill，每个子目录是一个独立可用的 skill（含 `SKILL.md`）。

| Skill | 做什么 | 模型 / 依赖 | 状态 |
| --- | --- | --- | --- |
| [cantonese-coach](cantonese-coach/SKILL.md) | 粤语一对一学习教练：六题诊断 → 4 周计划 → 100 核心词分层 → 实时对话纠错 → 语法／发音／记忆法／每日 20 分钟练习；所有粤语句强制「汉字 + 粤拼 + 意思」三行制，粤拼由脚本校验 | 零第三方依赖（纯标准库 Python ≥ 3.9） | 可用 |
| [code-discipline](code-discipline/SKILL.md) | 七条工程纪律：看清再动手、最小改动、先复现再修、先固化再重构、对抗性测试、只查三类问题的提交审查、单文件 HTML 报告 | 零第三方依赖（纯标准库 Python ≥ 3.9） | 可用 |

## 与 image 目录的区别

`image/` 下的 skill 都是**产物生成型**：给输入，出一张图，靠提示词和收尾脚本保证质量。
`code-discipline` 是**行为约束型**：它不生成代码，它约束「在什么条件下才允许动手」，以及动手之后必须交出什么证据。
`cantonese-coach` 是**领域教练型**：它输出的是教学内容，但每一段内容都要过可验证的判据（粤拼合法性、新项数量、纠正数量、三行制是否齐全），所以它既是教材也是验收表。

所以它们的「脚本」都不是在产出内容，是在**降低执行成本**——勘察脚本省掉半小时翻目录，报告脚本把结构化结论变成没人能滑过去的 HTML，粤拼校验脚本把「模型写错一个声调」这种最难自查的错变成一条 exit code。

## 七条纪律是什么

一句话：**六个「先不要」的顺序。**

| # | 纪律 | 硬门 |
| --- | --- | --- |
| 1 | 先读懂再动手 | 复述 + 假设 + 最小方案 + ≤3 个问题，等确认 |
| 2 | 最小改动 | 每一行 diff 都能对上一条需求 |
| 3 | 先复现再修 | 有一个在**未修改代码**上失败的测试 |
| 4 | 先固定再重构 | 新测试在**原代码**上全绿 |
| 5 | 对抗性测试 | 八类攻击面逐类过，不是只测空输入 |
| 6 | 只查三类 | 每条带 `文件:行` 和触发条件，风格问题一条不提 |
| 7 | 输出 HTML | 单文件、无外链、生成后已打开浏览器 |

## 粤语教练的七个模式

一句话：**这不是「用粤语聊天」，是把粤语拆成可以训练的最小单位。**

| 模式 | 触发 | 产出 |
| --- | --- | --- |
| M0 诊断 | 第一次接触 | 六题分数 → A0/A1/A2/B1 |
| M1 导师 + 4 周计划 | 「帮我定个计划」 | `plan.md` + `schedule.csv` |
| M2 核心词汇 | 「给我最有用的 100 个词」 | 分层词表 + Anki CSV + 默写纸 |
| M3 实时对话 | 「同我练对话」 | 对话 + 每轮 ≤3 个修正 + 母语者版本 |
| M4 语法讲解 | 「解释一下 X」 | 一句定义 + 3 例 + 1 个必避错 |
| M5 发音训练 | 「练 X 的发音」 | 音素拆解 + 最小对立 + 60 秒 drill |
| M6 记忆法 | 「记不住」 | 三个钩子（音 / 形 / 场景） |
| M7 日常练习 | 「每天 20 分钟怎么练」 | 五段式 + 最低版 5 分钟 |

七条硬规则里最要紧的是前两条：**每个粤语句必须带粤拼与意思**（没有粤拼的句子对学员不可复习，因为粤语汉字几乎不提示读音），以及**口語优先、書面語必须标注**（`我喜欢吃苹果` 每个字都对，但没人这样讲）。

「一次最多 7 个新项」「每轮最多 3 个纠正」这两条来自工作记忆与挫败感的实测——不是风格偏好，超了就是白教。

## 怎么用

**方式一：当作流程手册**（推荐）

直接读 `code-discipline/SKILL.md`，按当前处境选模式，再翻对应的 `references/0X-*.md`。

```bash
cd code-discipline

# 模式 1：勘察，省掉翻目录的时间
python scripts/survey.py --root ../myproj

# 模式 7：结构化结论 → 单文件 HTML，直接在浏览器打开
python scripts/build_report.py --spec examples/review-demo.json --out out/review.html --open
```

```bash
cd cantonese-coach

# 粤拼校验：批量输出前必跑，混入普通話拼音／漏声调／漢字数≠音节数都会报错
python scripts/check_jyutping.py --text "ngo5 sik6 gan2 faan6"
python scripts/check_jyutping.py --json specs/deck-core-100.json

# 4 周计划（spec → plan.md + schedule.csv）
python scripts/build_plan.py --spec specs/travel-beginner.json --outdir out/plan

# 100 词 → 分层词表 / Anki CSV / 默写纸
python scripts/vocab_deck.py --deck specs/deck-core-100.json --outdir out/deck
```

**方式二：装进 WorkBuddy 技能目录**

```bash
# Windows (Git Bash)
ln -s "$(pwd)/code-discipline" ~/.workbuddy/skills/code-discipline
ln -s "$(pwd)/cantonese-coach" ~/.workbuddy/skills/cantonese-coach
# 或直接复制
cp -r code-discipline ~/.workbuddy/skills/
cp -r cantonese-coach ~/.workbuddy/skills/
```

装好后：

- `code-discipline`：说「先别改，看看这个项目怎么实现的」「用最小改动做」「先写个测试复现」「重构前先补测试」「做个对抗性测试」「审查这次提交」「不要 Markdown，做成 HTML」都会命中。
- `cantonese-coach`：说「学粤语」「广东话」「帮我定个粤语 4 周计划」「粤拼怎么读」「练一段点餐对话」「粤语语气助词」都会命中。

## 约定

```
<skill-name>/
├── SKILL.md          # 模式索引 + 通用约束 + 硬门（frontmatter 必填 name/description）
├── references/       # 每个模式一篇：判据、清单、反模式
├── scripts/          # 可独立运行的脚本，零第三方依赖
├── specs/            # 结构化输入（JSON），脚本的入口参数
├── tests/            # 脚本的回归测试，`python tests/test_scripts.py` 直接跑
├── assets/           # 本地素材（音档、图片不入库，只保留 README.txt）
├── examples/         # 一个完整可复现的案例（含成品 HTML / 完整对话）
└── out/              # 产物，已在 .gitignore 里忽略
```

`references/` 里的文档不是「背景知识」，是**判据库**——每一条都应该能被用来做「这一步过了没有」的判断。写不出判据的段落删掉。
