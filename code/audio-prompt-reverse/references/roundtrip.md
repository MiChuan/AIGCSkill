# 判据库：往返验证

**没有比对结果，就不许说「提示词写好了」。** 这是本 skill 唯一的硬性验收。

## 流程

```bash
# 1. 量原素材
python scripts/analyze_audio.py assets/target.wav --out out/ref

# 2. 写提示词
python scripts/build_prompt.py out/ref/features.json --spec specs/my.json --outdir out/prompt

# 3. 用 out/prompt/targets/ 里对应目标的那份去生成

# 4. 量生成结果
python scripts/analyze_audio.py generated.wav --out out/new

# 5. 打分
python scripts/compare_features.py out/ref/features.json out/new/features.json --gate 80
```

`--gate 80` 会让脚本在分数不够时返回退出码 1，可以直接当卡口用。

## 分数怎么读

| 分数 | 判定 | 该做什么 |
|---|---|---|
| ≥ 80 | PASS | 可以交付。把这份 prompt 存档，下次复用 |
| 60–79 | ITERATE | 骨架对了，按报告里「优先修这几项」改对应槽位，重生成一次 |
| < 60 | MISMATCH | prompt 没说到点上。先回去填「只能你答的问题」，别硬调参数 |

## 为什么加权平均会把分数抬高：否决机制

加权平均有个通病：十几个次要维度全绿，能把「速度完全错了」稀释成 86 分的好成绩。
所以 `CRITICAL = {tempo_bpm, centroid_hz, onset_rate, voiced_ratio}` 这四个维度不接受稀释——
任何一个得分 < 0.5，总分直接压到 59 并判 MISMATCH，同时把硬伤写进 `caps` 字段。

这四个是「一听就知道不是同一条」的量，没有商量余地。

两边都测不到节拍时（`tempo_bpm` 都是 0），速度维度视为无从比较，不参与否决——
否则两段环境音会因为都没有速度而被判 MISMATCH。

## 比例量在八度尺度上比

频谱质心、
 起音密度、时长、尾音这类比例量，误差算的是 `abs(log2(new/ref))`，单位是**八度**，
不是百分比。用「两边比值离 1 有多远」会把「亮 2.7 倍」压成「差 62.5%」，看着还能接受，
实际一听就是两条完全不同的素材。

| 维度 | 容差（八度） | 折算倍数 |
|---|---|---|
| tempo_bpm | 0.10 | ±7% |
| centroid_hz | 0.50 | ±41% |
| onset_rate | 0.60 | ±52% |
| reverb_tail_ms | 1.00 | 2 倍内 |
| duration_s | 0.40 | ±32% |
| low_end_ratio | 0.60 | ±52% |

## 分维度出问题该改哪个槽位

| 维度不达标 | 改第几槽 | 具体怎么改 |
|---|---|---|
| `tempo_bpm` | 4 | 换另一个八度解（`tempo_alt_bpm`）；确认到底是 half-time 还是 double-time 的听感 |
| `centroid_hz` | 5 | 亮度词换一档；有 `centroid_slope` 时补「滤波器逐渐打开」 |
| `flatness` | 5 | 谐波↔噪声的方向反了，检查是不是误判成 noisy |
| `onset_rate` | 4 / 7 | 律动密度：加花多还是每种乐器轮着来 |
| `voiced_ratio` | 6 | 人声有无；有则补语种 |
| `lra_db` / `crest_db` | 8 | 动态：要不要加「大动态对比 / 紧压」 |
| `rms_dbfs` | 8 | 响度：别把新素材压到和原素材一样响，看的是听感比例 |
| `reverb_tail_ms` | 8 | 空间词：dry / room / hall 换一档 |
| `stereo_corr` | 8 | 声场宽度 |
| `duration_s` | 7 | 时长与段落结构 |
| `key` | 2 | 调性色彩（权重最低，先看别的维度） |

## 三轮不涨怎么办

同一首改三轮、分数没有明显变化（涨幅 < 5 分），说明**这段素材当前模型写不出来**，
继续调参数是浪费时间。这时候回头做这三件事之一：

1. 回去填 spec ——「只能你答的问题」里每一条都可能撬动一大片维度；
2. 怀疑类型判定：跳过 `genre_bank` 换一个完全不同的类型试试；
3. 承认这段素材不在目标模型的能力范围内，换模型或换参考对象。

把这条写进来是因为「再调一遍肯定能成」是最常见的失败模式：人会一直调到第 8 轮。
