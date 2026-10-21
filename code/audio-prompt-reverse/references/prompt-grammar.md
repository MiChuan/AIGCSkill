# 判据库：提示词语法与槽位

## 产出物对照表

不同模型吃的东西不一样，所以产物里同时给了长、短两份，并在 `targets/` 下按目标分开：

| 用途 | 用哪个文件 | 原因 |
|---|---|---|
| Suno / Udio 的 style、tags 字段 | `targets/suno.txt`、`targets/udio.txt` | 这类字段吃短语堆砌，上限约 480 字符，塞整句会被截断 |
| Stable Audio | `targets/stable-audio.txt` | 吃整句描述，负向词要单独填进 negative prompt 字段 |
| ElevenLabs 音效 | `targets/elevenlabs-sfx.json` | 吃单一事件的画面化描述，并要求给出 `duration_seconds` |
| 其他 / 人工再加工 | `prompt.md` 的长提示词 | 中英双语，给后续修改用 |

不要把长提示词直接粘进 Suno 的 style 字段：超过上限会被截断成语义破碎的词串。
要用就直接用 `targets/` 下分好的那一份。

## 音乐：八槽

| # | 槽位 | 内容 | 依据来源 |
|---|---|---|---|
| 1 | scene_genre | 类型 / 场景 | `genre_bank` 最高命中的规则 |
| 2 | mood | 情绪 | `mood_bank` + 调性色彩 |
| 3 | instrumentation | 配器 | 用户填的优先，其后才是 `instrument_bank` 候选 |
| 4 | rhythm_key | 速度 / 调性 / 律动 | `tempo_bpm` + `tempo_alt_bpm` + `key` + `onset_rate` |
| 5 | timbre | 音色与制作 | `centroid_hz` + `flatness` |
| 6 | vocal | 人声 | `voiced_ratio` + 用户确认 |
| 7 | structure | 结构与时值 | `duration_s` + 用户用途 |
| 8 | mix | 混音与母带 | `stereo_corr` + `reverb_tail_ms` + 频段占比 + `rms_dbfs` |

顺序不能打乱：多数模型对 prompt 头部的权重更高，第 1、2 槽放的是最确定的东西；
参数类描述最弱，放最后当修饰。

## 音效：八槽

顺序是 `source_action`（**必须由人填**）→ `material`（机器候选）→ `envelope`（起振 + 衰减，
机器最准的一槽）→ `spectral`（亮度 + 纹理 + 频段重心）→ `space` → `duration` →
`perspective`（收音远近）→ `use_case`（人填）。

机器在音效上最有用的是 `envelope` 和 `spectral`：这两个是波形直接可测的。
`source_action` 和 `use_case` 永远填不出来——看到质心 2 kHz、衰减 300 ms，
没法区分那是「抽屉关上」还是「木鱼敲了一下」。这两槽会显示【待填】并进待答问题，不许替用户编。

## 环境声 / 人声

- **环境声**：空间、声源构成、远近、时间天气、质感、动静、可否循环、混音。
  空间主体同样是机器给不了的，显示【待填】。
- **人声**：音色（人填）、音区（机器给）、表达方式、语速、情绪、录制质地、时长、用途。
  「语种 / 年龄感 / 音色形容词」一律待填——这是语音合成里最贵的一维，猜错了整段报废。

## 负向提示：八条封顶

负向词不是写得越多越保险：堆到十几条时，模型会为了满足「不要 X」而牺牲真正重要的特征，
而且你无法判断到底哪条在起作用。所以宁砍不堆。

| 触发条件 | 加入的负向词 |
|---|---|
| 总是 | 低中频浑浊、高频刺耳发脆、削波失真 |
| `vocal_presence = instrumental` | 人声、合唱、人声切片、歌词、阿卡贝拉 |
| brightness 为 dark / very_dark | 尖锐高频、齿音、金属光泽 |
| space = dry | 大混响、教堂残响、过度湿声 |
| density = sparse | 密集琶音、连续加花、音墙 |
| low_end 为 heavy / overwhelming | 低频薄弱、缺超低频 |
| texture = tonal | 过于干净的正弦音色、毫无噪声成分 |
| dynamics = compressed | 管弦式大动态、剧烈起伏 |
| kind = sfx | 音乐、旋律、节拍、节奏感 |
| 用户 spec 的 `negative` | 原样加入并排在前面，但不能突破八条上限 |

## 参数怎么接

`params` 里给了 `bpm`、`bpm_alt`、`key`、`duration_s`、`reference_rms_dbfs`、`suggested_ceiling_dbfs`：

- **BPM 两个都给**：`tempo_bpm` 与 `tempo_alt_bpm` 是同一个周期的两种数法，交给模型自己去挑。
- **`key` 只在置信 ≥ 0.55 时给出**，否则是 null。瞎写一个 key 会把模型锁在错误的调上，比不写更糟。
- **响度参数只用于留 headroom**：`rms_dbfs` 是未加权的 RMS，不能当 LUFS 用。
  目标不是「把新素材压得和原素材一样响」，而是「生成时留 -1 dBFS 的天花板」。
