#!/usr/bin/env python3
"""看图：把一张图量成一串能写进提示词的数字。

反推提示词最容易犯的错，是把「看起来」当「测出来」：
「电影感」其实是 2.39:1 + 青橙分离 + 高光柔化；「暖调」其实是 R-B 差 0.11。
这个脚本不给风格名，只给可复现的量，让人把形容词换成参数。

用法：
    python scripts/probe.py photo.jpg --outdir out/probe
    python scripts/probe.py photo.jpg --outdir out/probe --k 7 --no-annotate

产出：
    out/probe/probe.json   机器读，喂给 build_prompt.py
    out/probe/probe.md     人读，含「这一项怎么写进提示词」
    out/probe/probe.png    标注图（九宫格 / 三分线 / 主体框 / 色卡）

依赖：Pillow。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image, ImageDraw, ImageFilter, ImageOps  # noqa: E402

import imgkit as K  # noqa: E402

GRID_NAMES = ["左上", "顶部", "右上", "左侧", "中心", "右侧", "左下", "底部", "右下"]
DIRECTION_TEXT = [
    "左上 45° 侧光", "顶部光（正上方）", "右上 45° 侧光",
    "左侧 90° 侧光", "正面光 / 中心聚光", "右侧 90° 侧光",
    "左下低位侧光", "底光（非自然光）", "右下低位侧光",
]


# ------------------------------------------------------------------ 各项量测

def brightness_stats(luma):
    p5, p50, p95 = K.quantiles(luma, (0.05, 0.5, 0.95))
    m = K.mean(luma)
    clip_hi = sum(1 for v in luma if v > 0.985) / float(len(luma))
    clip_lo = sum(1 for v in luma if v < 0.015) / float(len(luma))
    contrast = p95 - p5
    return {
        "mean": round(m, 4),
        "p5": round(p5, 4),
        "p50": round(p50, 4),
        "p95": round(p95, 4),
        "contrast": round(contrast, 4),
        "clip_high": round(clip_hi, 4),
        "clip_low": round(clip_lo, 4),
        "label": K.level(contrast, [0.30, 0.55, 0.75],
                         ["低反差 / 灰雾 / 柔光", "中等反差", "高反差", "极高反差 / 剪影"]),
    }


def saturation_stats(r, g, b):
    sats = []
    for ri, gi, bi in zip(r, g, b):
        mx, mn = max(ri, gi, bi), min(ri, gi, bi)
        sats.append(0.0 if mx == 0 else (mx - mn) / float(mx))
    sats.sort()
    m = K.mean(sats)
    p90 = sats[int(0.9 * (len(sats) - 1))]
    return {
        "mean": round(m, 4),
        "p90": round(p90, 4),
        "label": K.level(m, [0.12, 0.30, 0.55, 0.75],
                         ["近黑白", "低饱和", "自然", "浓艳", "极艳 / 过饱和"]),
    }


def temperature_stats(r, g, b):
    warmth = (K.mean(r) - K.mean(b)) / 255.0
    return {
        "warmth": round(warmth, 4),
        "label": K.level(warmth, [-0.10, -0.03, 0.03, 0.10],
                         ["冷调（蓝青）", "微冷", "中性", "微暖", "暖调（金黄 / 橙）"]),
    }


def light_stats(luma, w, h):
    grid = K.grid_means(luma, w, h, 3, 3)
    flat = [grid[j][i] for j in range(3) for i in range(3)]
    spread = max(flat) - min(flat)
    m = K.mean(flat)
    hi = -1
    if spread < 0.055:
        direction = "均匀漫射（无明确主光方向）"
    else:
        rows = [K.mean(row) for row in grid]
        mid_gain = rows[1] - max(rows[0], rows[2])
        center = grid[1][1]
        corners = [grid[0][0], grid[0][2], grid[2][0], grid[2][2]]
        center_gain = center - max(grid[1][0], grid[1][2], grid[0][1], grid[2][1])
        if center_gain > 0.06 and all(c < center * 0.82 for c in corners):
            direction = "中心聚光（主体受光、四周压暗）"
            hi = 4
        elif mid_gain > 0.09:
            direction = "水平带状光源（地平线发光 / 低角度阳光 / 逆光）"
            hi = max((grid[1][i], i) for i in range(3))[1] + 3
        else:
            hi = max(range(9), key=lambda i: flat[i])
            direction = DIRECTION_TEXT[hi]

    p5, _, p95 = K.quantiles(luma, (0.05, 0.5, 0.95))
    ratio = (p95 + 0.02) / (p5 + 0.02)
    quality = K.level(ratio, [2.5, 6.0, 15.0],
                      ["软光（光比小、过渡长）", "中性光比", "硬光（光比大、边界利）",
                       "极高光比 / 剪影"])

    top = K.mean(grid[0])
    bottom = K.mean(grid[2])
    backlit = (top - bottom > 0.12) and (grid[1][1] < top * 0.92)

    return {
        "direction": direction,
        "grid_index": hi,
        "grid": [[round(v, 4) for v in row] for row in grid],
        "spread": round(spread, 4),
        "key_fill_ratio": round(ratio, 2),
        "quality": quality,
        "top_bottom_delta": round(top - bottom, 4),
        "backlit_suspect": bool(backlit),
    }


def sharpness_stats(mag, gm):
    gmean = K.mean(mag)
    score = int(round(100 * K.clamp(__import__("math").log10(gmean * 1000 + 1) / 1.7)))
    return {
        "grad_mean": round(gmean, 5),
        "score": score,
        "grid": [[round(v, 5) for v in row] for row in gm],
        "label": K.level(score, [25, 45, 70, 88],
                         ["很软 / 虚焦", "偏软", "正常", "锐利", "极锐（像过度锐化或渲染图）"]),
    }


def dof_stats(gm):
    center = gm[1][1]
    corners = K.mean([gm[0][0], gm[0][2], gm[2][0], gm[2][2]])
    ratio = K.safe_div(center, corners, 1.0)
    return {
        "center_edge_ratio": round(ratio, 3),
        "label": K.level(ratio, [0.85, 1.30, 1.80],
                         ["中心反而不如边缘清楚（径向模糊 / 移轴）", "全景清晰",
                          "中浅景深", "浅景深（背景明显虚化）"]),
    }


def noise_stats(work, luma, mag):
    blur = work.filter(ImageFilter.GaussianBlur(1.2))
    r2, g2, b2 = K.channels(blur)
    luma_b = K.luma_of(r2, g2, b2)
    thr = sorted(mag)[int(0.30 * (len(mag) - 1))]
    resid = [abs(a - c) * 255.0 for a, c, mm in zip(luma, luma_b, mag) if mm <= thr]
    if not resid:
        resid = [abs(a - c) * 255.0 for a, c in zip(luma, luma_b)]
    rms = K.mean(resid) * 1.28
    return {
        "rms": round(rms, 2),
        "label": K.level(rms, [1.2, 3.0, 6.0],
                         ["极干净（渲染 / AI 出图特征）", "轻微颗粒", "明显颗粒（胶片感）",
                          "高噪点（高 ISO）"]),
    }


def vignette_stats(luma, w, h):
    """角 vs 同半径边中点，减弱「上亮下暗」类内容对暗角的干扰。

    结果仍然是启发式：深色内容出现在四角时会高估暗角，
    所以这里只出倾向标签，不下硬结论。
    """
    g5 = K.grid_means(luma, w, h, 5, 5)
    corners = K.mean([g5[0][0], g5[0][4], g5[4][0], g5[4][4]])
    edges = K.mean([g5[0][2], g5[2][0], g5[2][4], g5[4][2]])
    center = g5[2][2]
    ce = K.safe_div(corners, edges, 1.0)
    cc = K.safe_div(corners, center, 1.0)
    return {
        "corner_to_edge": round(ce, 3),
        "corner_to_center": round(cc, 3),
        "label": K.level(ce, [0.82, 0.90, 0.96],
                         ["四角明显比同半径边中点暗，像有后期暗角", "疑似轻微暗角",
                          "角与边基本一致，暗角不明显", "无明显暗角"]),
        "note": "内容影响大：四角本身是暗部（地面 / 阴影 / 边框）时会把这项判高，须人眼对照原图。",
    }


def border_colors(work, k=3):
    """四边条带的众数色，作为「背景该长什么样」的参照。

    用边带聚类而不是四角中位数：渐变天空 / 上下双色这类图，
    四角混合出来的中位色会把大半张图都算成前景。
    """
    w, h = work.size
    bw = max(4, int(min(w, h) * 0.06))
    px = []
    for box in ((0, 0, w, bw), (0, h - bw, w, h), (0, 0, bw, h), (w - bw, 0, w, h)):
        crop = work.crop(box)
        cw, ch = crop.size
        small = crop.resize((max(2, cw // 4), max(2, ch // 4)), Image.Resampling.BILINEAR)
        d = small.tobytes()
        px += list(zip(d[0::3], d[1::3], d[2::3]))
    pal = K.kmeans_points(px, k=k, iters=10)
    return [tuple(p["rgb"]) for p in pal], pal


def foreground_regions(work, bg_rgb, thr=46):
    """与边带背景色差异大的连通区域，按面积排序，最多返回 3 个候选。

    这是「候选前景」，不是主体分割：亮源、深影、边框都可能上榜，
    选哪个由读图的人决定。
    """
    w, h = work.size
    sw, sh = max(8, w // 4), max(8, h // 4)
    small = work.resize((sw, sh), Image.Resampling.BILINEAR)
    r, g, b = K.channels(small)
    total = sw * sh
    mask = bytearray(total)
    for i in range(total):
        dmin = 1 << 20
        for (cr, cg, cb) in bg_rgb:
            d = abs(r[i] - cr) + abs(g[i] - cg) + abs(b[i] - cb)
            if d < dmin:
                dmin = d
        if dmin > thr:
            mask[i] = 255

    seen = bytearray(total)
    regions = []
    for start in range(total):
        if not mask[start] or seen[start]:
            continue
        stack = [start]
        seen[start] = 1
        area = 0
        minx, miny, maxx, maxy = sw, sh, -1, -1
        xs = ys = 0
        sr = sg = sb = 0
        while stack:
            i = stack.pop()
            area += 1
            y, x = divmod(i, sw)
            xs += x
            ys += y
            sr += r[i]
            sg += g[i]
            sb += b[i]
            if x < minx:
                minx = x
            if x > maxx:
                maxx = x
            if y < miny:
                miny = y
            if y > maxy:
                maxy = y
            if x > 0:
                n = i - 1
                if mask[n] and not seen[n]:
                    seen[n] = 1
                    stack.append(n)
            if x < sw - 1:
                n = i + 1
                if mask[n] and not seen[n]:
                    seen[n] = 1
                    stack.append(n)
            if y > 0:
                n = i - sw
                if mask[n] and not seen[n]:
                    seen[n] = 1
                    stack.append(n)
            if y < sh - 1:
                n = i + sw
                if mask[n] and not seen[n]:
                    seen[n] = 1
                    stack.append(n)
        borders = (minx <= 1) + (miny <= 1) + (maxx >= sw - 2) + (maxy >= sh - 2)
        regions.append({
            "area_share": area / float(total),
            "centroid": [xs / area / float(sw), ys / area / float(sh)],
            "bbox": [minx / float(sw), miny / float(sh),
                     maxx / float(sw), maxy / float(sh)],
            "color": (sr // area, sg // area, sb // area),
            "borders": borders,
        })

    keep = []
    for rg in regions:
        share = rg["area_share"]
        if share < 0.003:
            continue
        if rg["borders"] >= 2 and share > 0.05:
            continue
        if rg["borders"] == 1 and share > 0.15:
            continue
        keep.append(rg)
    keep.sort(key=lambda rg: -rg["area_share"])

    out = []
    for rg in keep[:3]:
        cx, cy = rg["centroid"]
        hx = "左三分" if cx < 0.38 else ("右三分" if cx > 0.62 else "水平中心")
        vy = "上三分" if cy < 0.38 else ("下三分" if cy > 0.62 else "垂直中心")
        cr, cg, cb = rg["color"]
        out.append({
            "area_share": round(rg["area_share"], 4),
            "centroid": [round(cx, 3), round(cy, 3)],
            "bbox": [round(v, 3) for v in rg["bbox"]],
            "position": "%s / %s" % (hx, vy),
            "mean_color": "#%02x%02x%02x" % (cr, cg, cb),
            "borders": rg["borders"],
        })
    return out


def pick_subject(regions):
    if not regions:
        return {"area_share": 0.0, "centroid": None, "bbox": None,
                "position": "未检出前景候选（整幅与边缘同色，可能是纹理 / 场景图）",
                "confidence": "low", "source": "regions"}
    first = regions[0]
    conf = "high"
    if len(regions) > 1 and regions[1]["area_share"] > first["area_share"] * 0.6:
        conf = "medium"
    out = dict(first)
    out["confidence"] = conf
    out["source"] = "regions"
    out.pop("borders", None)
    return out


def composition_stats(luma, mag, dx, dy, w, h):
    mirror = 0.0
    cnt = 0
    for y in range(0, h, 2):
        base = y * w
        for x in range(0, w // 2, 2):
            mirror += abs(luma[base + x] - luma[base + (w - 1 - x)])
            cnt += 1
    sym = K.safe_div(mirror, cnt)
    thr = sorted(mag)[int(0.90 * (len(mag) - 1))]
    hv = 0
    vv = 0
    for i in range(len(mag)):
        if mag[i] >= thr and thr > 0:
            if dy[i] >= dx[i]:
                hv += 1
            else:
                vv += 1
    h_share = K.safe_div(hv, hv + vv)
    return {
        "mirror_diff": round(sym, 4),
        "symmetry": K.level(sym, [0.035, 0.07, 0.12],
                            ["强左右对称", "近似对称", "弱对称", "不对称"]),
        "h_line_share": round(h_share, 3),
        "line_hint": ("明显水平线（地平线 / 檐口 / 桌面）" if h_share > 0.62
                      else ("明显竖直线（建筑 / 树干 / 门框）" if h_share < 0.38
                            else "斜向或曲线为主")),
    }


def text_suspect(work, mag, w, h):
    gmean = K.mean(mag) or 1e-6
    bands = {}
    for name, (y0, y1) in (("top", (0, max(2, int(h * 0.10)))),
                           ("bottom", (int(h * 0.88), h))):
        vals = mag[y0 * w:y1 * w]
        bands[name] = round(K.safe_div(K.mean(vals), gmean), 3)
    bw, bh = max(4, int(w * 0.07)), max(4, int(h * 0.07))
    corner_vals = []
    for bx, by in ((0, 0), (w - bw, 0), (0, h - bh), (w - bw, h - bh)):
        for y in range(by, by + bh, 2):
            base = y * w
            corner_vals += mag[base + bx: base + bx + bw]
    corners = round(K.safe_div(K.mean(corner_vals), gmean), 3)
    reasons = []
    if bands["bottom"] > 1.25:
        reasons.append("底部条带边缘密度为全图 %.2f 倍，像有一行字 / 水印 / 版权条" % bands["bottom"])
    if bands["top"] > 1.25:
        reasons.append("顶部条带边缘密度为全图 %.2f 倍，注意标题区 / 台标" % bands["top"])
    if corners > 1.60:
        reasons.append("角落边缘密度为全图 %.2f 倍，可能有 logo / 角标" % corners)
    return {"top_ratio": bands["top"], "bottom_ratio": bands["bottom"],
            "corner_ratio": corners, "flag": bool(reasons), "reasons": reasons}


# ------------------------------------------------------------------ 判读建议

def build_advice(d):
    a = []
    lt = d["light"]
    a.append("光位写「%s」，光比 %.1f:1 按「%s」描述；只写「柔和自然光」等于没写。"
             % (lt["direction"], lt["key_fill_ratio"], lt["quality"]))
    if lt["backlit_suspect"]:
        a.append("上亮下暗且中部偏暗，疑似逆光：写「逆光 / 轮廓光」，并补「主体正面补光」防止糊成黑影。")
    cx = d["brightness"]["contrast"]
    a.append("反差 %.2f（%s）：翻译成「%s」，别写「层次丰富」。"
             % (cx, d["brightness"]["label"],
                "低反差、阴影通透" if cx < 0.35 else
                ("中间调饱满、黑白场都到位" if cx < 0.62 else "硬调、暗部压死、高光留一点细节")))
    pal = [p for p in d["palette"] if p["share"] >= 0.05][:5]
    if pal:
        a.append("色板写成具体颜色而不是「色彩丰富」：%s（占比 %s）。"
                 % ("、".join("%s %s" % (p["name"], p["hex"]) for p in pal[:4]),
                    "/".join("%.0f%%" % (p["share"] * 100) for p in pal[:4])))
    temp = d["temperature"]
    a.append("色温写「%s」（R-B = %.3f）：%s" % (
        temp["label"], temp["warmth"],
        "在提示词里写「暖调、夕阳金光」" if temp["warmth"] > 0.03 else
        ("写「冷调、蓝青阴影」" if temp["warmth"] < -0.03 else "写「中性白平衡」，别加冷暖词")))
    sat = d["saturation"]
    a.append("饱和度 %.2f（%s）：%s" % (
        sat["mean"], sat["label"],
        "写「低饱和、接近胶片淡彩」" if sat["mean"] < 0.30 else
        ("写「自然饱和度」" if sat["mean"] < 0.55 else "写「高饱和、色彩浓郁」，注意别再加 HDR")))
    dof = d["dof"]
    a.append("景深写「%s」（中心/边缘梯度 %.2f）：%s" % (
        dof["label"], dof["center_edge_ratio"],
        "补「背景虚化、焦外柔和、主体锐利」" if dof["center_edge_ratio"] > 1.3
        else "补「全景清晰、从前景到远景都在焦内」"))
    if d["sharpness"]["score"] >= 88:
        a.append("锐度 %d 分（%s）：这是渲染图或强锐化的特征，写「干净的数字渲染、无噪点」。"
                 % (d["sharpness"]["score"], d["sharpness"]["label"]))
    if d["noise"]["rms"] >= 3.0:
        a.append("噪声 RMS %.1f：写「%s」，不要写「干净无噪」。"
                 % (d["noise"]["rms"], "细密胶片颗粒" if d["noise"]["rms"] < 6 else "高 ISO 粗颗粒"))
    if d["vignette"]["corner_to_center"] < 0.95:
        a.append("四角/中心亮度比 %.2f（%s）：写「%s」。"
                 % (d["vignette"]["corner_to_center"], d["vignette"]["label"],
                    "轻微暗角" if d["vignette"]["corner_to_center"] > 0.90 else "明显暗角、四周压暗"))
    sj = d["subject"]
    if sj["centroid"]:
        a.append("主体候选质心在 (%.0f%%, %.0f%%)，即%s：构图段写「主体位于%s」，比写「居中构图」准。"
                 "若候选表里有第二候选（面积接近），先人眼确认哪个是主体。"
                 % (sj["centroid"][0] * 100, sj["centroid"][1] * 100, sj["position"], sj["position"]))
    cp = d["composition"]
    a.append("线条倾向：%s（水平线占比 %.2f）；对称性：%s。" % (cp["line_hint"], cp["h_line_share"], cp["symmetry"]))
    if d["text_suspect"]["flag"]:
        a.append("这张图本身可能有文字/水印，反推时**不要**把水印写进正向提示词：先判断它是内容还是叠加物。")
    return a


# ------------------------------------------------------------------ 标注图

def annotate(work, d, out_path, scale_to=720):
    im = work.copy()
    if im.width < scale_to:
        f = scale_to / float(im.width)
        im = im.resize((scale_to, max(1, int(im.height * f))), Image.Resampling.LANCZOS)
    w, h = im.size
    canvas = Image.new("RGB", (w, h + int(h * 0.09)), (250, 250, 250))
    canvas.paste(im, (0, 0))
    dr = ImageDraw.Draw(canvas, "RGBA")

    for i in (1, 2):
        dr.line([(w * i / 3, 0), (w * i / 3, h)], fill=(255, 255, 255, 90), width=1)
        dr.line([(0, h * i / 3), (w, h * i / 3)], fill=(255, 255, 255, 90), width=1)
    dr.rectangle([0, 0, w - 1, h - 1], outline=(255, 255, 255, 140))

    sj = d["subject"]
    if sj.get("bbox"):
        x0, y0, x1, y1 = sj["bbox"]
        dr.rectangle([x0 * w, y0 * h, x1 * w, y1 * h], outline=(255, 214, 0, 230), width=2)
        dr.line([(x0 * w, y0 * h), (x0 * w + 18, y0 * h)], fill=(255, 214, 0, 255), width=3)
    for rg in d.get("regions", [])[1:]:
        if rg.get("bbox"):
            x0, y0, x1, y1 = rg["bbox"]
            dr.rectangle([x0 * w, y0 * h, x1 * w, y1 * h], outline=(255, 140, 0, 180), width=1)
    if sj.get("centroid"):
        cx, cy = sj["centroid"][0] * w, sj["centroid"][1] * h
        dr.line([(cx - 8, cy), (cx + 8, cy)], fill=(255, 60, 60, 230), width=2)
        dr.line([(cx, cy - 8), (cx, cy + 8)], fill=(255, 60, 60, 230), width=2)

    pal = d["palette"][:6] or []
    if pal:
        bw = w / float(len(pal))
        top = h
        for i, p in enumerate(pal):
            dr.rectangle([i * bw, top, (i + 1) * bw, top + h * 0.09], fill=tuple(p["rgb"]))
    dr.text((6, 6), "%s | key/fill %.1f | contrast %.2f | grad %d" %
            (d["aspect"], d["light"]["key_fill_ratio"], d["brightness"]["contrast"],
             d["sharpness"]["score"]), fill=(255, 255, 255))
    canvas.save(out_path)


# ------------------------------------------------------------------ 报告

def write_md(d, path):
    L = []
    L.append("# 反推前量测：%s" % d["file"])
    L.append("")
    L.append("> 先信这里的数字，再决定形容词。所有「看着像」的结论都要能在下面找到一行出处。")
    L.append("")
    L.append("## 1 基本")
    L.append("")
    L.append("| 项 | 值 |")
    L.append("| --- | --- |")
    L.append("| 尺寸 | %d × %d |" % tuple(d["size"]))
    L.append("| 画幅 | %s（%.3f） |" % (d["aspect"], d["aspect_float"]))
    L.append("| 有效像素 | %.1f MP |" % d["megapixels"])
    L.append("| 平均亮度 | %.2f（%s） |" % (d["brightness"]["mean"],
                                             "偏暗" if d["brightness"]["mean"] < 0.35 else
                                             ("偏亮" if d["brightness"]["mean"] > 0.68 else "中间调")))
    L.append("| 反差 | %.2f — %s |" % (d["brightness"]["contrast"], d["brightness"]["label"]))
    L.append("| 高光溢出 / 暗部死黑 | %.1f%% / %.1f%% |" %
             (d["brightness"]["clip_high"] * 100, d["brightness"]["clip_low"] * 100))
    L.append("| 饱和度 | %.2f — %s |" % (d["saturation"]["mean"], d["saturation"]["label"]))
    L.append("| 色温 | %s（R-B %.3f） |" % (d["temperature"]["label"], d["temperature"]["warmth"]))
    L.append("")
    L.append("## 2 光")
    L.append("")
    L.append("| 项 | 值 |")
    L.append("| --- | --- |")
    L.append("| 光位 | **%s** |" % d["light"]["direction"])
    L.append("| 光比（p95:p5） | %.1f : 1 — %s |" % (d["light"]["key_fill_ratio"], d["light"]["quality"]))
    L.append("| 九宫格亮度 | %s |" % " / ".join("%.2f" % v for row in d["light"]["grid"] for v in row))
    L.append("| 逆光嫌疑 | %s |" % ("是" if d["light"]["backlit_suspect"] else "否"))
    L.append("")
    L.append("九宫格读数按行排列（左上、上、右上、左、中、右、左下、下、右下）。")
    L.append("")
    L.append("## 3 色板")
    L.append("")
    L.append("| 色 | hex | 占比 | 角色 | HSV（H/S/V） |")
    L.append("| --- | --- | --- | --- | --- |")
    for p in d["palette"]:
        role = ("主色" if p["share"] >= 0.30 else
                ("次色" if p["share"] >= 0.15 else
                 ("点缀" if p["share"] >= 0.05 else "可忽略")))
        if p["hsv"][2] < 0.25 and p["share"] >= 0.15:
            role = "暗部 / 阴影"
        if p["hsv"][2] > 0.85 and p["hsv"][1] < 0.15 and p["share"] >= 0.15:
            role = "高光 / 白"
        L.append("| %s | `%s` | %.1f%% | %s | %.0f / %.2f / %.2f |" %
                 (p["name"], p["hex"], p["share"] * 100, role, p["hsv"][0], p["hsv"][1], p["hsv"][2]))
    L.append("")
    L.append("## 4 画质")
    L.append("")
    L.append("| 项 | 值 | 说明 |")
    L.append("| --- | --- | --- |")
    L.append("| 锐度 | %d / 100 | %s |" % (d["sharpness"]["score"], d["sharpness"]["label"]))
    L.append("| 景深 | %s | 中心/边缘梯度 %.2f |" % (d["dof"]["label"], d["dof"]["center_edge_ratio"]))
    L.append("| 噪声 | RMS %.2f | %s |" % (d["noise"]["rms"], d["noise"]["label"]))
    L.append("| 暗角 | %s | 四角/中心亮度 %.2f |" % (d["vignette"]["label"], d["vignette"]["corner_to_center"]))
    L.append("")
    L.append("## 5 前景候选与构图")
    L.append("")
    sj = d["subject"]
    L.append("| 项 | 值 |")
    L.append("| --- | --- |")
    L.append("| 建议主体 | %s（置信度 %s） |" % (sj["position"], sj["confidence"]))
    L.append("| 质心 | %s |" % ("(%.0f%%, %.0f%%)" % (sj["centroid"][0] * 100, sj["centroid"][1] * 100)
                                  if sj["centroid"] else "—"))
    L.append("| 面积占比 | %.1f%% |" % (sj["area_share"] * 100))
    L.append("| 对称性 | %s（镜像差 %.3f） |" % (d["composition"]["symmetry"], d["composition"]["mirror_diff"]))
    L.append("| 线条 | %s（水平线占比 %.2f） |" % (d["composition"]["line_hint"], d["composition"]["h_line_share"]))
    L.append("")
    L.append("候选区域（与边带背景差异大的连通块，按面积排序）：")
    L.append("")
    L.append("| 候选 | 位置 | 面积 | 平均色 | 贴边 |")
    L.append("| --- | --- | --- | --- | --- |")
    for i, rg in enumerate(d.get("regions", []), 1):
        L.append("| %d | %s | %.1f%% | `%s` | %s |" %
                 (i, rg["position"], rg["area_share"] * 100, rg["mean_color"],
                  ("%d 边" % rg["borders"]) if rg["borders"] else "不贴边"))
    L.append("")
    L.append("> 候选是基于「与边带背景色差异 + 连通性」估的，不是分割结果；")
    L.append("> 亮源、深影、大面积同色装饰都可能上榜，选哪个当主体由读图的人决定。")
    L.append("")
    L.append("## 6 嫌疑项")
    L.append("")
    ts = d["text_suspect"]
    if ts["flag"]:
        for r in ts["reasons"]:
            L.append("- " + r)
    else:
        L.append("- 未发现明显的文字 / 水印 / 角标迹象（边带梯度比 %.2f / %.2f，角落 %.2f）。"
                 % (ts["top_ratio"], ts["bottom_ratio"], ts["corner_ratio"]))
    L.append("")
    L.append("## 7 转写建议")
    L.append("")
    for i, s in enumerate(d["advice"], 1):
        L.append("%d. %s" % (i, s))
    L.append("")
    L.append("下一步：把上面几条填进 `specs/` 的 JSON，跑 `build_prompt.py`，")
    L.append("出图后用 `verify.py` 拿这张图当基准打分。")
    Path(path).write_text("\n".join(L) + "\n", encoding="utf-8")


# ------------------------------------------------------------------ 主流程

def analyze(path, k=6):
    im, work = K.load_rgb(path)
    w, h = im.size
    ww, hh = work.size
    r, g, b = K.channels(work)
    luma = K.luma_of(r, g, b)
    # 锐度 / 景深 / 构图在 3x3 中值滤波后的亮度上算：颗粒会把梯度均值抬高，
    # 不去噪的话一张胶片质感的图会被判成「极锐 / 像渲染图」
    wm = work.filter(ImageFilter.MedianFilter(3))
    r2, g2, b2 = K.channels(wm)
    luma_s = K.luma_of(r2, g2, b2)
    mag, dx, dy = K.grad_maps(luma_s, ww, hh)
    gm = K.grid_means(mag, ww, hh, 3, 3)
    bg_rgb, bg_pal = border_colors(work)
    regions = foreground_regions(work, bg_rgb)

    d = {
        "file": Path(path).name,
        "size": [w, h],
        "aspect": K.aspect_label(w, h),
        "aspect_float": round(w / float(h), 3),
        "megapixels": round(w * h / 1e6, 2),
        "brightness": brightness_stats(luma),
        "saturation": saturation_stats(r, g, b),
        "temperature": temperature_stats(r, g, b),
        "light": light_stats(luma, ww, hh),
        "palette": K.kmeans_palette(work, k=k),
        "sharpness": sharpness_stats(mag, gm),
        "dof": dof_stats(gm),
        "noise": noise_stats(work, luma, mag),
        "vignette": vignette_stats(luma, ww, hh),
        "background": {"colors": ["#%02x%02x%02x" % c for c in bg_rgb],
                       "shares": [p["share"] for p in bg_pal]},
        "regions": regions,
        "subject": pick_subject(regions),
        "composition": composition_stats(luma, mag, dx, dy, ww, hh),
        "text_suspect": text_suspect(work, mag, ww, hh),
    }
    d["advice"] = build_advice(d)
    return d, work


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="把一张图量成一串能写进提示词的数字（光位 / 色板 / 景深 / 主体位置）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python scripts/probe.py assets/ref.jpg --outdir out/probe\n"
               "  python scripts/probe.py assets/ref.jpg --outdir out/probe --k 7\n"
               "  python scripts/probe.py assets/ref.jpg --outdir out/probe --json-only\n")
    ap.add_argument("image", help="输入图片路径")
    ap.add_argument("--outdir", default="out/probe", help="输出目录（默认 out/probe）")
    ap.add_argument("--k", type=int, default=6, help="色板聚类数，5–8 之间（默认 6）")
    ap.add_argument("--json-only", action="store_true", help="只写 JSON，不写 md / 标注图")
    ap.add_argument("--no-annotate", action="store_true", help="不画标注图")
    args = ap.parse_args(argv)

    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    d, work = analyze(args.image, k=max(3, min(10, args.k)))
    K.dump_json(d, out / "probe.json")

    if not args.json_only:
        write_md(d, out / "probe.md")
        if not args.no_annotate:
            annotate(work, d, out / "probe.png")

    print("[ok] 画幅 %s | 光位 %s | 光比 %.1f | 反差 %.2f | 主体 %s" %
          (d["aspect"], d["light"]["direction"], d["light"]["key_fill_ratio"],
           d["brightness"]["contrast"], d["subject"]["position"]))
    print("[ok] 主色 %s" % "、".join("%s %s(%.0f%%)" % (p["name"], p["hex"], p["share"] * 100)
                                   for p in d["palette"][:4]))
    print("[ok] 输出 %s" % out.resolve())
    if d["text_suspect"]["flag"]:
        print("[warn] 疑似含文字/水印：%s" % "；".join(d["text_suspect"]["reasons"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
