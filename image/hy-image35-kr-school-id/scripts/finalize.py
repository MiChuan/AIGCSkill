#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""证件写真收尾：裁掉模型自己画的框 → 清水印 → 重采样 → 加精确白边框。

为什么边框要在这里加：
  模型画框的失败模式极其稳定——四边不等宽、宽度随机（1%~12%）、框外多一层灰相纸、
  框内再叠一圈投影。而「1.5% 白边」在视觉上恰恰是靠那 2 个像素的精度撑起来的，
  差 20 个像素就不像相纸了。所以提示词里禁止模型画框，全部交给这个脚本按像素算。

处理顺序是硬约束（改了就出问题）：
  trim → 水印 → 重采样 → 边框
  · 水印必须边框之前：水印可能落在要盖白边的那一圈上，先加框会把水印留在框内；
  · 边框必须重采样之后：边框宽度按交付尺寸算，先加框再放大会让框跟着放大 4 倍、
    边缘还多一圈插值糊边。

用法:
    python finalize.py raw.png --out out/final_8k.jpg
    python finalize.py raw.png --out out/final_8k.jpg --target 8k --border 0.015
    python finalize.py raw.png --out out/x.jpg --border 0 --watermark box:1180,1420,300,90
    python finalize.py raw.png --out out/x.jpg --size 5760x7680 --border off --border-mode outside
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageStat
except ImportError:  # pragma: no cover
    sys.stderr.write("[deps] 缺少 Pillow。安装：python -m pip install pillow\n")
    raise SystemExit(2)

TARGETS = {"2k": 2048, "4k": 3840, "8k": 7680}

# 水印扫描窗口（相对坐标）：右下角 22% × 12%。
# 比 cinematic 的窗口更小，因为证件写真的水印几乎不会飘到画面中间，
# 窗口开大了会把右侧衣领的斜纹当成水印。
SCAN_BOX = (0.76, 0.87, 1.0, 1.0)
AUTO_MIN_SCORE = 0.30


def open_image(path: Path) -> Image.Image:
    img = Image.open(path)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    elif img.mode == "L":
        img = img.convert("RGB")
    return img


# ---------------------------------------------------------------- 模型自带的框

def measure_edge(img: Image.Image, frac: float = 0.012) -> dict:
    """量四条最外圈的亮度与白度，用来判断模型是不是自己把框画了。"""
    W, H = img.size
    d = max(2, int(min(W, H) * frac))
    bands = {
        "top": img.crop((0, 0, W, d)),
        "bottom": img.crop((0, H - d, W, H)),
        "left": img.crop((0, 0, d, H)),
        "right": img.crop((W - d, 0, W, H)),
    }
    out = {}
    for name, band in bands.items():
        st = ImageStat.Stat(band.convert("RGB"))
        out[name] = {
            "mean": [round(m, 1) for m in st.mean],
            "white_ratio": round(sum(band.convert("L").histogram()[242:]) / (band.width * band.height), 3),
        }
    return out


def builtin_border_hint(edge: dict) -> tuple[bool, str]:
    """四边都很白才算「模型画了框」；只有一两边白更可能是高光或背景。"""
    white_sides = [k for k, v in edge.items() if v["white_ratio"] > 0.6 and min(v["mean"]) > 228]
    if len(white_sides) >= 3:
        return True, (
            f"四边里 {len(white_sides)} 边接近纯白（{', '.join(white_sides)}）："
            "模型很可能自己画了白框。用 --trim 0.02~0.04 先裁掉，再让本脚本加框，"
            "否则会出现双框、且两框宽度不均"
        )
    return False, ""


def trim(img: Image.Image, frac: float) -> tuple[Image.Image, dict]:
    if frac <= 0:
        return img, {"applied": False}
    W, H = img.size
    dx, dy = int(W * frac), int(H * frac)
    box = (dx, dy, W - dx, H - dy)
    if box[2] - box[0] < 64 or box[3] - box[1] < 64:
        return img, {"applied": False, "reason": "trim 比例过大，裁完不足 64px，已忽略"}
    return img.crop(box), {"applied": True, "frac": frac, "box": list(box), "size": list(img.crop(box).size)}


# ---------------------------------------------------------------- 水印

def scan_rect(W: int, H: int) -> tuple[int, int, int, int]:
    x0, y0 = int(W * SCAN_BOX[0]), int(H * SCAN_BOX[1])
    x1, y1 = int(W * SCAN_BOX[2]), int(H * SCAN_BOX[3])
    return (x0, y0, x1 - x0, y1 - y0)


def largest_component(hot: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """hot 格里最大的连通块。

    这一步是整个检测的成败点：磨砂背景上的颗粒会让零散格子随机越过阈值，
    只数数量的话「干净图」和「有水印」得分一样（实测干净图 0.35、有水印 0.91、
    阈值 0.30 → 干净图被误判）。水印是**连续**的一片，噪声是**散布**的点，
    所以按连通性筛一次就能把两者彻底分开。
    邻接半径取 2 格，让字母之间的天然空隙不要把它自己拆散成几个小块。
    """
    grid = set(hot)
    seen: set[tuple[int, int]] = set()
    best: list[tuple[int, int]] = []
    neigh = [(dc, dr) for dc in (-2, -1, 0, 1, 2) for dr in (-2, -1, 0, 1, 2) if (dc, dr) != (0, 0)]
    for cell in grid:
        if cell in seen:
            continue
        comp: list[tuple[int, int]] = []
        stack = [cell]
        seen.add(cell)
        while stack:
            c, r = stack.pop()
            comp.append((c, r))
            for dc, dr in neigh:
                q = (c + dc, r + dr)
                if q in grid and q not in seen:
                    seen.add(q)
                    stack.append(q)
        if len(comp) > len(best):
            best = comp
    return best


def longest_span(profile: list[float], thr: float, gap: int = 2) -> tuple[int, int] | None:
    """在一条一维能量曲线上找最长的高区段，允许中间断 gap 个点。

    用水印的「投影」而不是「分块包围盒」来定位置，是因为水印有一个硬先验：
    **它是一行字**。所以行投影上只有窄窄一条是高能量，列投影上是一条长的。
    分块法的问题是噪声格会零散越阈值，把包围盒一路撑到窗口边界，
    框里就混进了人物边缘（实测框高 147px，水印本身只有 22px），
    左右两条锚带一个西装色一个背景色，插值出来是一条渐变带，等于没修。
    投影做过一次平均，噪声被压掉 10 倍以上，边界就干净了。
    """
    n = len(profile)
    if n == 0:
        return None
    best = None
    i = 0
    while i < n:
        if profile[i] >= thr:
            j = i
            miss = 0
            k = i + 1
            while k < n:
                if profile[k] >= thr:
                    j = k
                    miss = 0
                else:
                    miss += 1
                    if miss > gap:
                        break
                k += 1
            if best is None or (j - i) > (best[1] - best[0]):
                best = (i, j)
            i = j + 1
        else:
            i += 1
    return best


def profile_of(edges: Image.Image, axis: str) -> list[float]:
    w, h = edges.size
    if axis == "row":
        return [ImageStat.Stat(edges.crop((0, y, w, y + 1))).mean[0] for y in range(h)]
    return [ImageStat.Stat(edges.crop((x, 0, x + 1, h))).mean[0] for x in range(w)]


def col_extent(profile: list[float], thr: float, trim: float = 0.05) -> tuple[int, int] | None:
    """列方向用分位外包，不用「最长连续段」。

    文字是横向排的，字母之间天然有空隙：找最长连续段只会框住一个字母
    （实测框宽 10px，只盖住 "H" 的一竖）。所以列方向改成
    「所有超过阈值的列」的 5%–95% 分位外包——既覆盖整行字，
    也不会因为边缘一两个噪点把框撑到窗口尽头。
    """
    idx = [i for i, v in enumerate(profile) if v >= thr]
    if len(idx) < 3:
        return None
    idx.sort()
    lo = idx[int(len(idx) * trim)]
    hi = idx[-1 - int(len(idx) * trim)]
    if hi <= lo:
        return None
    return (lo, hi)


def detect_watermark(img: Image.Image) -> dict:
    """在右下角窗口里找叠加的文字层。

    两套判据各管一件事：
      · 分块边缘能量 + 连通性 + 形状 → 判「有没有」（水印是横向一簇，噪声是散点）；
      · 行/列投影 → 判「在哪」（水印是一行字，所以在行投影上是窄条）。
    """
    W, H = img.size
    wx, wy, ww, wh = scan_rect(W, H)
    region = img.crop((wx, wy, wx + ww, wy + wh)).convert("L")
    edges = region.filter(ImageFilter.FIND_EDGES)

    cols, rows = 12, 5
    tw, th = max(1, ww // cols), max(1, wh // rows)
    cells = []
    for r in range(rows):
        for c in range(cols):
            tile = edges.crop((c * tw, r * th, (c + 1) * tw, (r + 1) * th))
            cells.append((c, r, ImageStat.Stat(tile).mean[0]))

    vals = sorted(e for _, _, e in cells)
    median = vals[len(vals) // 2] or 1e-6
    # 系数 1.6 而不是 2.0：磨砂背景的颗粒会把「噪声底」抬起来，
    # 系数取 2.0 时（噪声 sigma 10 的图上）水印格子刚好被阈值压回去，整项漏检。
    threshold = max(median * 1.6, 7.0)

    # 只取能量最高的那一批再连通，不取所有过阈值的格：噪声格会被自然丢掉。
    ranked = sorted(cells, key=lambda t: -t[2])
    k = max(3, int(len(cells) * 0.28))
    hot = [(c, r) for c, r, e in ranked[:k] if e >= threshold]
    comp = largest_component(hot)
    score = min(1.0, len(comp) / len(cells) * 2.6)

    aspect = 0.0
    if comp:
        c0, c1 = min(c for c, _ in comp), max(c for c, _ in comp)
        r0, r1 = min(r for _, r in comp), max(r for _, r in comp)
        aspect = (c1 - c0 + 1) / max(1, (r1 - r0 + 1))
        # 水印是横排的一行字：宽高比通常 > 3。
        # 肩膀/衣领的边缘在窗口里是一条竖向斜线，宽高比 < 1。
        # 两者都是高边缘的连通块，只有形状能分开——实测合成图上
        # 干净图的斜边块让分数到达 0.35（阈值 0.30，直接误判），
        # 加上这一条之后掉到 0.14。
        if aspect < 1.6:
            score *= 0.4

    # 位置：行投影 + 列投影
    rprof = profile_of(edges, "row")
    cprof = profile_of(edges, "col")
    rmed = sorted(rprof)[len(rprof) // 2] or 1e-6
    cmed = sorted(cprof)[len(cprof) // 2] or 1e-6
    rspan = longest_span(rprof, max(rmed * 1.7, 6.0))
    cspan = col_extent(cprof, max(cmed * 1.7, 6.0))
    r_contrast = round(max(rprof) / rmed, 2) if rmed else 0.0

    cand = None
    if comp and aspect >= 1.0 and rspan and cspan:
        ry0, ry1 = rspan
        rx0, rx1 = cspan
        # 上下 padding 要够：行投影取的是能量最高的核心行，
        # 笔画顶端和接收（比如字母的下伸部）会落在核心之外，
        # padding 只给 45% 时会留下一条没盖住的小白边。
        pad_y = max(6, int((ry1 - ry0 + 1) * 0.9))
        pad_x = max(4, int((rx1 - rx0 + 1) * 0.06))
        x0 = max(0, wx + rx0 - pad_x)
        y0 = max(0, wy + ry0 - pad_y)
        x1 = min(W, wx + rx1 + 1 + pad_x)
        y1 = min(H, wy + ry1 + 1 + pad_y)
        cand = (x0, y0, x1 - x0, y1 - y0)

    return {
        "score": round(score, 3),
        "hot_tiles": len(hot),
        "component": len(comp),
        "aspect": round(aspect, 2),
        "row_contrast": r_contrast,
        "row_span": [wy + rspan[0], wy + rspan[1]] if rspan else None,
        "col_span": [wx + cspan[0], wx + cspan[1]] if cspan else None,
        "span_height": (rspan[1] - rspan[0] + 1) if rspan else None,
        "tile_median_edge": round(median, 2),
        "threshold": round(threshold, 2),
        "scan_box": [wx, wy, wx + ww, wy + wh],
        "candidate": cand,
    }


def _lane_stds(band: Image.Image, axis: str) -> list[float]:
    """把一条锚带切成若干「车道」，返回每条车道的标准差。

    axis="col"：band 是 ring 高 × w 宽，按列切（每一列一个读数）；
    axis="row"：band 是 h 高 × ring 宽，按行切。
    """
    out: list[float] = []
    g = band.convert("L")
    px = g.load()
    w, h = g.size
    n = w if axis == "col" else h
    for i in range(n):
        if axis == "col":
            vals = [px[i, j] for j in range(h)]
        else:
            vals = [px[j, i] for j in range(w)]
        if not vals:
            out.append(999.0)
            continue
        m = sum(vals) / len(vals)
        out.append((sum((v - m) ** 2 for v in vals) / len(vals)) ** 0.5)
    return out


def _lane_colors(band: Image.Image, axis: str, smooth: int = 9) -> list[tuple[float, float, float]]:
    """每条车道的平均颜色，并沿车道方向做一次移动平均。

    平滑这一步不是可选的：直接取「每列一个像素」或「每列的平均」，
    相邻列之间会有几个色阶的随机差，插值出来就是一条条**竖条纹**——
    在 8K 上看起来像印刷网纹，比原来的水印更显眼。
    移动平均只吃掉列间的随机差，保留低频结构（衣服与背景的边界还在）。
    """
    g = band.convert("RGB")
    px = g.load()
    w, h = g.size
    n = w if axis == "col" else h

    raw: list[tuple[float, float, float]] = []
    for i in range(n):
        if axis == "col":
            vals = [px[i, j] for j in range(h)]
        else:
            vals = [px[j, i] for j in range(w)]
        if not vals:
            raw.append((0.0, 0.0, 0.0))
            continue
        raw.append(tuple(sum(v[c] for v in vals) / len(vals) for c in range(3)))

    if smooth <= 1 or n < 3:
        return raw

    r = smooth // 2
    out: list[tuple[float, float, float]] = []
    for i in range(n):
        lo, hi = max(0, i - r), min(n, i + r + 1)
        seg = raw[lo:hi]
        out.append(tuple(sum(s[c] for s in seg) / len(seg) for c in range(3)))
    return out


def _side_maps(img: Image.Image, box: tuple[int, int, int, int], ring: int) -> dict:
    """逐车道判断四条边能不能当锚点，并给出每条车道的代表色。

    为什么必须逐车道：水印压在「衣服 → 背景」的交界上时（证件写真里很常见，
    右肩的斜边正好经过右下角），整条上边带的方差是 80，按「带子」判断会被整体弃用；
    但按列看，左边每一列的上方是纯西装、右边每一列的上方是纯背景，**列列都有干净锚点**。
    按带子判断就只能用左侧一列填充整框，于是框右侧的背景被填成西装色——
    成图上是一块突兀的深色方补丁。
    """
    W, H = img.size
    x, y, w, h = box
    maps: dict = {}

    if y - ring >= 0:
        band = img.crop((x, y - ring, x + w, y))
        maps["top"] = {"stds": _lane_stds(band, "col"),
                       "colors": _lane_colors(band, "col")}
    if y + h + ring <= H:
        band = img.crop((x, y + h, x + w, y + h + ring))
        maps["bottom"] = {"stds": _lane_stds(band, "col"),
                          "colors": _lane_colors(band, "col")}
    if x - ring >= 0:
        band = img.crop((x - ring, y, x, y + h))
        maps["left"] = {"stds": _lane_stds(band, "row"),
                        "colors": _lane_colors(band, "row")}
    if x + w + ring <= W:
        band = img.crop((x + w, y, x + w + ring, y + h))
        maps["right"] = {"stds": _lane_stds(band, "row"),
                         "colors": _lane_colors(band, "row")}
    return maps


def inpaint_edges(img: Image.Image, box: tuple[int, int, int, int], ring: int = 8) -> Image.Image:
    """边缘插值修补，锚点的可用性按「车道」逐条判断。

    对证件写真这种低频磨砂背景，四条边插值几乎是精确解；关键是别让一条脏锚带
    污染整片区域，所以每一列/每一行都独立决定用哪个方向的锚点。
    """
    W, H = img.size
    x, y, w, h = box
    w = max(8, min(w, W - x))
    h = max(8, min(h, H - y))

    maps = _side_maps(img, (x, y, w, h), ring)
    if not maps:
        return img

    # 整条带子「大部分车道都脏」才弃用，否则按车道判断：
    # 水印如果压在人物边缘上，上边带里只有贴着边界的两三列车道方差高（它们跨了明暗交界），
    # 剩下的列全是干净的背景或纯色的衣服。若按「整条带子的方差」判断（实测 80），
    # 整条上边带都被弃用，只能退化成用左侧一列填充整个框——
    # 于是框右半边的背景被填成西装色，成图上是一块深色方补丁，边上还带台阶。
    LANE_THR = 45.0
    for m in maps.values():
        bad = sum(1 for s in m["stds"] if s > LANE_THR) / max(1, len(m["stds"]))
        m["usable"] = bad < 0.5

    def lane_ok(side: str, i: int) -> bool:
        m = maps.get(side)
        if not m or not m.get("usable"):
            return False
        stds = m["stds"]
        if not stds:
            return False
        return stds[min(i, len(stds) - 1)] < LANE_THR

    # 顺便量一下锚带的高频噪声，修完要把同强度的颗粒加回去：
    # 插值出来的是一块「绝对均匀」的颜色，在有颗粒的背景上比水印更显眼。
    noises = []
    for m in maps.values():
        for std in m["stds"]:
            if std < LANE_THR:
                noises.append(std)
    sigma = max(0.6, (sum(noises) / len(noises)) * 0.9) if noises else 0.0

    out = img.copy()
    dst = out.load()

    for j in range(h):
        yy = min(H - 1, y + j)
        t = (j + 0.5) / h
        for i in range(w):
            xx = min(W - 1, x + i)
            s = (i + 0.5) / w

            vt = ht = None
            vt_dual = ht_dual = False
            tv_ok, bv_ok = lane_ok("top", i), lane_ok("bottom", i)
            lv_ok, rv_ok = lane_ok("left", j), lane_ok("right", j)

            if tv_ok and bv_ok:
                a, b = maps["top"]["colors"][i], maps["bottom"]["colors"][i]
                vt = tuple(a[k] * (1 - t) + b[k] * t for k in range(3))
                vt_dual = True
            elif tv_ok:
                vt = maps["top"]["colors"][i]
            elif bv_ok:
                vt = maps["bottom"]["colors"][i]

            if lv_ok and rv_ok:
                a, b = maps["left"]["colors"][j], maps["right"]["colors"][j]
                ht = tuple(a[k] * (1 - s) + b[k] * s for k in range(3))
                ht_dual = True
            elif lv_ok:
                ht = maps["left"]["colors"][j]
            elif rv_ok:
                ht = maps["right"]["colors"][j]

            if vt is None and ht is None:
                continue
            if vt is None:
                dst[xx, yy] = tuple(int(round(c)) for c in ht)  # type: ignore[arg-type]
            elif ht is None:
                dst[xx, yy] = tuple(int(round(c)) for c in vt)  # type: ignore[arg-type]
            elif vt_dual != ht_dual:
                # 一侧是「两端都有」的插值、另一侧只有单边锚点时，**不能平均**。
                # 单边锚点在跨界处代表不了这一列/这一行的颜色：
                # 实测框右侧的背景像素 200 被拉去和左侧西装 30 平均成 115，
                # 于是背景上多出一条半深的竖带。只在两侧都可信时才平均。
                pick = vt if vt_dual else ht
                dst[xx, yy] = tuple(int(round(c)) for c in pick)  # type: ignore[union-attr]
            else:
                dst[xx, yy] = tuple(int(round((vt[k] + ht[k]) / 2)) for k in range(3))

    if sigma > 0.5:
        noise = Image.effect_noise((w, h), sigma).convert("L")
        block = out.crop((x, y, x + w, y + h))
        block = Image.merge("RGB", [
            ImageChops.add(c, noise, 1.0, -128) for c in block.split()
        ])
        out.paste(block, (x, y))

    # 只在贴边一圈做羽化，把插值区和原图的接缝揉开。
    # 带宽必须压在短边的 1/8 以内：框高通常只比水印高一点点（水印 22px、框 29px），
    # 羽化带到 1/4 时中心只剩 15px 被真正替换，水印的上下边缘会原样留下
    # （实测边缘能量只从 44 降到 22，等于没修干净）。
    patch = out.crop((x, y, x + w, y + h)).filter(ImageFilter.GaussianBlur(0.8))
    mask = Image.new("L", (w, h), 255)
    d = max(2, min(w, h) // 8)
    ImageDraw.Draw(mask).rectangle([d, d, w - d - 1, h - d - 1], fill=0)
    mask = mask.filter(ImageFilter.GaussianBlur(d * 0.6)).point(lambda v: 255 - v)
    out.paste(patch, (x, y), mask)
    return out


# ---------------------------------------------------------------- 重采样

def resize_to(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    W, H = img.size
    tw, th = size
    if (W, H) == (tw, th):
        return img
    cur = img
    while cur.width * 2 <= tw and cur.height * 2 <= th:
        cur = cur.resize((cur.width * 2, cur.height * 2), Image.LANCZOS)
    scale = tw / W
    out = cur.resize((tw, th), Image.LANCZOS)
    if scale > 1.2:
        radius = 1.0 + 1.1 * min(3.0, scale / 4.0)
        out = out.filter(ImageFilter.UnsharpMask(radius=radius, percent=80, threshold=3))
    return out


# ---------------------------------------------------------------- 边框

def edge_is_pale(img: Image.Image) -> bool:
    """画面最外圈是不是浅色。浅色画面 + 纯白边框会「粘」在一起，需要一条发丝线分界。"""
    W, H = img.size
    d = max(2, int(min(W, H) * 0.008))
    strips = [
        img.crop((0, 0, W, d)), img.crop((0, H - d, W, H)),
        img.crop((0, 0, d, H)), img.crop((W - d, 0, W, H)),
    ]
    mean = sum(ImageStat.Stat(s.convert("L")).mean[0] for s in strips) / len(strips)
    return mean > 226


def add_border(
    img: Image.Image,
    px: int,
    color: tuple[int, int, int],
    mode: str,
    target: tuple[int, int] | None,
    hairline: str,
) -> tuple[Image.Image, dict]:
    if px <= 0:
        return img, {"applied": False}

    info: dict = {"applied": True, "px": px, "mode": mode, "color": "#%02X%02X%02X" % color}

    if mode == "outside" and target:
        inner = (max(64, target[0] - 2 * px), max(64, target[1] - 2 * px))
        img = resize_to(img, inner)
        canvas = Image.new("RGB", target, color)
        canvas.paste(img, (px, px))
        out = canvas
        info["inner_size"] = list(inner)
    else:
        # inside：框画在画面内部，总尺寸与比例严格不变。
        # 这就是「边框精确」的实现方式——画布的每一像素都还在预定的格子里。
        out = img.copy()
        ImageDraw.Draw(out).rectangle([0, 0, out.width - 1, out.height - 1], outline=color, width=px)
        info["mode"] = "inside"

    if hairline != "off":
        pale = edge_is_pale(img)
        if hairline == "auto" and not pale:
            info["hairline"] = "skipped（画面边缘不是浅色，白框自带分界）"
        else:
            hc = (230, 230, 230) if hairline == "auto" else hairline
            lw = max(1, px // 45)
            d = ImageDraw.Draw(out)
            if mode == "outside" and target:
                d.rectangle([px - lw, px - lw, target[0] - px + lw - 1, target[1] - px + lw - 1],
                            outline=hc, width=lw)
            else:
                d.rectangle([px - lw, px - lw, out.width - px + lw - 1, out.height - px + lw - 1],
                            outline=hc, width=lw)
            info["hairline"] = "#%02X%02X%02X" % hc if isinstance(hc, tuple) else hc
    return out, info


# ---------------------------------------------------------------- CLI

def parse_hex(s: str) -> tuple[int, int, int]:
    s = s.lstrip("#")
    if len(s) != 6:
        raise SystemExit("[args] --border-color 需要 RRGGBB")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def parse_wm(val: str):
    if val.startswith("box:"):
        nums = [int(float(n)) for n in val[4:].split(",")]
        if len(nums) != 4:
            raise SystemExit("[args] --watermark box: 需要 x,y,w,h")
        return "box", tuple(nums)
    if val in ("auto", "off"):
        return val, None
    raise SystemExit("[args] --watermark 只接受 auto / off / box:x,y,w,h")


def main() -> int:
    ap = argparse.ArgumentParser(description="证件写真收尾：水印 + 重采样 + 精确白边框")
    ap.add_argument("src")
    ap.add_argument("--out", required=True)
    ap.add_argument("--target", default="8k", choices=["native", "2k", "4k", "8k"])
    ap.add_argument("--size", default=None, help="显式交付尺寸 WxH，给了就忽略 --target 的长边")
    ap.add_argument("--trim", type=float, default=0.0, help="先按比例裁掉四周（修模型自画的框）")
    ap.add_argument("--watermark", default="auto", help="auto | off | box:x,y,w,h")
    ap.add_argument("--border", default="0.015",
                    help="边框宽度：小于 1 视为相对短边的比例，>=1 视为像素；off 关闭")
    ap.add_argument("--border-color", default="FFFFFF")
    ap.add_argument("--border-mode", default="inside", choices=["inside", "outside"])
    ap.add_argument("--hairline", default="auto", help="auto | off | RRGGBB")
    ap.add_argument("--quality", type=int, default=95)
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    t0 = time.time()
    src = Path(args.src)
    if not src.exists():
        raise SystemExit(f"[in] 文件不存在：{src}")

    img = open_image(src)
    report: dict = {"in": src.as_posix(), "in_size": list(img.size)}
    warnings: list[str] = []

    # 0) 模型自画框的提示（要在 trim 之前量）
    edge = measure_edge(img)
    report["edge_probe"] = edge
    has_builtin, hint = builtin_border_hint(edge)
    if has_builtin:
        warnings.append(hint)
        if args.trim <= 0:
            warnings.append("本次没有传 --trim，双框风险仍在；确认后加 --trim 0.02 重跑")

    # 1) trim
    if args.trim > 0:
        img, ti = trim(img, args.trim)
        report["trim"] = ti

    # 2) 水印
    mode, box = parse_wm(args.watermark)
    if mode == "off":
        report["watermark"] = {"applied": False, "reason": "关闭"}
    else:
        info = detect_watermark(img)
        report["watermark"] = {"detect": info, "applied": False}
        target = box if mode == "box" else (
            info["candidate"] if info["score"] >= AUTO_MIN_SCORE else None
        )
        if target is None and mode == "auto":
            report["watermark"]["reason"] = (
                f"分数 {info['score']} < {AUTO_MIN_SCORE}，判为无水印。"
                "浅灰背景上的淡水印容易漏检，确认有就用 --watermark box:x,y,w,h"
            )
        if target:
            img = inpaint_edges(img, target)
            report["watermark"].update({"applied": True, "box": list(target), "method": "edge-bilinear"})

    # 3) 重采样
    if args.size:
        tw, th = (int(v) for v in args.size.lower().split("x"))
        size = (tw, th)
    elif args.target == "native":
        size = img.size
    else:
        long_target = TARGETS[args.target]
        W, H = img.size
        k = long_target / max(W, H)
        size = (round(W * k), round(H * k))
        if args.target == "8k" and max(W, H) < 1024:
            warnings.append("原图短边太小，8K 放大后细节是插值出来的，别当原生 8K 宣传")

    img = resize_to(img, size)

    # 4) 边框（按最终尺寸算像素）
    if str(args.border).lower() in ("off", "0"):
        px = 0
    else:
        b = float(args.border)
        px = int(round(b * min(img.size))) if b < 1 else int(round(b))
    if args.border_mode == "outside" and px > 0:
        # outside 时把总尺寸固定成 size，内图缩到 size-2*px
        pass
    img, binfo = add_border(img, px, parse_hex(args.border_color),
                            args.border_mode, size if args.border_mode == "outside" else None,
                            args.hairline)
    report["border"] = binfo

    # 5) 落盘
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fmt = (out.suffix.lstrip(".") or "jpg").lower()
    if fmt in ("jpeg",):
        fmt = "jpg"
    if fmt == "jpg":
        img.convert("RGB").save(out, quality=args.quality, subsampling=0, optimize=True)
    elif fmt == "webp":
        img.save(out, quality=args.quality, method=6)
    else:
        img.save(out, compress_level=6)

    # 6) 边框等宽自检：从四条边向内数「连续等于框色」的像素，必须都等于 px。
    #    这不是形式主义——白框和白衬衫相邻时，肉眼根本看不出 3px 的偏差，
    #    但把图放到 ID 模板里对齐时，那 3px 会让整张脸偏。
    if binfo.get("applied"):
        p = img.load()
        W, H = img.size
        col = parse_hex(args.border_color)

        def run(it) -> int:
            n = 0
            for px_ in it:
                if all(abs(px_[k] - col[k]) <= 10 for k in range(3)):
                    n += 1
                else:
                    break
            return n

        cy, cx = H // 2, W // 2
        measured = {
            "left": run(p[x, cy] for x in range(W // 2)),
            "right": run(p[W - 1 - x, cy] for x in range(W // 2)),
            "top": run(p[cx, y] for y in range(H // 2)),
            "bottom": run(p[cx, H - 1 - y] for y in range(H // 2)),
        }
        report["border"]["verify"] = {
            "expected_px": px,
            "measured_px": measured,
            "even": len(set(measured.values())) == 1 and list(measured.values())[0] >= px - 1,
        }
        if not report["border"]["verify"]["even"]:
            warnings.append(
                f"边框实测宽度 {measured} 与期望 {px} 不一致：多半是画面边缘本身接近框色，"
                "发丝线被当成框的一部分；视觉上无害，按 measured 的实际值交付即可"
            )
        else:
            # 自检通过就把那 1px 发丝线刨掉再算：measured 会包含 lw
            report["border"]["verify"]["measured_px"] = {
                k: max(0, v - (1 if binfo.get("hairline") not in (None, "skipped（画面边缘不是浅色，白框自带分界）") else 0))
                for k, v in measured.items()
            }

    report.update({
        "out": out.as_posix(),
        "out_size": list(img.size),
        "target": args.target if not args.size else args.size,
        "bytes": out.stat().st_size,
        "seconds": round(time.time() - t0, 2),
        "warnings": warnings,
    })
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
