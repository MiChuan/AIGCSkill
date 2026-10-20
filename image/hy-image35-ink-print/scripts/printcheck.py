#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""水墨平涂印刷品成图体检：量的是版面几何，不是摄影指标。

这类图翻车的方式和照片完全不同——不是噪点、不是色偏，而是**版面塌了**：
纸色不米白了、留白被填满、插画压到文字上、文字只有一行、字糊成一团、
纸面被加了颗粒噪点。这些眼睛一眼能看出「不对」，但看不出「差多少」。
所以这个脚本把六件事量化：

  1. 纸色    四角与边带取样，判定是否落在米白区间（亮度 / 微暖 / 低饱和）
  2. 留白率  与纸色接近的像素占比。低于 40% 就不是「有留白的版面」了
  3. 重心    非纸像素质心的高度。这类版面必须上重下轻
  4. 文字带  行数、水平居中偏差、标题/描述的字高比、文字最深处与纸的对比
  5. 间距    插画下缘到文字顶部的空档
  6. 干净度  纸区的高频噪声（纸纹噪点）+ 全图梯度（是否整体发虚）

依赖 Pillow。

用法：
    python printcheck.py out/demo/raw/x.png
    python printcheck.py out/demo/raw/x.png --layout out/demo/meta.json --palette out/pal/palette.json
    python printcheck.py out/demo/raw/x.png --annotate out/demo/check.png --json out/demo/report.json
"""

from __future__ import annotations

import argparse
import colorsys
import json
import math
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFilter
except ImportError:  # pragma: no cover
    sys.stderr.write("[deps] 缺少 Pillow。安装：\n  python -m pip install pillow\n")
    raise SystemExit(2)

ANALYZE_W = 760          # 分析宽度：够准、够快
PAPER_DIST = 18          # 与纸色的 RGB 距离小于它，算「纸」
INK_DIST = 60            # 大于它，算「实墨」（用于判断文字够不够深）

# 判定区间。数值来源见 references/design-notes.md
TARGET = {
    "paper_luma": (0.855, 0.985),
    "paper_warm": (4, 26),        # R - B，米白是暖的
    "paper_sat": (0.0, 0.13),
    "whitespace": (0.40, 0.76),
    "centroid_y": (0.22, 0.56),
    "text_share": (0.004, 0.14),   # 文字带内非纸像素占比
    "text_center": (0.44, 0.56),
    "size_ratio": (1.6, 4.8),      # 标题墨迹高 / 描述墨迹高（不是字号比，见 design-notes）
    "gap": (0.045, 0.24),
    "paper_noise": (0.0, 7.0),     # 纸区亮度标准差
    "edge_peak": (220.0, 900.0),   # 边缘强度 99.5 分位（Sobel 幅值，0–1020）：糊了就掉下去
    "sharp_mean": (1.0, 60.0),     # 平均梯度
}


# ---------------------------------------------------------------- 基础

def hex2rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def rgb2hex(rgb) -> str:
    return "#{:02X}{:02X}{:02X}".format(*[max(0, min(255, int(round(v)))) for v in rgb])


def luma(rgb) -> float:
    return (0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]) / 255.0


def sat(rgb) -> float:
    """HSV 饱和度。

    不要用 HLS 的 S：它对浅色极其敏感 —— 米白 (244,239,228) 在 HLS 里 S=0.42，
    在 HSV 里只有 0.065。按 HLS 判会把合格的米白纸判成「纸被染色了」。
    """
    r, g, b = [v / 255.0 for v in rgb]
    return colorsys.rgb_to_hsv(r, g, b)[1]


def hue(rgb) -> float:
    r, g, b = [v / 255.0 for v in rgb]
    return colorsys.rgb_to_hsv(r, g, b)[0] * 360.0


def cdist(a, b) -> float:
    dr, dg, db = a[0] - b[0], a[1] - b[1], a[2] - b[2]
    return math.sqrt(2 * dr * dr + 4 * dg * dg + 3 * db * db) / 3.0


def median(vals: list[float]) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def in_range(v: float, rng: tuple[float, float]) -> bool:
    return rng[0] <= v <= rng[1]


# ---------------------------------------------------------------- 测量

def detect_paper(px, w: int, h: int) -> tuple[tuple[int, int, int], dict]:
    """纸色 = 画布边缘带里最亮的那一批像素的中位色。

    为什么不用四角：插画块可能贴到上缘，文字可能压到底缘。
    为什么取「最亮的一批」：本风格的纸白永远是画面最亮的东西。
    """
    band = max(6, int(min(w, h) * 0.035))
    samples: list[tuple[int, int, int]] = []
    step = max(1, int(math.sqrt(w * h / 40000)))
    for y in range(0, h, step):
        for x in range(0, w, step):
            if x < band or x >= w - band or y < band or y >= h - band:
                samples.append(px[x, y])
    if not samples:
        samples = [px[0, 0]]

    samples.sort(key=luma)
    n = len(samples)
    # 取 40%–80% 分位：下缘避开贴边的插画块，上缘避开噪点/JPEG 高光的极值。
    # 用「最亮的 4%」会踩到噪点（噪点把上端整体抬高，纸色就量成 #FCF9EE 了）。
    lo, hi = int(n * 0.40), int(n * 0.80)
    core = samples[lo:max(lo + 1, hi)]
    paper = tuple(int(round(median([p[i] for p in core]))) for i in range(3))

    l, s, warm = luma(paper), sat(paper), paper[0] - paper[2]
    checks = {
        "luma": round(l, 4),
        "sat": round(s, 4),
        "warm_r_minus_b": warm,
        "ok_luma": in_range(l, TARGET["paper_luma"]),
        "ok_warm": in_range(warm, TARGET["paper_warm"]),
        "ok_sat": in_range(s, TARGET["paper_sat"]),
    }
    checks["ok"] = checks["ok_luma"] and checks["ok_warm"] and checks["ok_sat"]
    return paper, checks


def build_masks(img: Image.Image, paper, dist_paper: float):
    """返回 (ink_mask, whiteness_ratio)。ink = 与纸色距离超过阈值。"""
    w, h = img.size
    px = img.load()
    cols = [0] * w
    rows = [0] * h
    total = w * h
    ink = 0
    for y in range(h):
        r = 0
        for x in range(w):
            p = px[x, y]
            d = abs(p[0] - paper[0]) + abs(p[1] - paper[1]) + abs(p[2] - paper[2])
            if d > dist_paper:
                r += 1
                ink += 1
                cols[x] += 1
        rows[y] = r
    return rows, cols, 1.0 - ink / total


def row_segments(rows: list[int], h: int, zone_top: int, thr_ratio: float = 0.12) -> list[tuple[int, int]]:
    """在给定行区间里，按水平投影切出「行段」。用来数文字有几行。"""
    zone = rows[zone_top:h]
    if not zone:
        return []
    peak = max(zone) or 1
    thr = max(1, int(peak * thr_ratio))
    segs: list[tuple[int, int]] = []
    start = None
    blank = 0
    for i, v in enumerate(zone):
        if v > thr:
            if start is None:
                start = i
            blank = 0
        else:
            if start is not None:
                blank += 1
                if blank >= 3:            # 连续 3 行空白才算断行
                    segs.append((start, i - blank))
                    start = None
                    blank = 0
    if start is not None:
        segs.append((start, len(zone) - 1))
    # 丢掉高度过小的碎片（< 0.6% 画高）
    min_h = max(2, int(h * 0.006))
    return [(zone_top + a, zone_top + b) for a, b in segs if b - a >= min_h]


def zone_metrics(img: Image.Image, paper, y0: int, y1: int) -> dict:
    """统计某个水平带里的墨水分布：占比、水平中心、最深色、x 范围。"""
    w = img.size[0]
    px = img.load()
    n = ink = 0
    sx = 0
    minx, maxx = w, -1
    darkest = paper
    for y in range(y0, y1 + 1):
        for x in range(0, w, 1):
            p = px[x, y]
            d = abs(p[0] - paper[0]) + abs(p[1] - paper[1]) + abs(p[2] - paper[2])
            n += 1
            if d > PAPER_DIST * 3:
                ink += 1
                sx += x
                if x < minx:
                    minx = x
                if x > maxx:
                    maxx = x
                if luma(p) < luma(darkest):
                    darkest = p
    return {
        "share": ink / n if n else 0.0,
        "cx": (sx / ink / w) if ink else None,
        "minx": minx if ink else None,
        "maxx": maxx if ink else None,
        "darkest": darkest,
        "ink_px": ink,
    }


def sharpness(img: Image.Image) -> tuple[float, float]:
    """Sobel 梯度统计：返回 (平均梯度, 99.5 分位梯度)，尺度 0–1020。

    为什么不用 PIL 的 FIND_EDGES：它的输出被截到 255，干净的阶跃边缘一律饱和成
    239/255，模糊之后就分不出来了。自己算 Sobel 保留真实幅值（最强边可到 500+），
    模糊后掉到 100 上下，一眼能分开。

    为什么要分位而不是平均：这类版面 70% 以上是纯纸，平均值被 0 稀释得毫无区分度。
    """
    g = img.convert("L")
    px = g.load()
    w, h = g.size
    step = max(1, int(math.sqrt(w * h / 150000)))
    vals: list[int] = []
    for y in range(1, h - 1, step):
        for x in range(1, w - 1, step):
            a, b, c = px[x - 1, y - 1], px[x, y - 1], px[x + 1, y - 1]
            d0, f = px[x - 1, y], px[x + 1, y]
            g0, hh, i = px[x - 1, y + 1], px[x, y + 1], px[x + 1, y + 1]
            gx = (c + 2 * f + i) - (a + 2 * d0 + g0)
            gy = (g0 + 2 * hh + i) - (a + 2 * b + c)
            vals.append(abs(gx) + abs(gy))
    if not vals:
        return 0.0, 0.0
    vals.sort()
    mean = sum(vals) / len(vals)
    return mean, vals[min(len(vals) - 1, int(len(vals) * 0.995))]


def paper_noise(img: Image.Image, paper) -> tuple[float, int]:
    """纸区亮度标准差（网格法）。纸纹、颗粒、噪点都会把这个数顶上去。

    为什么不用「主体上方的整条带」：插画块可能贴到上缘，一裁就裁进色块；
    而噪点图会让「墨」散布全图，主体边界也就测不准了。
    改成按 24×24 网格找「中位色接近纸色」的格子——那些格子一定是纸，
    再量它们内部的标准差。干净纸 < 2，有颗粒 > 8。
    """
    g = img.convert("RGB")
    px = g.load()
    w, h = g.size
    gw, gh = 24, 24
    cw, ch = max(2, w // gw), max(2, h // gh)
    pl = luma(paper)
    stds: list[float] = []
    for gy in range(gh):
        for gx in range(gw):
            x0, y0 = gx * cw, gy * ch
            if x0 + cw > w or y0 + ch > h:
                continue
            vals = []
            for y in range(y0, y0 + ch, 2):
                for x in range(x0, x0 + cw, 2):
                    vals.append((px[x, y][0] + px[x, y][1] + px[x, y][2]) / 3 / 255.0)
            if not vals:
                continue
            m = sum(vals) / len(vals)
            if abs(m - pl) > 0.02:      # 这一格不是纸
                continue
            sd = math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals))
            stds.append(sd * 255.0)
    if not stds:
        return 0.0, 0
    return median(stds), len(stds)


# ---------------------------------------------------------------- 主判定

def check(img_path: Path, layout: dict | None, palette: dict | None) -> tuple[dict, int]:
    src = Image.open(img_path).convert("RGB")
    full_w, full_h = src.size
    img = src.copy()
    img.thumbnail((ANALYZE_W, ANALYZE_W * 4), Image.LANCZOS)
    w, h = img.size
    px = img.load()

    paper, paper_chk = detect_paper(px, w, h)
    rows, cols, whitespace = build_masks(img, paper, PAPER_DIST * 3)

    # 文字带：优先用 layout 给出的 title_y/desc_y，否则取底部 26%
    lo = dict(layout or {})
    ty = float(lo.get("title_y", 0.80))
    dy = float(lo.get("desc_y", 0.875))
    zone_top_ratio = max(0.0, ty - 0.055)
    zone_top = int(h * zone_top_ratio)
    zone = zone_metrics(img, paper, zone_top, h - 1)

    segs = row_segments(rows, h, zone_top)
    seg_info = []
    for a, b in segs:
        m = zone_metrics(img, paper, a, b)
        seg_info.append({
            "top": round(a / h, 4),
            "bottom": round(b / h, 4),
            "height": b - a + 1,
            "cx": round(m["cx"], 3) if m["cx"] is not None else None,
            "share_in_band": round(m["share"], 4),
        })
    # 行段按高度排序：标题一定是高的那个
    by_h = sorted(segs, key=lambda s: -(s[1] - s[0]))
    size_ratio = None
    if len(by_h) >= 2:
        size_ratio = (by_h[0][1] - by_h[0][0] + 1) / max(1, by_h[1][1] - by_h[1][0] + 1)

    # 插画带：文字带以上、非纸像素的行范围
    body_rows = rows[:zone_top]
    body_ink_rows = [i for i, v in enumerate(body_rows) if v > max(2, int(w * 0.006))]
    body_top = body_ink_rows[0] if body_ink_rows else None
    body_bottom = body_ink_rows[-1] if body_ink_rows else None
    body_ink_px = sum(body_rows)

    cy = None
    if body_ink_px:
        acc = 0
        for i, v in enumerate(body_rows):
            acc += v * i
        cy = acc / body_ink_px / h

    gap = None
    if body_bottom is not None:
        gap = (segs[0][0] - body_bottom) / h if segs else (zone_top - body_bottom) / h

    # 边距（不含文字带，只看插画块）
    margins = None
    body_cols = [v for v in cols]
    left = next((i for i, v in enumerate(body_cols) if v > max(2, int(h * 0.004))), None)
    right = next((i for i, v in enumerate(reversed(body_cols)) if v > max(2, int(h * 0.004))), None)
    if left is not None and right is not None:
        margins = {"left": round(left / w, 4), "right": round(right / w, 4)}

    # 纸区噪声：网格法，不依赖主体的位置
    noise, noise_cells = paper_noise(img, paper)
    noise = round(noise, 2)

    sharp_mean, sharp_peak = sharpness(img)
    sharp_mean, sharp_peak = round(sharp_mean, 2), round(sharp_peak, 2)

    # 文字深浅：文字带最深色与纸色的亮度差（0–255 尺度，80 以上缩到缩略图也看得见）
    text_contrast = round(abs(luma(zone["darkest"]) - luma(paper)) * 255, 1)

    # 色相校验（可选）
    hue_note = None
    if palette:
        target_hues = [hue(tuple(c["rgb"])) for c in palette.get("clusters", []) if c.get("role") != "drop"]
        if target_hues:
            # 采样成图的实墨像素，看它的色相离原图色板多远
            ink_hues = []
            step = max(1, int(math.sqrt(w * h / 9000)))
            for y in range(0, h, step):
                for x in range(0, w, step):
                    p = px[x, y]
                    d = abs(p[0] - paper[0]) + abs(p[1] - paper[1]) + abs(p[2] - paper[2])
                    if d > INK_DIST and sat(p) > 0.10:
                        ink_hues.append(hue(p))
            if ink_hues:
                far = 0
                for hh in ink_hues:
                    best = min(min(abs(hh - t), 360 - abs(hh - t)) for t in target_hues)
                    if best > 32:
                        far += 1
                hue_note = {
                    "off_palette_ratio": round(far / len(ink_hues), 3),
                    "sampled": len(ink_hues),
                }

    # ------------------------------------------------------------ 判据
    issues: list[tuple[str, str]] = []
    score = 100.0

    if not paper_chk["ok"]:
        score -= 22
        if not paper_chk["ok_luma"]:
            issues.append(("error", f"纸色亮度 {paper_chk['luma']:.3f} 不在 {TARGET['paper_luma']} 区间："
                                    "不是死白（>0.985）就是灰了（<0.855）。米白纸要的是「比白暗一点点的暖白」"))
        if not paper_chk["ok_warm"]:
            issues.append(("error", f"纸色 R-B = {paper_chk['warm_r_minus_b']}："
                                    f"米白必须偏暖（{TARGET['paper_warm']}）。"
                                    "接近 0 是中性白，负值是冷白——那就是 PPT 背景"))
        if not paper_chk["ok_sat"]:
            issues.append(("error", f"纸色饱和 {paper_chk['sat']:.3f} 偏高（>{TARGET['paper_sat'][1]}）："
                                    "纸被染了色，或者画面整体色偏"))

    if not in_range(whitespace, TARGET["whitespace"]):
        score -= 18
        if whitespace < TARGET["whitespace"][0]:
            issues.append(("error", f"留白只有 {whitespace * 100:.1f}%（目标 {TARGET['whitespace'][0] * 100:.0f}%–"
                                    f"{TARGET['whitespace'][1] * 100:.0f}%）：插画把画面填满了。"
                                    "这不是「有留白的版面」，这是「一张插画」。重出时把 structure.planes 砍到 6 个以内、"
                                    "layout.block_bottom 压到 0.60 以下"))
        else:
            issues.append(("warn", f"留白 {whitespace * 100:.1f}% 偏多（>{TARGET['whitespace'][1] * 100:.0f}%）："
                                   "插画太小了，会读作「一个 logo 摆在纸上」。放大到画幅宽度的 65%–75%"))

    if cy is not None and not in_range(cy, TARGET["centroid_y"]):
        score -= 10
        issues.append(("warn", f"墨色重心在 {cy * 100:.0f}% 高度（目标 {TARGET['centroid_y'][0] * 100:.0f}%–"
                               f"{TARGET['centroid_y'][1] * 100:.0f}%）：本版面必须上重下轻。"
                               "重心偏低说明插画往下坠了，或者下面多了一块不该有的色"))

    if not in_range(zone["share"], TARGET["text_share"]):
        score -= 14
        if zone["ink_px"] == 0:
            issues.append(("error", "底部文字带里没有任何墨：文字没排上去。"
                                    "这是本风格的第二大失败模式（模型常把文字省掉）"))
        elif zone["share"] > TARGET["text_share"][1]:
            issues.append(("error", f"文字带里的墨占 {zone['share'] * 100:.1f}%（>{TARGET['text_share'][1] * 100:.0f}%）："
                                    "不是文字，是一块色块或一条装饰线。文字只该占这条带的百分之几"))
        else:
            issues.append(("warn", f"文字带里的墨只有 {zone['share'] * 100:.2f}%：字太小或太淡，"
                                   "打印出来会消失"))

    if len(segs) == 0:
        issues.append(("error", "没检测到文字行"))
    elif len(segs) == 1:
        score -= 10
        issues.append(("error", "只检测到 1 行文字：标题与描述没分开，或者描述被模型漏掉了。"
                                "两行是这套版面的基本结构，缺一行就塌了一半"))
    elif len(segs) > 2:
        score -= 8
        issues.append(("warn", f"检测到 {len(segs)} 行文字（应为 2 行）：模型多排了内容，"
                               "或者某一行被拆成了两行。多余的行会破坏克制感"))

    if zone["cx"] is not None and not in_range(zone["cx"], TARGET["text_center"]):
        score -= 8
        issues.append(("warn", f"文字块水平中心在 {zone['cx'] * 100:.0f}%：没有居中。"
                               "目测会明显偏，重出或后期叠字"))

    if size_ratio is not None and not in_range(size_ratio, TARGET["size_ratio"]):
        score -= 6
        issues.append(("warn", f"标题/描述字高比 {size_ratio:.2f}（目标 {TARGET['size_ratio'][0]}–"
                               f"{TARGET['size_ratio'][1]}，按墨迹高度量）：小于 1.6 是「两个标题」，"
                               "大于 4.8 是「标题配一行免责声明」，都不像图录扉页"))

    if gap is not None and not in_range(gap, TARGET["gap"]):
        score -= 12
        if gap < TARGET["gap"][0]:
            issues.append(("error", f"插画下缘到文字的间距只有 {gap * 100:.1f}%（目标 ≥ "
                                    f"{TARGET['gap'][0] * 100:.0f}%）：文字被色块托着，"
                                    "版面立刻变挤。这是最常见的排版事故"))
        else:
            issues.append(("warn", f"间距 {gap * 100:.1f}% 过大：插画和文字断成两截，中间空得没有理由"))

    if text_contrast < 80:
        score -= 10
        issues.append(("warn", f"文字最深处与纸色的差只有 {text_contrast:.0f}（目标 ≥ 80）："
                               "字太淡，缩到缩略图就看不见了"))

    if margins:
        if min(margins["left"], margins["right"]) < 0.03:
            score -= 6
            issues.append(("warn", f"插画块贴边了（左 {margins['left'] * 100:.1f}% / 右 "
                                   f"{margins['right'] * 100:.1f}%）：左右各留 12% 以上的纸白"))

    if noise_cells >= 8 and not in_range(noise, TARGET["paper_noise"]):
        score -= 10
        issues.append(("error", f"纸区亮度标准差 {noise}（目标 ≤ {TARGET['paper_noise'][1]}，"
                                f"取样 {noise_cells} 个纸格）：纸面上有颗粒/纹理/明暗不匀。"
                                "米白纸的质感是「均匀哑光」，加了噪点就变成「一张拍了纸的照片」"))

    if not in_range(sharp_peak, TARGET["edge_peak"]):
        score -= 10
        if sharp_peak < TARGET["edge_peak"][0]:
            issues.append(("error", f"色块边缘的陡度（梯度 99.5 分位）只有 {sharp_peak}（目标 ≥ "
                                    f"{TARGET['edge_peak'][0]}）：整张图发虚。「边缘柔和」被理解成全体高斯模糊了"
                                    "——色块的边必须是干净的，只有墨接触纸的那一圈是渐隐的"))
        else:
            issues.append(("warn", f"边缘陡度 {sharp_peak} 偏高：像被加了锐化，或者边界有噪点"))

    if hue_note and hue_note["off_palette_ratio"] > 0.35:
        score -= 8
        issues.append(("warn", f"{hue_note['off_palette_ratio'] * 100:.0f}% 的实墨像素色相距原图色板超过 32°："
                               "模型自己加了一套色。重出时把配色段的 hex 写在提示词最前面"))

    report = {
        "image": str(img_path),
        "size": [full_w, full_h],
        "analyzed_size": [w, h],
        "paper": {"hex": rgb2hex(paper), "rgb": list(paper), **paper_chk},
        "whitespace": round(whitespace, 4),
        "ink_ratio": round(1 - whitespace, 4),
        "centroid_y": round(cy, 4) if cy is not None else None,
        "body_top": round(body_top / h, 4) if body_top is not None else None,
        "body_bottom": round(body_bottom / h, 4) if body_bottom is not None else None,
        "text_zone_top": round(zone_top_ratio, 4),
        "text_share": round(zone["share"], 4),
        "text_cx": round(zone["cx"], 3) if zone["cx"] is not None else None,
        "text_lines": len(segs),
        "lines": seg_info,
        "size_ratio": round(size_ratio, 2) if size_ratio is not None else None,
        "gap_to_text": round(gap, 4) if gap is not None else None,
        "text_contrast": text_contrast,
        "margins": margins,
        "paper_noise": noise,
        "paper_cells": noise_cells,
        "sharpness": sharp_mean,
        "edge_peak": sharp_peak,
        "palette_check": hue_note,
        "score": round(max(0.0, score), 1),
        "issues": [f"[{lv}] {m}" for lv, m in issues],
    }
    errors = [m for lv, m in issues if lv == "error"]
    return report, (1 if errors else 0)


def annotate(img_path: Path, report: dict, out: Path) -> None:
    """把检测结果画在图上，方便肉眼对照。"""
    img = Image.open(img_path).convert("RGB")
    w, h = img.size
    ov = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    paper = tuple(report["paper"]["rgb"])

    # 文字带
    zt = int(h * report["text_zone_top"])
    d.rectangle([0, zt, w, h - 1], outline=(220, 90, 90, 200), width=max(2, w // 400))
    d.text((8, zt + 4), "text zone", fill=(220, 90, 90, 255))

    # 每一行
    for ln in report["lines"]:
        a, b = int(ln["top"] * h), int(ln["bottom"] * h)
        d.rectangle([0, a, w, b], outline=(90, 140, 220, 220), width=max(2, w // 500))

    # 插画带
    if report["body_top"] is not None and report["body_bottom"] is not None:
        bt, bb = int(report["body_top"] * h), int(report["body_bottom"] * h)
        d.rectangle([0, bt, w, bb], outline=(70, 160, 110, 200), width=max(2, w // 400))
        d.text((8, bt + 4), f"body  gap={report['gap_to_text']}", fill=(70, 160, 110, 255))

    # 间距带
    if report["body_bottom"] is not None and report["lines"]:
        a = int(report["body_bottom"] * h)
        b = int(report["lines"][0]["top"] * h)
        d.rectangle([int(w * 0.02), a, int(w * 0.98), b], outline=(230, 170, 60, 220), width=max(2, w // 500))

    # 纸色样本角
    s = max(10, w // 30)
    for (x, y) in ((0, 0), (w - s, 0), (0, h - s), (w - s, h - s)):
        d.rectangle([x, y, x + s, y + s], fill=paper + (170,))

    img = Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")
    dd = ImageDraw.Draw(img)
    head = (
        f"paper {report['paper']['hex']}  white {report['whitespace'] * 100:.0f}%  "
        f"cy {report['centroid_y']}  lines {report['text_lines']}  "
        f"ratio {report['size_ratio']}  score {report['score']}"
    )
    dd.rectangle([0, 0, w, 26], fill=(255, 255, 255))
    dd.text((8, 7), head, fill=(30, 30, 30))
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)


def load_layout(p: str | None) -> dict | None:
    if not p:
        return None
    try:
        data = json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return None
    return data.get("layout", data)


def main() -> int:
    ap = argparse.ArgumentParser(description="水墨平涂印刷品 成图版面体检")
    ap.add_argument("image", help="成图路径")
    ap.add_argument("--layout", help="build_print_prompt.py 产出的 meta.json（读 layout 预期值）")
    ap.add_argument("--palette", help="extract_palette.py 产出的 palette.json（校验色相是否跑偏）")
    ap.add_argument("--json", dest="json_out", help="把报告写成 JSON")
    ap.add_argument("--annotate", help="输出一张标注图，画出检测到的文字带/行/间距")
    ap.add_argument("--quiet", action="store_true", help="只输出判定行，不打印明细")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    path = Path(args.image)
    if not path.exists():
        print(f"[error] 成图不存在：{path}", file=sys.stderr)
        return 2

    palette = None
    if args.palette and Path(args.palette).exists():
        palette = json.loads(Path(args.palette).read_text(encoding="utf-8"))

    report, rc = check(path, load_layout(args.layout), palette)

    if not args.quiet:
        p = report["paper"]
        print(f"[paper] {p['hex']}  luma {p['luma']:.3f}  sat {p['sat']:.3f}  R-B {p['warm_r_minus_b']}"
              f"  {'OK' if p['ok'] else 'FAIL'}")
        print(f"[layout] white {report['whitespace'] * 100:.1f}%  ink {report['ink_ratio'] * 100:.1f}%  "
              f"centroid_y {report['centroid_y']}  body {report['body_top']}–{report['body_bottom']}")
        print(f"[text]   lines {report['text_lines']}  share {report['text_share'] * 100:.2f}%  "
              f"cx {report['text_cx']}  size_ratio {report['size_ratio']}  "
              f"gap {report['gap_to_text']}  contrast {report['text_contrast']}")
        print(f"[clean]  paper_noise {report['paper_noise']} ({report['paper_cells']} cells)  "
              f"sharpness {report['sharpness']}  edge_peak {report['edge_peak']}")
        if report["palette_check"]:
            print(f"[hue]    off_palette {report['palette_check']['off_palette_ratio'] * 100:.0f}%"
                  f"  ({report['palette_check']['sampled']} px)")

    for line in report["issues"]:
        print(line)
    if not report["issues"]:
        print("[ok] 版面、纸色、文字、干净度全部落在区间内")
    print(f"[score] {report['score']}")

    if args.json_out:
        p = Path(args.json_out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[write] {p}")
    if args.annotate:
        annotate(path, report, Path(args.annotate))
        print(f"[write] {args.annotate}")

    return rc


if __name__ == "__main__":
    sys.exit(main())
