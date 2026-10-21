# 端到端案例：黄昏沙丘剪影

一句话：**从一张演示图到 verify 打分，全程五条命令，零外部素材。**

演示图由 `make_demo.py` 合成：右上太阳、地平线亮带、左下人形剪影、轻微暗角与颗粒。
它的每一项都在 probe 的量程内可验证，适合作为「这套流水线有没有跑对」的基准。

---

## 命令序列

```bash
# 0. 合成演示原图（真实场景里换成你自己的图）
python scripts/make_demo.py --out assets/demo-ref.png --seed 7

# 1. 量测
python scripts/probe.py assets/demo-ref.png --outdir out/probe

# 2. 填 spec（参照 specs/dune-silhouette.json），装配
python scripts/build_prompt.py --spec specs/dune-silhouette.json \
    --outdir out/dune --probe out/probe/probe.json

# 3. 拿 prompt-natural.txt 去出图，存到 out/dune/raw/
#    （本案例用 seed 8 的合成图模拟「一次合格的重出」）
python scripts/make_demo.py --out out/dune/raw/gen.png --seed 8

# 4. 校验
python scripts/verify.py --ref assets/demo-ref.png \
    --gen out/dune/raw/gen.png --outdir out/dune/verify
```

---

## probe 关键读数（seed 7 演示图）

| 项 | 读数 | 说明 |
| --- | --- | --- |
| 画幅 | 3:2（1.500） | 直接进 spec |
| 光位 | 水平带状光源 | 中排整体亮于上下，是地平线发光，不是聚光 |
| 光比 | 4.3:1 中性 | 介于软光与硬光之间 |
| 反差 | 0.38 中等 | 中间调饱满 |
| 色温 | R-B 0.100 暖调 | 写「暖调、夕阳金光」 |
| 色板 | 棕 #956a46（23%）/ 暗深灰 #1d223c（17%） | hex 直接进提示词 |
| 景深 | 全景清晰（0.89） | 写「从前景到远景都在焦内」 |
| 噪声 | RMS 2.35 轻微颗粒 | 渲染图特征，不写颗粒 |
| 主体候选 | 左三分 / 下三分，面积 2.6% | 人影 + 暗沙丘连通块，位置对 |
| 文字嫌疑 | 无 | 边带梯度比均 <1.25 |

## spec 要点

人眼补的三项：主体是「独行旅人剪影」（probe 只给位置不给语义）、
动作是「静立于沙丘脊线上」、风格是「写实摄影」。
其余全部由 probe 自动填入——`--probe` 时 spec 里空着的字段会被补上，已写的不会被覆盖。

## 四档提示词（build 产出）

**short**（一句话，检索用）：

> 独行旅人剪影，静立于沙丘脊线上，黄昏时的沙漠，黄金时刻最后十分钟，水平带状光源，远景全身，35mm，写实摄影

**natural**（六段整句，给 Hy / GPT-image / 豆包 / 即梦）：见 `out/dune/prompt-natural.txt`。

**mj**（短语 + `--ar`）：见 `out/dune/prompt-mj.txt`。

**sd**（标签 + 负面）：见 `out/dune/prompt-sd.txt` 与 `out/dune/prompt-sd-negative.txt`。

## verify 两种结果

**合格的重出**（seed 8，结构与统计特征几乎一致）：100 分，全过。

**失败的重出**（调亮 22% + 偏冷 + 裁成 4:3，模拟「光写错、色温反了、画幅没认」）：
25 分，不通过。扣分项直接指向该改的字段：

| 扣分 | 项 | 怎么改 |
| --- | --- | --- |
| -20 | 画幅 | 改 composition.aspect |
| -15 | 明暗分布 | 核对 light.direction / quality |
| -15 | 色板 | 把 hex 写进提示词 |
| -15 | 景深方向 | camera.dof 写反了 |
| -5 | 色温 | 写清「暖调金光」 |
| -5 | 饱和度 | 加「自然饱和度」 |

一次改一处，重出再验。
