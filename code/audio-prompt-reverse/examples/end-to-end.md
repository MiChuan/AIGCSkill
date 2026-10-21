# 端到端案例：一段音频 → Suno 提示词

这份案例跑得起来，**不需要任何外部素材**——所有音频都是 `selftest.py` 现场合成的探针。
跟着跑一遍，重点是看每一步的「门」在哪。

## 0. 环境

```bash
cd code/audio-prompt-reverse
python scripts/selftest.py --emit out/probes -v     # 合成探针，顺便确认 94 项断言全绿
```

探针清单与已知答案（这些就是验证脚本用的 ground truth）：

| 探针 | 内容 | 已知答案 |
|---|---|---|
| `kick-120.wav` | 每 0.5 s 一记底鼓，4 s | **120 BPM**，无乐器 |
| `kick-80-hat.wav` | 80 BPM 底鼓 + 反拍镲，5 s | **80 BPM**（全频段包络会被镲骗到 160） |
| `sine-1k.wav` | 1 kHz 正弦，3 s | 质心 **1000 Hz**，平坦度低 |
| `noise.wav` | 白噪，3 s | 平坦度高，质心远高于正弦 |
| `sweep.wav` | 200→4000 Hz 扫频 | 质心斜率为**正** |
| `silence.wav` | 全零，1 s | 必须报 SilentAudio |

## 1. 测量

```bash
python scripts/analyze_audio.py out/probes/kick-120.wav --out out/demo120
```

关键读数：

```
判定类型  music (把握 0.88) ← 节拍清晰度 3.97、锁定 120 BPM
速度      120.2 BPM   节拍清晰度 3.97   起音 1.8/s
明暗      质心 147 Hz   滚降85 88 Hz
频段占比  sub 3%  bass 97%  lowmid 0%  mid 0%  highmid 0%  air 0%
人声      有声帧 0.00
标签      tempo_band=up / brightness=very_dark / texture=tonal / density=sparse
          / dynamics=wide / space=room / width=mono / low_end=overwhelming / vocal_presence=instrumental
```

**这一 step 的门**：确认 `秒数`= 4.0、`decoder` = wave、没有「6–8 kHz 为空」之外的意外警告。
这里的「6–8 kHz 几乎为空」是真的——探针就是一支底鼓，本来就没有高频内容。

## 2. 组装

```bash
python scripts/build_prompt.py out/demo120/features.json \
  --spec specs/demo-music.json --outdir out/demo120
```

得到的关键判断（完整版在 `out/demo120/prompt.md` 的追溯表里）：

| 槽位 | 措辞 | 依据 |
|---|---|---|
| 1 类型与场景 | 现场乐队摇滚 | genre:rock 命中率 0.75（tempo_bpm=120.2∈[100,150]、crest_db=15.5∈[10,40]、texture=tonal） |
| 3 配器 | 有力的底鼓 | 推断：kick(1.00) |
| 4 速度与调性 | 120 BPM（或按 60 BPM 数） | tempo_bpm=120.2 强度 0.884 清晰度 3.97 |
| 5 音色与制作 | 极暗，像被低通滤过；干净的谐波音色 | centroid_hz=147 → very_dark；flatness=0.045 → tonal |
| 6 人声 | 纯器乐，无人声 | voiced_ratio=0.0 |

注意两件事：

1. **「氛围长音 / 无节拍」没有出现**。早期版本会把它判成 ambient drone —— 5 个条件命中 4 个，
   靠软打分拿到 0.80。加了 `unless`（有明确节拍就否决）之后才干净。**这类自相矛盾的输出，
   不加否决项就一定会出现。**
2. **把握度里「配器」只有 0.35**。机器给的是候选，不是结论。用户说「我听到了钢琴」，
   用 `--spec` 传进去，它会带 `user:instruments_seen` 前缀排在推断前面。

## 3. 往返验证（故意拿错素材演示）

```bash
python scripts/analyze_audio.py out/probes/kick-80-hat.wav --out out/demo80
python scripts/compare_features.py out/demo120/features.json out/demo80/features.json --gate 80
```

结果：

```
得分 59.0 / 100 — MISMATCH — 关键维度有硬伤：
  tempo_bpm 得分只有 0.00（120 vs 80.2，1.50 倍 = 0.58 个八度，容差 0.10）
  centroid_hz 得分只有 0.00（147 vs 960，6.54 倍 = 2.71 个八度，容差 0.50）
```

换上同一首：

```bash
python scripts/analyze_audio.py out/probes/kick-120.wav --out out/demo120b
python scripts/compare_features.py out/demo120/features.json out/demo120b/features.json
# 得分 100.0 / 100 — PASS
```

**这一 step 的门**：`--gate 80` 返回退出码非 0 时，说明还不许交付。

## 4. 复用这段工作流

换成真实素材时，把 `--spec` 换成你自己的：

```json
{
  "usage": "短视频背景音乐，不能抢人声",
  "instruments_seen": ["钢琴", "贝斯", "底鼓"],
  "vocal": {"present": false},
  "negative": ["不要歌词"]
}
```

音效素材则必须自补 `source_action` 与 `usage`，因为这两样是频谱分析给不出来的：

```bash
python scripts/build_prompt.py out/sfx/features.json --kind sfx --spec specs/demo-sfx.json --outdir out/sfx
```

音效输出里第一槽会显示【待填：这是什么东西在发声 / 什么动作】，
并且进「只能你答的问题」——这是正确结果，不是脚本没算出来。
