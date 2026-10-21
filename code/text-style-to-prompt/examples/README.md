# 端到端示例：跑一遍完整流程

这个目录里的两个文件配合 `specs/demo-cold-tech.json` 跑通从文本到验收的全流程。

| 文件 | 角色 |
| --- | --- |
| `sample-input.md` | 原文本：817 字冷峻风格的技术评论「良率不上发布会」 |
| `sample-repro.md` | 复刻文本：664 字，按 spec 规格写的同风格不同主题「电池不看标称」 |

## 复现四步

从 skill 根目录跑：

```bash
# 1. 探针：原文本 → 风格指纹
python scripts/style_probe.py --text examples/sample-input.md \
    --out out/demo/ref.json --summary

# 2. 校验 + 编译：spec → 三档提示词
python scripts/build_prompt.py --spec specs/demo-cold-tech.json --check
python scripts/build_prompt.py --spec specs/demo-cold-tech.json --outdir out/demo

# 3. 探针：复刻文本 → 风格指纹
python scripts/style_probe.py --text examples/sample-repro.md \
    --out out/demo/cand.json --summary

# 4. 复刻验收：两份指纹对比
python scripts/style_compare.py --ref out/demo/ref.json --cand out/demo/cand.json
echo $?    # 0 过线
```

`out/demo/` 下的全部产物：

```
ref.json              原文本指纹（量化口径）
cand.json             复刻文本指纹
cmp.json              七轴偏差详情 + 总分
prompt.json           规格本身（被校验的对象）
prompt-short.txt      ≤ 200 字短版，塞 system prompt
prompt-standard.md    七段式主版，不含原文句子
prompt-anchored.md    主版 + 1–3 句语感锚点
```

## 实测结果

```
### 复刻验收：85.7 / 100  PASS

| 轴 | 权重 | 得分 | 最偏的三项 |
| --- | --- | --- | --- |
| 句法节奏 | 0.22 | 90.8 | 短句率 0.27→0.20；句长波动 5.29→4.83；句均长 13.18→12.3 |
| 段落推进 | 0.16 | 93.3 | 单句成段率 0.125→0.143；段均字数 34.0→31.6 |
| 标点习惯 | 0.16 | 95.5 | 逗号/千字 45.29→60.24；句号/千字 74.66→79.82 |
| 人称姿态 | 0.10 | 60.9 | 第三人称 8.57→0；第二人称 4.9→3.01；第一人称 3.67→3.01 |
| 用词密度 | 0.20 | 68.0 | 西文 13.46→0；数字 2.45→4.52；
| 引语占比 | 0.06 | 100.0 | 都很接近 |
| 版面形态 | 0.10 | 98.6 | 行均长 34.0→31.6 |
```

**最偏的两轴恰好说明为什么需要这套流程**：

- 人称姿态 60.9：原始文本通过「他」「有人会说」引入第三方观点，复刻文本讲电池时改用了第一人称主导——这是换主题必然带来的差异，不是 spec 没写好。
- 用词密度 68.0：原始文本带「3nm / TFLOPS」这类西文词，复刻文本几乎全中文。spec 没把「西文密度 13/千字」写成硬指标，是后续可以补的轴。

总分 85.7 已经过线（默认 75/55 阈值），说明 spec 写得对、复刻像。

## 拿新的文本跑一遍

```bash
# 1. 替换 sample-input.md 为你的文本（保持 UTF-8）
python scripts/style_probe.py --text 你的文本.md --out out/mine/ref.json --summary

# 2. 照 specs/demo-cold-tech.json 的格式写你的 spec
#    每条 rule 必带数字或对照，每条 evidence 必须原文照抄
python scripts/build_prompt.py --spec specs/你的spec.json --check

# 3. 用生成的 prompt-anchored.md 让模型写新文本，落到 你的复刻.md
python scripts/style_probe.py --text 你的复刻.md --out out/mine/cand.json --summary
python scripts/style_compare.py --ref out/mine/ref.json --cand out/mine/cand.json
```

如果复刻 < 75，看哪一轴最低，按 `references/03-replay.md` 的「没过线怎么办」改 spec。**不要去改原文本**——原文本是 ground truth。