#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""纪实调色：把一张图的影调与色彩拉向经典纪实摄影，并给出可量化的体检报告。

两个能力，共用一个色彩模型：

  体检（--check）
      不产图，只量六件事：暗部是否压死、高光是否曝死、中间调对比够不够、
      饱和度是否过头、是否有整体色偏、蓝/绿/大地色的色相是否跑偏。
      「浓郁」和「虚假」的区别就在这几个数上，眼睛会骗人，直方图不会。

  调色（默认）
      白平衡归正 → 影调曲线（先立动态范围，再给中间调对比）→ 分色饱和度
      → 肤色保护 → 局部对比 → 颗粒。纯 Pillow 实现，8K 也能跑。

依赖 Pillow（pip install pillow）。

用法：
    python grade.py in.jpg --out out.jpg --preset travel --check
    python grade.py in.jpg --out out_8k.jpg --preset nature --target 8k
    python grade.py in.jpg --out out.jpg --strength 0.6 --clarity 30
    python grade.py in.jpg --out out.jpg --preset neutral     # 只做白平衡与导出

原理与判据见 references/grade.md 与 references/color-science.md。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

try:
    from PIL import Image, ImageChops, ImageFilter, ImageStat
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "[deps] 缺少 Pillow。安装：\n"
        "  python -m pip install pillow\n"
    )
    raise SystemExit(2)

SIZES = {"2k": 2048, "4k": 3840, "8k": 7680}

# ---------------------------------------------------------------- 预设

# 数值口径见 references/color-science.md。
# shadow_lift / white_level / knee / mid 四个数决定影调，其余决定分色。
# skin 是「肤色保留原饱和度的比例」：1.0 = 完全不参与调色，0.85 = 保留 85%。
PRESETS = {
    "nature": {
        "label": "自然风光",
        "shadow_lift": 12.0, "white_level": 249, "knee": 0.70, "mid": 1.18,
        "vibrance": 1.16, "sat_sky": 1.10, "sat_green": 1.10, "sat_earth": 1.06,
        "skin": 0.90, "clarity": 24, "grain": 0.0, "vintage": 0.25,
    },
    "street": {
        "label": "人文扫街",
        "shadow_lift": 9.0, "white_level": 247, "knee": 0.68, "mid": 1.24,
        "vibrance": 1.10, "sat_sky": 1.04, "sat_green": 1.05, "sat_earth": 1.04,
        "skin": 0.85, "clarity": 30, "grain": 0.06, "vintage": 0.35,
    },
    "travel": {
        "label": "旅行纪实",
        "shadow_lift": 11.0, "white_level": 248, "knee": 0.70, "mid": 1.20,
        "vibrance": 1.14, "sat_sky": 1.09, "sat_green": 1.08, "sat_earth": 1.06,
        "skin": 0.88, "clarity": 26, "grain": 0.04, "vintage": 0.30,
    },
    "neutral": {
        "label": "仅归正（不过风格）",
        "shadow_lift": 4.0, "white_level": 253, "knee": 0.80, "mid": 1.04,
        "vibrance": 1.02, "sat_sky": 1.0, "sat_green": 1.0, "sat_earth": 1.0,
        "skin": 1.0, "clarity": 8, "grain": 0.0, "vintage": 0.0,
    },
}

# 分色作用的色相区间（度）与作用门限
BANDS = {
    "sky": (196, 224, "天空"),
    "green": (72, 148, "植被"),
    "earth": (14, 46, "大地"),
}

# 分析抽样尺寸：颜色统计在这个尺寸上做，够准且够快
ANALYZE_SIDE = 1400


# ---------------------------------------------------------------- 基础工具

def clamp8(v: float) -> int:
    return 0 if v <= 0 else 255 if v >= 255 else int(round(v))


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def scale_preset(p: dict, strength: float) -> dict:
    """按强度把预设往「不调色」方向插值。strength=1 为原预设。"""
    neutral = PRESETS["neutral"]
    out = dict(p)
    for k in ("vibrance", "sat_sky", "sat_green", "sat_earth", "skin"):
        out[k] = lerp(1.0, float(p[k]), strength)
    for k in ("shadow_lift", "clarity", "grain", "vintage"):
        out[k] = lerp(0.0, float(p[k]), strength)
    return out


# ---------------------------------------------------------------- 白平衡

def auto_wb(img: Image.Image) -> tuple[tuple[float, float, float], dict]:
    """灰世界白平衡：只统计近中性像素，并死死夹在 ±4% 以内。

    这条不是默认开的，原因写在 references/color-science.md：
    风光片里草地与蓝天各占三分之一，它们的「非中性」会把灰世界拉偏，
    结果是把暖色的土路推成紫色。拍摄时白平衡已经对了的片子，再猜一次只会更差。
    """
    small = img.copy()
    small.thumbnail((480, 480), Image.LANCZOS)
    hsv = small.convert("HSV")
    _, s, v = hsv.split()

    # S < 40 且 30 < V < 245：近中性的中亮部
    ms = s.point([255 if i < 40 else 0 for i in range(256)])
    mv = v.point([255 if 30 < i < 245 else 0 for i in range(256)])
    mask = ImageChops.multiply(ms, mv)

    ratio = ImageStat.Stat(mask).mean[0] / 255.0
    if ratio < 0.06:
        return (1.0, 1.0, 1.0), {"applied": False, "reason": f"近中性像素只占 {ratio:.1%}，判不准"}

    stat = ImageStat.Stat(small.convert("RGB"), mask)
    r, g, b = stat.mean
    gray = (r + g + b) / 3.0
    gains, clipped = [], []
    for c in (r, g, b):
        g_ = gray / c if c > 1e-6 else 1.0
        gc = min(1.04, max(0.96, g_))
        if abs(gc - g_) > 1e-3:
            clipped.append(round(g_, 3))
        gains.append(gc)
    return tuple(gains), {
        "applied": any(abs(x - 1.0) > 0.003 for x in gains),
        "neutral_ratio": round(ratio, 4),
        "neutral_mean": [round(x, 1) for x in (r, g, b)],
        "gains": [round(x, 4) for x in gains],
        "clamped": clipped,
    }


def apply_gains(img: Image.Image, gains: tuple[float, float, float]) -> Image.Image:
    if all(abs(g - 1.0) < 0.002 for g in gains):
        return img
    chans = img.split()
    out = []
    for ch, g in zip(chans, gains):
        out.append(ch.point([clamp8(v * g) for v in range(256)]))
    return Image.merge("RGB", out)


# ---------------------------------------------------------------- 黑白场归位

LAUNCH_BLACK = 8.0
LAUNCH_WHITE = 248.0
LAUNCH_FULL = 215.0   # 色阶跨度超过这个值就算「已经铺满」，不再拉伸
LAUNCH_MAX_GAIN = 2.5 # 拉伸倍数上限：再高就是硬拉，出来的不是照片是色块


def auto_levels(img: Image.Image) -> tuple[Image.Image, dict]:
    """把被压缩的色阶拉回全幅。

    这是整条链的第一道工序，也是最容易被跳过的一道。
    雾化图（假 HDR、网红滤镜、隔着车窗拍的）的色阶常常只占 117–162 这 45 级，
    在这种图上加对比，只是在 45 级里加对比——灰还是灰。
    必须先把它拉回 8–248，后面所有影调才有效。

    只拉伸不压缩；拉伸倍数上限 4 倍，避免把低反差的雾天、雪景拉出断层。
    """
    hist = img.convert("L").histogram()
    pc = _percentiles(hist, (1, 99))
    lo, hi = pc["p1"], pc["p99"]
    span = hi - lo

    if span >= LAUNCH_FULL:
        return img, {"applied": False, "range": [lo, hi], "span": span,
                     "reason": "色阶已铺满，无需归位"}

    gain = min(LAUNCH_MAX_GAIN, (LAUNCH_WHITE - LAUNCH_BLACK) / max(1.0, span))
    lut = [clamp8((i - lo) * gain + LAUNCH_BLACK) for i in range(256)]
    out = Image.merge("RGB", [ch.point(lut) for ch in img.split()])
    return out, {
        "applied": True,
        "range": [lo, hi],
        "span": span,
        "gain": round(gain, 3),
        "capped": gain >= LAUNCH_MAX_GAIN and (LAUNCH_WHITE - LAUNCH_BLACK) / max(1.0, span) > LAUNCH_MAX_GAIN,
    }


# ---------------------------------------------------------------- 影调曲线

def tone_lut(p: dict, channel: int) -> list[int]:
    """单通道影调曲线。

    顺序刻意如此：
      1) 中间调对比：窗口 4x(1-x) 在两端归零，所以加对比不动黑也不动白
      2) 黑位抬升：(1-y)^3 只在暗部起效，指数衰减保证不影响中间调
      3) 白位压缩：knee 以上线性收进 white_level，让 98% 落在 ~248 而不是 255
      4) 复古分离：阴影略冷、高光略暖，强度极小（通道间差 < 3/255）
    """
    mid = float(p["mid"])
    lift = float(p["shadow_lift"]) / 255.0
    knee = float(p["knee"])
    wp = float(p["white_level"]) / 255.0
    vintage = float(p["vintage"])

    lut = []
    for i in range(256):
        x = i / 255.0
        w = 4.0 * x * (1.0 - x)
        y = x + (mid - 1.0) * (x - 0.5) * w
        y = y + lift * (1.0 - y) ** 3
        if y > knee:
            y = knee + (y - knee) * (wp - knee) / (1.0 - knee)
        if vintage:
            if channel == 0:      # R：高光暖一点，暗部收一点
                y += vintage * (0.035 * x * x - 0.012 * (1 - x) ** 2)
            elif channel == 2:    # B：暗部冷一点，高光退一点
                y += vintage * (0.035 * (1 - x) ** 2 - 0.018 * x * x)
        lut.append(clamp8(y * 255.0))
    return lut


def apply_tone(img: Image.Image, p: dict) -> Image.Image:
    chans = img.split()
    return Image.merge("RGB", [ch.point(tone_lut(p, i)) for i, ch in enumerate(chans)])


# ---------------------------------------------------------------- 分色饱和度

def vib_lut(gain: float) -> list[int]:
    """中间饱和度的像素提得多、已经很高的提得少——这就是 vibrance 与 saturation 的区别。"""
    out = []
    for i in range(256):
        s = i / 255.0
        s2 = s + (gain - 1.0) * s * (1.0 - s) * 2.0
        out.append(clamp8(min(1.0, s2) * 255.0))
    return out


def band_weight_lut(lo: float, hi: float, feather: float = 7.0) -> list[int]:
    lo %= 360.0
    hi %= 360.0
    out = []
    for i in range(256):
        deg = i * 360.0 / 256.0
        if lo <= hi:
            inside = lo <= deg <= hi
            d = min(abs(deg - lo), abs(deg - hi))
        else:
            inside = deg >= lo or deg <= hi
            d = min(abs(deg - lo), abs(deg - hi))
        if inside:
            out.append(255)
        elif feather <= 0:
            out.append(0)
        else:
            out.append(clamp8(max(0.0, 1.0 - d / feather) * 255.0))
    return out


def range_lut(lo: int, hi: int, feather: int = 8) -> list[int]:
    """对 0–255 的通道值做区间权重（用于 S / V 门限）。"""
    out = []
    for i in range(256):
        if lo <= i <= hi:
            out.append(255)
        elif feather <= 0:
            out.append(0)
        else:
            d = lo - i if i < lo else i - hi
            out.append(clamp8(max(0.0, 1.0 - d / feather) * 255.0))
    return out


def apply_color(img: Image.Image, p: dict) -> Image.Image:
    """一次性完成色相/饱和度/明度的全部工作，只做一次 RGB↔HSV 往返。

    肤色的处理是这里最要紧的一步：土墙、岩石、皮肤在色相上几乎重叠（20°–42°），
    所以不能「先给大地色加饱和，再给皮肤减一点」——那样脸一定跟着走。
    正确做法是把肤色区域从分色掩膜里剔出去，再让它按比例回到原饱和度。
    """
    hsv = img.convert("HSV")
    h, s, v = hsv.split()
    s_orig = s.copy()

    # 只对「有颜色、且不是过暗过亮」的像素做分色，避免把阴影噪声一起推上去
    gate = ImageChops.multiply(
        s.point(range_lut(18, 255, 8)), v.point(range_lut(26, 244, 12))
    )

    # 肤色区域：暖色相 + 低到中饱和 + 中明度
    skin_mask = ImageChops.multiply(
        ImageChops.multiply(h.point(band_weight_lut(12, 42, 8)), s.point(range_lut(0, 150, 20))),
        v.point(range_lut(76, 236, 16)),
    )
    not_skin = ImageChops.invert(skin_mask)

    s = s.point(vib_lut(float(p["vibrance"])))

    for key, (lo, hi, _label) in BANDS.items():
        gain = float(p[f"sat_{key}"])
        if abs(gain - 1.0) < 0.005:
            continue
        mask = ImageChops.multiply(
            ImageChops.multiply(h.point(band_weight_lut(lo, hi)), gate), not_skin
        )
        if ImageStat.Stat(mask).mean[0] < 1.0:
            continue
        s = Image.composite(s.point(vib_lut(gain)), s, mask)

    # 肤色按比例保留原饱和度：skin = 1 表示完全不参与调色，0.85 表示保留 85%
    prot = max(0.0, min(1.0, float(p["skin"])))
    if prot > 0.01:
        s = Image.composite(s_orig, s, skin_mask.point([int(i * prot) for i in range(256)]))

    v = add_clarity(v, float(p["clarity"]))
    v = add_grain(v, float(p["grain"]))

    return Image.merge("HSV", (h, s, v)).convert("RGB")


# ---------------------------------------------------------------- 局部对比与颗粒

def add_clarity(v: Image.Image, amount: float) -> Image.Image:
    """明度通道的大半径局部对比：半径够大才是「光影层次」，半径小就成锐化了。"""
    if amount <= 0.5:
        return v
    radius = max(3.0, max(v.size) / 620.0)
    return v.filter(ImageFilter.UnsharpMask(radius=radius, percent=int(round(amount)), threshold=3))


def add_grain(v: Image.Image, amount: float) -> Image.Image:
    """极细颗粒：目标标准差 = amount × 40 级。effect_noise 的 sigma 是 64，所以要换算。"""
    if amount <= 0.001:
        return v
    factor = amount * 40.0 / 64.0
    noise = Image.effect_noise(v.size, 64.0).point(
        [clamp8((i - 128) * factor + 128) for i in range(256)]
    )
    return ImageChops.add(v, noise, 1.0, -128)


# ---------------------------------------------------------------- 整链

def apply_pipeline(img: Image.Image, p: dict, wb: tuple[float, float, float] = (1.0, 1.0, 1.0),
                   levels: str = "auto") -> tuple[Image.Image, dict]:
    """白平衡 → 色阶归位 → 影调曲线 → 分色 → 局部对比 + 颗粒。

    顺序是硬约束，理由见 references/grade.md：
    色阶归位必须在影调曲线之前，否则是在被压缩的区间里加对比；
    分色必须在色调之后，否则色相锚点会跟着曲线漂。
    """
    info: dict = {}
    p = dict(p)

    cur = apply_gains(img, wb)
    if levels == "auto":
        cur, lv = auto_levels(cur)
    else:
        lv = {"applied": False, "reason": "关闭"}
    info["levels"] = lv

    if lv.get("applied"):
        # 拉色阶本身就在提饱和，按拉伸倍数把饱和加成收回来，避免两道加法叠成溢出
        k = 1.0 / max(1.0, float(lv.get("gain", 1.0))) ** 0.6
        for key in ("vibrance", "sat_sky", "sat_green", "sat_earth"):
            p[key] = 1.0 + (float(p[key]) - 1.0) * k
        info["sat_compensation"] = round(k, 3)

    cur = apply_tone(cur, p)
    cur = apply_color(cur, p)
    info["params_used"] = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in p.items()}
    return cur, info


# ---------------------------------------------------------------- 体检

def _percentiles(hist: list[int], points=(1, 5, 10, 25, 50, 75, 90, 95, 99)) -> dict:
    total = sum(hist) or 1
    out = {}
    acc, idx = 0, 0
    for p in points:
        need = total * p / 100.0
        while idx < 256:
            acc += hist[idx]
            if acc >= need:
                break
            idx += 1
        out[f"p{p}"] = min(255, idx)
    return out


def _circular_mean(degs: list[float]) -> float | None:
    if not degs:
        return None
    sx = sum(math.sin(math.radians(d)) for d in degs)
    sy = sum(math.cos(math.radians(d)) for d in degs)
    if abs(sx) < 1e-9 and abs(sy) < 1e-9:
        return None
    ang = math.degrees(math.atan2(sx, sy)) % 360.0
    return round(ang, 1)


def analyze(img: Image.Image) -> dict:
    rgb = img.convert("RGB")
    lum = rgb.convert("L")
    hist = lum.histogram()
    total = sum(hist) or 1
    pc = _percentiles(hist)

    small = rgb.copy()
    small.thumbnail((ANALYZE_SIDE, ANALYZE_SIDE), Image.LANCZOS)
    W, H = small.size

    # 暗部细节：取最暗的那 20% 区块，看它们内部还有没有结构。
    # 一片死黑 → std ≈ 0；有地形、衣褶、墙面的走向 → std 明显 > 0。
    tw = max(16, W // 40)
    tiles: list[tuple[float, float]] = []
    for y in range(0, H, tw):
        for x in range(0, W, tw):
            tile = lum.crop((x, y, min(x + tw, W), min(y + tw, H)))
            st = ImageStat.Stat(tile)
            tiles.append((st.mean[0], st.stddev[0]))
    tiles.sort(key=lambda t: t[0])
    n_shadow = max(8, len(tiles) // 5)
    darkest = tiles[:n_shadow]
    shadow_mean = sum(m for m, _ in darkest) / len(darkest)
    shadow_open = round(sum(s for _, s in darkest) / len(darkest), 2)

    # 颜色统计：逐像素
    px = small.load()
    sat_sum, sat_n = 0.0, 0
    neut: list[tuple[float, int, int]] = []
    buckets: dict[str, list[float]] = {k: [] for k in BANDS}
    skin_degs: list[float] = []
    for y in range(0, H, 2):
        for x in range(0, W, 2):
            r, g, b = px[x, y]
            mx, mn = max(r, g, b), min(r, g, b)
            yy = 0.299 * r + 0.587 * g + 0.114 * b
            if mx == mn:
                continue
            s = (mx - mn) / mx
            if 20 < yy < 235:
                sat_sum += s
                sat_n += 1
                neut.append((s, r - g, b - g))
            if s < 0.20 or yy < 30:
                continue
            deg = (60.0 * ((g - b) / (mx - mn))) if mx == r else \
                  (60.0 * (2.0 + (b - r) / (mx - mn))) if mx == g else \
                  (60.0 * (4.0 + (r - g) / (mx - mn)))
            deg %= 360.0
            # 肤色是「暖区间 + 低饱和 + 中明度」的子集，与大地色统计重叠，两个都记
            if 12 <= deg <= 42 and 0.14 < s < 0.55 and 76 < yy < 232:
                skin_degs.append(deg)
            for key, (lo, hi, _l) in BANDS.items():
                if lo <= deg <= hi:
                    buckets[key].append(deg)
                    break

    # 色偏：取「饱和度最低的 10% 像素」的中位数。
    # 这只是个参考值，不是判决——没有参考白的情况下，色偏检测会被自己骗：
    # 给画面加 +18 的蓝，原本 S=0.29 的土路会「变中性」被选进来，
    # 而原本 S=0.15 的远山被推到 0.27 又被踢出去，选样自己换了一批人。
    # 所以同时给一个可信度（这批像素到底中不中性），不可信就 skip，绝不硬判。
    cast_rg = cast_bg = None
    cast_conf = None
    if len(neut) >= 200:
        neut.sort(key=lambda t: t[0])
        k = max(1, len(neut) // 10)
        low = neut[:k]
        rg = sorted(t[1] for t in low)
        bg = sorted(t[2] for t in low)
        ss = sorted(t[0] for t in low)
        cast_rg = round(rg[len(rg) // 2], 2)
        cast_bg = round(bg[len(bg) // 2], 2)
        cast_conf = round(ss[len(ss) // 2], 3)

    out = {
        "size": list(rgb.size),
        "tone": {
            **{k: v for k, v in pc.items()},
            "contrast_p90_p10": pc["p90"] - pc["p10"],
            "crushed_ratio": round(sum(hist[:4]) / total, 4),
            "clipped_ratio": round(sum(hist[254:]) / total, 4),
            "shadow_open": shadow_open,
            "shadow_luma": round(shadow_mean, 1),
        },
        "color": {
            "sat_mean": round(sat_sum / sat_n, 4) if sat_n else None,
            "cast_rg": cast_rg,
            "cast_bg": cast_bg,
            "cast_conf": cast_conf,
            "hue_sky": _circular_mean(buckets["sky"]),
            "hue_green": _circular_mean(buckets["green"]),
            "hue_earth": _circular_mean(buckets["earth"]),
            "hue_skin": _circular_mean(skin_degs),
            "pixels_sky": len(buckets["sky"]),
            "pixels_green": len(buckets["green"]),
            "pixels_earth": len(buckets["earth"]),
            "pixels_skin": len(skin_degs),
        },
    }
    return out


# 判据：name, 取值路径, 下限, 上限。越界只是 warn——单看一个数不足以判死。
CHECKS = [
    ("contrast_p90_p10", "tone", 95, 205, None),
    ("crushed_ratio", "tone", 0.0, 0.02, None),
    ("clipped_ratio", "tone", 0.0, 0.01, None),
    ("p5", "tone", 3, 32, None),
    ("p99", "tone", 230, 254, None),
    ("shadow_open", "tone", 5.0, 60.0, None),
    ("shadow_luma", "tone", 4, 92, None),
    ("sat_mean", "color", 0.26, 0.56, None),
]


def check(metrics: dict) -> tuple[list[dict], int]:
    rows, hard = [], 0

    tone = metrics.get("tone") or {}
    # 两个组合判据：单独看比例会冤枉夜景，单独看分位数会冤枉高调片
    crush_judge = None
    p5 = tone.get("p5")
    cr = tone.get("crushed_ratio")
    if p5 is not None and cr is not None:
        crushed = p5 <= 1 and cr > 0.02
        crush_judge = "fail" if crushed else "ok"
        if crushed:
            hard += 1
    blow_judge = None
    p99 = tone.get("p99")
    cl = tone.get("clipped_ratio")
    if p99 is not None and cl is not None:
        blown = p99 >= 254 and cl > 0.004
        blow_judge = "fail" if blown else "ok"
        if blown:
            hard += 1

    for name, sect, lo, hi, _unused in CHECKS:
        val = (metrics.get(sect) or {}).get(name)
        if val is None:
            rows.append({"metric": name, "value": None, "range": [lo, hi], "level": "skip"})
            continue
        level = "ok" if lo <= val <= hi else "warn"
        rows.append({"metric": name, "value": val, "range": [lo, hi], "level": level})

    if crush_judge:
        rows.append({"metric": "shadow_crushed", "value": None,
                     "range": [0, 0], "level": crush_judge})
    if blow_judge:
        rows.append({"metric": "highlight_blown", "value": None,
                     "range": [0, 0], "level": blow_judge})

    # 色偏：只作为参考读数，不给判定。
    # 原因是它测不准，而且是「自信地测不准」——没有参考白，任何自动色偏估计都会被
    # 内容骗：给整张图加 +18 的蓝，原本 S=0.29 的土路会变中性被选进来，
    # 原本 S=0.15 的远山被推到 0.27 又被踢出去，选样整个换了一批物体。
    # 试过白块法（取最亮 1%）：天空亮的片子会把蓝天当成白，报出 +36 的假色偏。
    # 所以这里只把数报出来，判色偏请看图或用灰卡。
    conf = (metrics.get("color") or {}).get("cast_conf")
    for name in ("cast_rg", "cast_bg"):
        val = (metrics.get("color") or {}).get(name)
        rows.append({"metric": name, "value": val, "range": [-10.0, 10.0], "level": "info"})
    rows.append({"metric": "cast_conf", "value": conf, "range": [0.0, 0.22], "level": "info"})

    # 色相锚点：只在画面里真的有这个元素时判（抽样像素数门槛）
    hue_targets = [("hue_sky", 195, 228, 200), ("hue_green", 68, 148, 200), ("hue_earth", 12, 50, 200)]
    for name, lo, hi, min_px in hue_targets:
        val = (metrics.get("color") or {}).get(name)
        n = (metrics.get("color") or {}).get("pixels_%s" % name.replace("hue_", "")) or 0
        if val is None or n < min_px:
            rows.append({"metric": name, "value": val, "range": [lo, hi], "level": "skip"})
            continue
        level = "ok" if lo <= val <= hi else "warn"
        rows.append({"metric": name, "value": val, "range": [lo, hi], "level": level})
    return rows, hard


def print_rows(rows: list[dict]) -> None:
    print(f"  {'指标':<20}{'实测':>9}   {'期望区间':<18}判定")
    for r in rows:
        v = "—" if r["value"] is None else (f"{r['value']:.3f}" if isinstance(r["value"], float) else str(r["value"]))
        lo, hi = r["range"]
        rng = f"[{lo}, {hi}]"
        mark = {"ok": "ok", "warn": "warn", "fail": "FAIL", "skip": "skip", "info": "参考"}[r["level"]]
        print(f"  {r['metric']:<20}{v:>9}   {rng:<18}{mark}")


# ---------------------------------------------------------------- 导出

def upscale(img: Image.Image, target_long: int) -> Image.Image:
    W, H = img.size
    if max(W, H) >= target_long:
        return img
    scale = target_long / max(W, H)
    cur = img
    while max(cur.size) * 2 <= target_long:
        cur = cur.resize((cur.width * 2, cur.height * 2), Image.LANCZOS)
    final = cur.resize((max(1, round(W * scale)), max(1, round(H * scale))), Image.LANCZOS)
    radius = 1.2 + 1.2 * min(3.0, scale / 4.0)
    return final.filter(ImageFilter.UnsharpMask(radius=radius, percent=85, threshold=3))


def main() -> int:
    ap = argparse.ArgumentParser(description="纪实调色 + 体检")
    ap.add_argument("src")
    ap.add_argument("--out", help="输出路径（--check 时可省）")
    ap.add_argument("--preset", default="travel", choices=list(PRESETS))
    ap.add_argument("--strength", type=float, default=1.0, help="调色强度 0–1.5，默认 1.0")
    ap.add_argument("--clarity", type=float, default=None, help="覆盖预设的局部对比（0–60）")
    ap.add_argument("--grain", type=float, default=None, help="覆盖预设的颗粒（0–0.3）")
    ap.add_argument("--vintage", type=float, default=None, help="覆盖预设的复古分离（0–1）")
    ap.add_argument("--wb", default="off", choices=["off", "auto"],
                    help="白平衡：默认 off。auto 是灰世界猜测，风光片里常判错，慎用")
    ap.add_argument("--levels", default="auto", choices=["auto", "off"],
                    help="黑白场归位，默认 auto（色阶跨度不足 215 级时拉开）")
    ap.add_argument("--target", default="native", choices=["native", "2k", "4k", "8k"])
    ap.add_argument("--quality", type=int, default=95)
    ap.add_argument("--check", action="store_true", help="只体检，不产图")
    ap.add_argument("--report", help="把体检结果写成 JSON")
    args = ap.parse_args()

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    t0 = time.time()
    src = Path(args.src)
    if not src.exists():
        raise SystemExit(f"[in] 文件不存在：{src}")

    img = Image.open(src)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    else:
        img = img.convert("RGB")

    report: dict = {"in": src.as_posix(), "in_size": list(img.size), "preset": args.preset}

    before = analyze(img)
    report["before"] = before
    rows_b, hard_b = check(before)
    print(f"[before] {src.name} {img.width}x{img.height}")
    print_rows(rows_b)

    if args.check:
        if args.report:
            Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\n[write] {args.report}")
        print(f"\n[gate] {'未通过' if hard_b else '通过'}：{hard_b} 处硬失败（暗部压死 / 高光曝死）")
        return 1 if hard_b else 0

    if not args.out:
        raise SystemExit("[args] 不体检就必须给 --out")

    p = scale_preset(PRESETS[args.preset], args.strength)
    if args.clarity is not None:
        p["clarity"] = float(args.clarity)
    if args.grain is not None:
        p["grain"] = float(args.grain)
    if args.vintage is not None:
        p["vintage"] = float(args.vintage)
    report["params"] = {k: round(v, 4) if isinstance(v, float) else v for k, v in p.items()}

    wb, wb_info = auto_wb(img) if args.wb == "auto" else ((1.0, 1.0, 1.0), {"applied": False, "reason": "默认关闭（用 --wb auto 打开）"})
    report["white_balance"] = wb_info

    cur, pipe = apply_pipeline(img, p, wb, args.levels)
    report.update(pipe)

    if args.target != "native":
        cur = upscale(cur, SIZES[args.target])

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = out_path.suffix.lstrip(".").lower() or "jpg"
    if fmt in ("jpeg",):
        fmt = "jpg"
    if fmt == "jpg":
        cur.save(out_path, quality=args.quality, subsampling=0, optimize=True)
    elif fmt == "webp":
        cur.save(out_path, quality=args.quality, method=6)
    else:
        cur.save(out_path, compress_level=6)

    after = analyze(cur)
    report["after"] = after
    rows_a, hard_a = check(after)
    print(f"\n[after] {out_path.name} {cur.width}x{cur.height}  preset={args.preset} strength={args.strength}")
    print_rows(rows_a)

    report.update({
        "out": out_path.as_posix(),
        "out_size": list(cur.size),
        "target": args.target,
        "bytes": out_path.stat().st_size,
        "seconds": round(time.time() - t0, 2),
        "gate": {"before": hard_b, "after": hard_a},
    })
    if args.report:
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[write] {args.report}")
    print(f"[done] {out_path.as_posix()}  {report['bytes'] / 1e6:.1f} MB  {report['seconds']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
