#!/usr/bin/env python3
"""probe.py 与 verify.py 共用的量测口径。

两个脚本必须用同一套算法量同一张图，否则「反推」出来的数字和「还原校验」出来的
数字会互相打架，人就不知道该信哪一个。这个模块只做这件事。

依赖：Pillow。
"""

from __future__ import annotations

import json
import math
from math import gcd
from pathlib import Path

from PIL import Image, ImageOps

# ---------------------------------------------------------------- 读图

def load_rgb(path, max_side=480):
    """返回 (原尺寸图, 工作副本)。工作副本长边不超过 max_side，所有量测都在它上面做。"""
    im = ImageOps.exif_transpose(Image.open(path))
    im = im.convert("RGB")
    w, h = im.size
    if max(w, h) > max_side:
        s = max_side / float(max(w, h))
        work = im.resize((max(1, round(w * s)), max(1, round(h * s))), Image.Resampling.LANCZOS)
    else:
        work = im.copy()
    return im, work


def channels(work):
    data = work.tobytes()
    return data[0::3], data[1::3], data[2::3]


_LR = [0.2126 * i / 255.0 for i in range(256)]
_LG = [0.7152 * i / 255.0 for i in range(256)]
_LB = [0.0722 * i / 255.0 for i in range(256)]


def luma_of(r, g, b):
    lr, lg, lb = _LR, _LG, _LB
    return [lr[ri] + lg[gi] + lb[bi] for ri, gi, bi in zip(r, g, b)]


def quantiles(vals, qs=(0.05, 0.5, 0.95)):
    s = sorted(vals)
    n = len(s)
    out = []
    for q in qs:
        i = int(q * (n - 1) + 0.5)
        out.append(s[max(0, min(n - 1, i))])
    return out


def mean(vals):
    return sum(vals) / float(len(vals)) if vals else 0.0


# ---------------------------------------------------------------- 网格统计

def grid_means(vals, w, h, gx=3, gy=3):
    """把一维数值图按 gx*gy 切块求均值，返回 [[...]]，行优先（左上开始）。"""
    acc = [[0.0] * gx for _ in range(gy)]
    for j in range(gy):
        y0, y1 = j * h // gy, max(j * h // gy + 1, (j + 1) * h // gy)
        y1 = min(h, y1)
        for i in range(gx):
            x0 = i * w // gx
            x1 = min(w, max(x0 + 1, (i + 1) * w // gx))
            s = 0.0
            c = 0
            for y in range(y0, y1):
                base = y * w
                s += sum(vals[base + x0: base + x1])
                c += x1 - x0
            acc[j][i] = s / c if c else 0.0
    return acc


def grad_maps(vals, w, h):
    """简化 Sobel：返回 (幅值, |水平差|, |垂直差|)。

    用 |dx|+|dy| 而不是 hypot，是为了在纯 Python 里少一次开方——
    这套数字只用来比较相对强弱（网格之间、图与图之间），不需要精确到角度。
    """
    n = w * h
    mag = [0.0] * n
    dx = [0.0] * n
    dy = [0.0] * n
    for y in range(1, h - 1):
        base = y * w
        for x in range(1, w - 1):
            i = base + x
            vy = vals[i + w] - vals[i - w]
            vx = vals[i + 1] - vals[i - 1]
            dy[i] = -vy if vy < 0 else vy
            dx[i] = -vx if vx < 0 else vx
            mag[i] = dy[i] + dx[i]
    return mag, dx, dy


# ---------------------------------------------------------------- 色板

def kmeans_palette(work, k=6, iters=14, shrink=4):
    """在缩小的副本上 k-means，返回带 hex / hsv / 中文色的色板。"""
    small = work.resize(
        (max(16, work.width // shrink), max(16, work.height // shrink)),
        Image.Resampling.BILINEAR,
    )
    r, g, b = channels(small)
    px = list(zip(r, g, b))
    out = kmeans_points(px, k=k, iters=iters)
    for p in out:
        cr, cg, cb = p["rgb"]
        p["hex"] = "#%02x%02x%02x" % (cr, cg, cb)
        p["hsv"] = [round(v, 3) for v in rgb_to_hsv(cr, cg, cb)]
        p["name"] = color_name(cr, cg, cb)
    return out


def kmeans_points(px, k=6, iters=14):
    """通用 k-means（按亮度等分初始化，结果确定可复现）。px 为 [(r,g,b), ...]。"""
    if not px:
        return []
    k = max(2, min(k, len(px)))
    px.sort(key=lambda p: 0.2126 * p[0] + 0.7152 * p[1] + 0.0722 * p[2])
    n = len(px)
    centers = [px[min(n - 1, int((i + 0.5) * n / k))] for i in range(k)]

    for _ in range(iters):
        acc = [[0, 0, 0, 0] for _ in range(k)]
        for pr, pg, pb in px:
            best, bd = 0, 1 << 30
            for ci, (cr, cg, cb) in enumerate(centers):
                d = (pr - cr) ** 2 + (pg - cg) ** 2 + (pb - cb) ** 2
                if d < bd:
                    bd, best = d, ci
            a = acc[best]
            a[0] += pr
            a[1] += pg
            a[2] += pb
            a[3] += 1
        new = []
        for ci in range(k):
            a = acc[ci]
            new.append((a[0] // a[3], a[1] // a[3], a[2] // a[3]) if a[3] else centers[ci])
        if new == centers:
            break
        centers = new

    # 真实占比按最终中心再分一次
    counts = [0] * k
    for pr, pg, pb in px:
        best, bd = 0, 1 << 30
        for ci, (cr, cg, cb) in enumerate(centers):
            d = (pr - cr) ** 2 + (pg - cg) ** 2 + (pb - cb) ** 2
            if d < bd:
                bd, best = d, ci
        counts[best] += 1

    total = float(sum(counts)) or 1.0
    out = []
    for (cr, cg, cb), c in zip(centers, counts):
        out.append({"rgb": [cr, cg, cb], "share": round(c / total, 4)})
    out.sort(key=lambda d: -d["share"])
    return out


def rgb_to_hsv(r, g, b):
    r, g, b = r / 255.0, g / 255.0, b / 255.0
    mx, mn = max(r, g, b), min(r, g, b)
    d = mx - mn
    if d == 0:
        h = 0.0
    elif mx == r:
        h = (60 * ((g - b) / d)) % 360
    elif mx == g:
        h = 60 * ((b - r) / d) + 120
    else:
        h = 60 * ((r - g) / d) + 240
    s = 0.0 if mx == 0 else d / mx
    return h, s, mx


_NAMED = [
    ("黑", (16, 16, 16)), ("深灰", (58, 58, 58)), ("中灰", (120, 120, 120)),
    ("浅灰", (185, 185, 185)), ("白", (246, 246, 246)),
    ("暗红", (110, 30, 35)), ("红", (198, 42, 42)), ("朱", (222, 88, 52)),
    ("橙", (228, 138, 42)), ("土黄", (186, 148, 62)), ("黄", (232, 206, 74)),
    ("黄绿", (152, 190, 68)), ("绿", (62, 148, 76)), ("墨绿", (38, 82, 58)),
    ("青", (64, 186, 184)), ("蓝", (46, 92, 196)), ("靛", (48, 52, 122)),
    ("紫", (136, 72, 168)), ("品红", (206, 62, 148)), ("棕", (118, 78, 48)),
    ("卡其", (172, 152, 112)), ("米", (228, 214, 184)), ("肤", (222, 182, 152)),
]


def color_name(r, g, b):
    best, bd = "灰", 1 << 30
    for name, (nr, ng, nb) in _NAMED:
        d = (r - nr) ** 2 + (g - ng) ** 2 + (b - nb) ** 2
        if d < bd:
            bd, best = d, name
    h, s, v = rgb_to_hsv(r, g, b)
    if s < 0.10:
        return best
    if v < 0.30 and not best.startswith("暗") and not best.startswith("墨"):
        return "暗" + best
    if v > 0.82 and s < 0.35 and best not in ("白", "米", "肤"):
        return "浅" + best
    return best


# ---------------------------------------------------------------- 画幅

_STD = [(1, 1), (4, 5), (5, 4), (2, 3), (3, 2), (3, 4), (4, 3),
        (9, 16), (16, 9), (21, 9), (9, 21), (16, 10), (10, 16)]


def aspect_label(w, h):
    r = w / float(h)
    for a, b in _STD:
        if abs(r - a / float(b)) / (a / float(b)) < 0.02:
            gg = gcd(a, b)
            return "%d:%d" % (a // gg, b // gg)
    gg = gcd(w, h)
    if w // gg <= 24 and h // gg <= 24:
        return "%d:%d" % (w // gg, h // gg)
    best = min(_STD, key=lambda t: abs(r - t[0] / float(t[1])))
    return "~%d:%d" % best


# ---------------------------------------------------------------- 杂项

def dump_json(obj, path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def level(value, cuts, labels):
    """按阈值把数值映射成标签。cuts 升序，labels 比 cuts 多一个。"""
    for c, lab in zip(cuts, labels):
        if value < c:
            return lab
    return labels[-1]


def safe_div(a, b, default=0.0):
    return a / b if b else default
