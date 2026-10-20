# image — 图片生成类 Skill

本目录按主题收录图片方向的 AIGC skill，每个子目录是一个独立可用的 skill（含 `SKILL.md`）。

| Skill | 做什么 | 模型 / 依赖 | 状态 |
| --- | --- | --- | --- |
| [hy-image35-cinematic](hy-image35-cinematic/SKILL.md) | 参考图锁脸，把同一个人放进任意剧集场景，出无右下角水印的 8K 电影单帧 | Hy Image3.5 preview；内置出图能力；Pillow | 可用 |
| [hy-image35-lowkey-portrait](hy-image35-lowkey-portrait/SKILL.md) | 参考照片二创：伦勃朗单侧硬光 + 纯黑背景 + 身后横向拖影，锁长相与发色，3:2 / 8K 电影海报人像 | Hy Image3.5 preview；内置出图能力；Pillow | 可用 |
| [hy-image35-photo-seal](hy-image35-photo-seal/SKILL.md) | 把照片转成专属印章：提取元素翻成篆刻语言，单色朱砂印泥、边缘磨损、留白处刻主题字，圆形/方形，4K + 透明底 | Hy Image3.5 preview；内置出图能力；Pillow | 可用 |
| [hy-image35-nationalgeo](hy-image35-nationalgeo/SKILL.md) | 复刻经典纪实调色：真实、浓郁、立体、复古，风光/旅行/扫街三类；出图 + 调色 + 六轴量化体检，2K–8K | Hy Image3.5 preview；内置出图能力；Pillow | 可用 |
| [hy-image35-ink-print](hy-image35-ink-print/SKILL.md) | 基于参考原图重构平面设计图：米白哑光特种纸 + 极简几何平涂 + 毛笔淡墨晕染 + 底部居中两行英文，出图 + 版面体检 + 叠字保底 | Hy Image3.5 preview；内置出图能力；Pillow | 可用 |
| [hy-image35-kr-school-id](hy-image35-kr-school-id/SKILL.md) | 韩系学院风证件写真：锁五官把人放进受控影棚（三件套制服 / 浅灰蓝磨砂底 / 大面积柔光），精确等宽白边框 + 底部英文字排版，8K 交付 | Hy Image3.5 preview；内置出图能力；Pillow | 可用 |
| [hy-image35-hair-code](hy-image35-hair-code/SKILL.md) | 先诊断后出图的发型顾问：量出「脸型 + 最宽处 + 指宽」三位代码与发质，查表推荐 3 款，再把选中的发型画回本人照片（只换头发，锁脸、发际线、光与背景） | Hy Image3.5 preview；内置出图能力；Pillow（对比图 / 网格图） | 可用 |

七者的分工：

| | 对照片做什么 | 输出 |
| --- | --- | --- |
| `cinematic` | **人进场景**：保住这个人，换掉周围 | 电影感场景单帧 |
| `lowkey-portrait` | **人被重新打光**：保住这个人，换掉光与背景 | 纯黑背景海报人像 |
| `photo-seal` | **照片被压缩成符号**：扔掉细节，只留可辨认的骨架 | 印章 / 钤印 |
| `nationalgeo` | **照片被重新洗一遍**：内容一个不动，只换光色关系 | 纪实调色成片 |
| `ink-print` | **照片被重排成一张版面**：内容解构成几何面，承载换成纸 | 米白纸上的印刷品 |
| `kr-school-id` | **人被放进受控影棚**：脸不动，光位/服装/背景/边框/版式全部提前定死 | 带白边的证件写真 |
| `hair-code` | **先量人，再改发**：脸与发际线不动，只换发际线以上的形状 | 同一人的三款试发图 |

前两个是"保留人物、改变环境"，第三个是反过来——**必须把照片的信息量砍掉两个数量级**，否则出来就是"照片套了个圆框"。第四个与它们都不同：它不改变画面内容，改的是**同一场景被呈现成什么样**，所以它既能接在出图之后，也能直接吃你相机里的原片。第五个砍掉**一个数量级**，但它比印章多两件事：一是**换了承载**（从相纸到米白特种纸，于是没有光照、没有投影、没有纸纹），二是**多了文字与版面**（留白比例、基线、边距、字号比都是可量的）。第六个把变量反过来：环境是**给定**的（影棚、光位、服装、背景色都写死），唯一的自由度留给脸——于是它的难点全在"如何让模型不要动这张脸"，以及"边框和版式这些像素级的活儿怎么从模型手里拿回来"。第七个是唯一一个**先回答"你该剪什么"、再动手画**的：它不追求把照片变成某种风格，而是把"你适合什么"压成三个可测的数（脸型、最宽处、指宽）再加一个发质维度，查表出方案，最后只改发际线以上的形状——改动面积比前六个都小，所以它对"不许动"的要求最严。动手前先确定你要的是哪一种。

前五个共用同一套出图参数口径（`revise=false`、`footnote` 不传、`input_fidelity=high`），收尾脚本各自独立：场景图要清右下角水印，暗调人像要修 watermark 并守纯黑背景，印章图则靠几何掩膜把印面外的一切（连水印带桌面）一次性清掉，纪实调色则是唯一一个**会给成图打分**的——它的体检报告能告诉你差在哪一项。`ink-print` 也打分，但量的是另一套东西：纸色（亮度/暖度/饱和三判据）、留白率、重心、文字行数与字号比、插画到文字的间距、纸面噪声、边缘陡度——它查的是**版面塌没塌**，不是调子对不对。`kr-school-id` 的验收思路又不一样：它要的是**四边严格等宽**（从成图上量回来，四个数必须相等）和**水印修补后的边缘能量下降**（不是标准差——水印压在人物边界上时标准差本来就不会归零），这两个指标都是"对着要消除的那个东西"设计的。

## 怎么用

**方式一：当作提示词与流水线手册**（推荐）

直接读对应 skill 的 `SKILL.md`，按 Step 0–4 走。脚本都是命令行工具，可以脱离 agent 单独跑：

```bash
cd hy-image35-cinematic
python scripts/build_prompt.py --spec specs/yujian-kunlun.json --outdir out/demo
python scripts/finalize.py out/demo/raw/x.png --out out/demo/x_8k.jpg --target 8k

cd ../hy-image35-lowkey-portrait
python scripts/build_portrait_prompt.py --spec specs/lowkey-rembrandt.json --outdir out/demo
python scripts/postcheck.py out/demo/raw/x.png --out out/demo/x_8k.jpg --target 8k

cd ../hy-image35-photo-seal
python scripts/build_seal_prompt.py --spec specs/photo-seal.json --outdir out/seal
python scripts/sealcut.py out/seal/raw/x.png --shape round --out out/seal/seal_4k.png --size 4096 --crop --alpha
python scripts/stamp_text.py out/seal/raw_blank.png --text "山亭晚照" --out out/seal/seal_text.png   # 字多时的保底

cd ../hy-image35-nationalgeo
python scripts/build_ng_prompt.py --spec specs/natgeo-ganzi.json --outdir out/demo
python scripts/grade.py out/demo/raw/x.png --check                                  # 只体检，出六轴报告
python scripts/grade.py out/demo/raw/x.png --out out/demo/x_8k.jpg --preset travel --target 8k

cd ../hy-image35-ink-print
python scripts/make_demo_ref.py --out assets/demo-ref.png                            # 没素材时合成一张
python scripts/extract_palette.py assets/demo-ref.png --outdir out/pal --k 6 --top 0.62
python scripts/build_print_prompt.py --spec specs/ink-print-lake.json --outdir out/demo
python scripts/printcheck.py out/demo/raw/x.png --layout out/demo/meta.json --annotate out/demo/check.png
python scripts/settype.py out/demo/raw/x.png --title "Still Water" --desc "..." --out out/demo/typed.png
python scripts/selftest.py                                                          # 37 项断言，不用外部图

cd ../hy-image35-kr-school-id
python scripts/build_id_prompt.py --spec specs/male-glasses.json --outdir out/male
python scripts/finalize.py out/male/raw/x.png --out out/male/x_8k.jpg --target 8k --border 0.015
python scripts/settype.py out/male/x_8k.jpg --out out/male/x_final.jpg --spec specs/male-glasses.json
python scripts/selftest.py                                                          # 39 项断言，不用外部图
python out/demo/make_demo.py                                                        # 端到端演示

cd ../hy-image35-hair-code
python scripts/haircode.py --guide assets/face.jpg --out out/guide.png               # 打网格读六个位置
python scripts/haircode.py --forehead 210 --face-length 620 --temple 470 \
    --cheek 520 --jaw 470 --unit px --hair fine                                     # 代码 + 处方 + 推荐
python scripts/haircode.py --shape square --widest cheek --fingers 4 --hair hard \
    --emit-spec --style 1 --ref assets/face.jpg --out-spec specs/s1.json            # 导出第 1 款
python scripts/build_hair_prompt.py --spec specs/s1.json --outdir out/s1
python scripts/contact_sheet.py --ref assets/face.jpg --img out/s1/raw/x.png \
    --labels "参考/现状" "1 自然中分" --crop head --out out/sheet.jpg
python scripts/selftest.py                                                          # 296 项断言，不用外部图
```

**方式二：装进 WorkBuddy 技能目录**

```bash
# Windows (Git Bash)
ln -s "$(pwd)/hy-image35-cinematic" ~/.workbuddy/skills/hy-image35-cinematic
# 或直接复制
cp -r hy-image35-cinematic ~/.workbuddy/skills/
```

装好后在对话里说"用混元出图""用这张照片生成一个场景""做一张伦勃朗光人像""把这张照片做成印章""调成国家地理那种色调""把这张照片做成一张水墨留白的平面设计图""做一张韩系学院风证件照""我适合什么发型""帮我试几个发型"就会命中。

## 约定

每个 skill 目录结构：

```
<skill-name>/
├── SKILL.md          # 触发条件、流水线、验收标准（frontmatter 必填 name/description）
├── scripts/          # 可独立运行的脚本，优先零依赖或单一轻依赖
├── references/       # 原理与词库，随用随查，不塞进 SKILL.md
├── examples/         # 一个完整可复现的案例
├── specs/            # 结构化输入（JSON）
├── data/             # 结构化知识表（可选，如 hair-code 的 9×3 款式矩阵）
├── assets/           # 本地参考图（图片不入库，只保留 README.txt）
└── out/              # 产物（已在 .gitignore 里忽略）
```
