#!/usr/bin/env python3
"""合成一张演示图，让 examples/ 在没有外部素材的情况下也能从头跑通。

不是「示意图」——它的每一项都在 probe.py 的量程内可验证：
右上有太阳（光位应判右上）、地面压暗（下三分暗）、人形剪影在左下（主体应判左下）、
加了一点暗角和颗粒（噪声与暗角两项应有读数）。

用法：
    python scripts/make_demo.py --out assets/demo-ref.png [--size 1200x800]
"""

from __future__ import annotations

import argparse
import math
import random
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter


def vgrad(w, h, stops):
    """竖直渐变。stops = [(位置0-1, (r,g,b)), ...]，位置升序。"""
    col = Image.new("RGB", (1, h))
    px = col.load()
    for y in range(h):
        t = y / float(max(1, h - 1))
        lo = stops[0]
        hi = stops[-1]
        for i in range(len(stops) - 1):
            if stops[i][0] <= t <= stops[i + 1][0]:
                lo, hi = stops[i], stops[i + 1]
                break
        span = (hi[0] - lo[0]) or 1e-6
        f = (t - lo[0]) / span
        px[0, y] = tuple(int(round(lo[1][c] + (hi[1][c] - lo[1][c]) * f)) for c in range(3))
    return col.resize((w, h), Image.Resampling.BILINEAR)


def dunes(w, h, layers):
    """画几层沙丘，返回 (背景图, 前景图)。"""
    base = Image.new("RGB", (w, h), (0, 0, 0))
    dr = ImageDraw.Draw(base)
    for idx, (y_base, amp, phase, color) in enumerate(layers):
        pts = [(0, h)]
        for x in range(0, w + 8, 8):
            y = y_base + amp * math.sin(x / float(w) * math.pi * 1.6 + phase) \
                + amp * 0.35 * math.sin(x / float(w) * math.pi * 5.1 + phase * 2)
            pts.append((x, y))
        pts.append((w, h))
        dr.polygon(pts, fill=color)
    return base


def figure(dr, x, y, scale, color):
    """一个站姿剪影。y 是脚底。"""
    hh = 120 * scale
    dr.ellipse([x - 9 * scale, y - hh, x + 9 * scale, y - hh + 20 * scale], fill=color)
    dr.polygon([(x - 11 * scale, y - hh + 18 * scale), (x + 11 * scale, y - hh + 18 * scale),
                (x + 9 * scale, y - hh * 0.52), (x - 9 * scale, y - hh * 0.52)], fill=color)
    dr.line([(x - 5 * scale, y - hh * 0.52), (x - 8 * scale, y)], fill=color, width=int(7 * scale))
    dr.line([(x + 5 * scale, y - hh * 0.52), (x + 7 * scale, y)], fill=color, width=int(7 * scale))
    dr.line([(x - 10 * scale, y - hh * 0.80), (x - 16 * scale, y - hh * 0.60)],
            fill=color, width=int(5 * scale))
    dr.line([(x + 10 * scale, y - hh * 0.80), (x + 17 * scale, y - hh * 0.62)],
            fill=color, width=int(5 * scale))


def vignette_mask(w, h, strength=0.34):
    small = Image.new("L", (max(8, w // 8), max(8, h // 8)), 0)
    px = small.load()
    sw, sh = small.size
    for y in range(sh):
        for x in range(sw):
            nx = (x / float(sw - 1) - 0.5) * 2
            ny = (y / float(sh - 1) - 0.5) * 2
            r = min(1.0, math.hypot(nx, ny) / 1.35)
            px[x, y] = int(255 * (1 - strength * r ** 2))
    return small.resize((w, h), Image.Resampling.BICUBIC)


def build(out_path, w=1200, h=800, seed=7):
    random.seed(seed)
    jx = random.uniform(-0.03, 0.03)
    jy = random.uniform(-0.03, 0.03)
    ph = random.uniform(-0.6, 0.6)
    sky = vgrad(w, h, [
        (0.00, (34, 46, 92)),
        (0.22, (86, 88, 138)),
        (0.42, (196, 136, 118)),
        (0.56, (240, 168, 96)),
        (0.66, (250, 206, 132)),
        (1.00, (252, 218, 158)),
    ])

    # 太阳：右上三分
    sun = Image.new("RGB", (w, h), (0, 0, 0))
    sdr = ImageDraw.Draw(sun)
    sx, sy = int(w * (0.72 + jx)), int(h * (0.36 + jy))
    sr = int(w * 0.045)
    sdr.ellipse([sx - sr, sy - sr, sx + sr, sy + sr], fill=(255, 236, 196))
    glow = sun.filter(ImageFilter.GaussianBlur(sr * 1.6))
    img = Image.blend(sky, Image.blend(sky, glow, 0.55), 0.55)

    ground = dunes(w, h, [
        (h * 0.62, h * 0.035, 0.4 + ph, (154, 106, 74)),
        (h * 0.74, h * 0.030, 2.1 + ph, (96, 64, 50)),
        (h * 0.88, h * 0.022, 4.0 + ph, (46, 32, 28)),
    ])
    mask = Image.new("L", (w, h), 0)
    mdr = ImageDraw.Draw(mask)
    mdr.polygon([(0, h), (0, h * 0.63)] +
                [(x, h * 0.63 + h * 0.035 * math.sin(x / float(w) * math.pi * 1.6 + 0.4 + ph))
                 for x in range(0, w + 8, 8)] + [(w, h)], fill=255)
    img.paste(ground, (0, 0), mask)

    dr = ImageDraw.Draw(img)
    figure(dr, int(w * (0.27 + jx)), int(h * (0.895 + jy)), 2.1, (8, 6, 8))

    img = Image.composite(img, Image.new("RGB", (w, h), (0, 0, 0)), vignette_mask(w, h))
    grain = Image.effect_noise((w, h), 20).convert("L").point(lambda v: int(v * 0.4))
    grain = Image.merge("RGB", (grain, grain, grain))
    img = ImageChops.add(img, grain, scale=1.0, offset=-51)

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return out_path


def main(argv=None):
    ap = argparse.ArgumentParser(description="合成 examples 用的演示图（黄昏沙丘剪影）。")
    ap.add_argument("--out", default="assets/demo-ref.png", help="输出路径")
    ap.add_argument("--size", default="1200x800", help="宽x高，默认 1200x800")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(argv)
    try:
        w, h = [int(v) for v in args.size.lower().split("x")]
    except ValueError:
        sys.exit("--size 要走 WxH 格式，比如 1200x800")
    p = build(args.out, w, h, args.seed)
    print("[ok] 写出 %s（%dx%d）" % (p, w, h))
    return 0


if __name__ == "__main__":
    sys.exit(main())
