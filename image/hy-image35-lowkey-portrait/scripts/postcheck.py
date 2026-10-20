#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""低调人像成图体检 + 收尾：背景纯黑校验 / 右下角水印清除 / 8K 重采样。

依赖 Pillow（pip install pillow）。

用法:
    python postcheck.py in.png --out out.jpg --target 8k
    python postcheck.py in.png --out out.jpg --watermark off --target native
    python postcheck.py in.png --out out.jpg --watermark box:1600,980,400,150 --strict

退出码：0 正常；2 用了 --strict 且背景不纯。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageOps, ImageStat
except ImportError:  # pragma: no cover
    sys.stderr.write("[deps] 缺少 Pillow。安装：python -m pip install pillow\n")
    raise SystemExit(2)

SIZES = {"2k": 2048, "4k": 3840, "8k": 7680}

# 右下角水印扫描窗口（相对坐标）
SCAN_BOX = (0.75, 0.86, 1.0, 1.0)
AUTO_MIN_SCORE = 0.30

# 背景采样带（人物在右、拖影在中部，只采该纯黑的边角）：(x0, y0, x1, y1) 相对坐标
BG_BANDS = [
    ("left", (0.0, 0.0, 0.06, 1.0)),
    ("top", (0.0, 0.0, 0.50, 0.05)),
    ("bottom_left", (0.0, 0.96, 0.30, 1.0)),
]

BG_MEAN_MAX = 6.0      # 平均亮度超此值 → 有灰雾或环境光漏入（纯黑应为 0–3）
BG_P99_MAX = 30.0      # 99 分位超此值 → 有高光杂物、飞尘或漏光
BG_DARK_MIN = 0.90     # 接近全黑（L<9）的像素占比下限


def open_image(path: Path) -> Image.Image:
    img = Image.open(path)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    return img


# ---------- 背景纯黑校验 ----------

def check_background(img: Image.Image) -> dict:
    W, H = img.size
    gray = img.convert("L")
    bands = []
    means, p99s, black_ratios = [], [], []

    for name, (rx0, ry0, rx1, ry1) in BG_BANDS:
        box = (int(W * rx0), int(H * ry0), int(W * rx1), int(H * ry1))
        region = gray.crop(box)
        if region.width < 2 or region.height < 2:
            continue
        stat = ImageStat.Stat(region)
        mean = stat.mean[0]
        hist = region.histogram()
        total = sum(hist) or 1
        acc, p99 = 0, 255
        for v, cnt in enumerate(hist):
            acc += cnt
            if acc / total >= 0.99:
                p99 = v
                break
        dark = sum(hist[:9]) / total
        bands.append({"band": name, "box": list(box), "mean": round(mean, 2),
                      "p99": p99, "max": stat.extrema[0][1], "dark_ratio": round(dark, 4)})
        means.append(mean)
        p99s.append(p99)
        black_ratios.append(dark)

    if not bands:
        return {"verdict": "unknown", "reason": "采样带太小"}

    bg_mean = max(means)
    bg_p99 = max(p99s)
    dark_ratio = sum(black_ratios) / len(black_ratios)

    problems = []
    if bg_mean > BG_MEAN_MAX:
        problems.append(f"平均亮度 {bg_mean:.1f} > {BG_MEAN_MAX}：背景有灰雾或环境光漏入")
    if bg_p99 > BG_P99_MAX:
        problems.append(f"99 分位亮度 {bg_p99} > {BG_P99_MAX}：背景有高光杂物、飞尘或漏光")
    if dark_ratio < BG_DARK_MIN:
        problems.append(f"仅 {dark_ratio:.0%} 的像素接近全黑（应 ≥ {BG_DARK_MIN:.0%}）：背景被雾化或压不实")

    return {
        "verdict": "clean" if not problems else "impure",
        "bg_mean": round(bg_mean, 2),
        "bg_p99": bg_p99,
        "black_ratio": round(dark_ratio, 4),
        "problems": problems,
        "bands": bands,
    }


# ---------- 水印 ----------

def scan_rect(W: int, H: int) -> tuple[int, int, int, int]:
    x0 = int(W * SCAN_BOX[0])
    y0 = int(H * SCAN_BOX[1])
    return (x0, y0, int(W * SCAN_BOX[2]) - x0, int(H * SCAN_BOX[3]) - y0)


def detect(img: Image.Image) -> dict:
    """右下角分块边缘能量检测：文字/logo 的边缘远高于纯黑或渐变的基线。

    纯黑背景（窗口内 >80% 像素接近全黑）时，任何热块都是异常，
    改用「热块绝对数量」打分——否则贴角的小 logo 占比太小会被漏掉。
    """
    W, H = img.size
    wx, wy, ww, wh = scan_rect(W, H)
    region = img.crop((wx, wy, wx + ww, wy + wh)).convert("L")
    edges = region.filter(ImageFilter.FIND_EDGES)

    hist = region.histogram()
    total = sum(hist) or 1
    dark_ratio = sum(hist[:12]) / total
    near_white = sum(hist[230:]) / total
    black_bg = dark_ratio > 0.80

    cols, rows = 12, 5
    tw, th = max(1, ww // cols), max(1, wh // rows)
    energies = []
    for r in range(rows):
        for c in range(cols):
            tile = edges.crop((c * tw, r * th, (c + 1) * tw, (r + 1) * th))
            energies.append((c, r, ImageStat.Stat(tile).mean[0]))

    vals = sorted(e for _, _, e in energies)
    median = vals[len(vals) // 2] or 1e-6
    threshold = max(median * 2.2, 8.0)
    hot = [(c, r) for c, r, e in energies if e >= threshold]

    # 低照度人像的水印是「白字压在黑底上」：热块数量 + 近白像素占比两个信号都算分。
    # 只算占比会漏掉贴角小 logo（占窗口面积太小），所以对热块数量做绝对量加权。
    score = min(1.0, len(hot) / 9.0 + min(0.5, near_white * 60.0))

    cand = None
    if 0 < len(hot) < len(energies) * 0.45:
        c0 = min(c for c, _ in hot)
        c1 = max(c for c, _ in hot)
        r0 = min(r for _, r in hot)
        r1 = max(r for _, r in hot)
        # 只外扩半块，让框紧贴文字，避免把旁边的主体边缘卷进补片区
        pad_x, pad_y = max(6, tw // 2), max(6, th // 2)
        cx0 = max(0, wx + c0 * tw - pad_x)
        cy0 = max(0, wy + r0 * th - pad_y)
        cx1 = min(W, wx + (c1 + 1) * tw + pad_x)
        cy1 = min(H, wy + (r1 + 1) * th + pad_y)
        cand = (cx0, cy0, cx1 - cx0, cy1 - cy0)

    return {"score": round(score, 3), "hot_tiles": len(hot), "black_bg": black_bg,
            "window_dark_ratio": round(dark_ratio, 3), "window_near_white": round(near_white, 4),
            "tile_median_edge": round(median, 2), "threshold": round(threshold, 2),
            "scan_box": [wx, wy, ww, wh], "candidate": cand}


def snap_corner(box: tuple[int, int, int, int], size: tuple[int, int]) -> tuple[int, int, int, int]:
    W, H = size
    x, y, _, _ = box
    x2 = max(0, min(x, W - 1))
    y2 = max(0, min(y, H - 1))
    return (x2, y2, W - x2, H - y2)


def _shift(img: Image.Image, offset: tuple[float, float, float]) -> Image.Image:
    chans = img.convert("RGB").split()
    out = []
    for ch, o in zip(chans, offset):
        oi = int(round(o))
        if oi:
            ch = ch.point([max(0, min(255, v + oi)) for v in range(256)])
        out.append(ch)
    return Image.merge("RGB", out)


def _clean_strip(img: Image.Image, box) -> Image.Image | None:
    """取一条紧贴边界的 2px 条带；若被水印（近白）污染则返回 None。"""
    st = img.crop(box)
    if st.width < 2 or st.height < 2:
        return None
    if st.convert("L").getextrema()[1] > 210:
        return None
    return st


def edge_fill(img: Image.Image, box: tuple[int, int, int, int]) -> Image.Image | None:
    """纯黑/近黑背景的补片来源：取四条边界条带里**最暗**的一条，整块铺平。

    暗调人像里水印永远压在黑底上，而贴边的主体（肩、发）会把相邻条带抬亮；
    取最暗条带 = 取真正的背景色，铺平后黑底上完全看不出来。
    """
    x, y, w, h = box
    W, H = img.size
    cands = []
    for strip_box in (
        (x, y - 3, x + w, y - 1) if y - 3 >= 0 else None,
        (x, y + h + 1, x + w, y + h + 3) if y + h + 3 <= H else None,
        (x - 3, y, x - 1, y + h) if x - 3 >= 0 else None,
        (x + w + 1, y, x + w + 3, y + h) if x + w + 3 <= W else None,
    ):
        if strip_box is None:
            continue
        st = _clean_strip(img, strip_box)
        if st is None:
            continue
        cands.append((ImageStat.Stat(st.convert("L")).mean[0], st))

    if not cands:
        return None
    cands.sort(key=lambda t: t[0])
    strip_mean, strip = cands[0]
    color = ImageStat.Stat(strip.convert("RGB")).median
    return Image.new("RGB", (w, h), tuple(int(round(c)) for c in color))


def linear_inpaint(region: Image.Image, mask: Image.Image, clean_limit: int = 999,
                   search: int = 24) -> Image.Image:
    """沿行、沿列各做一次线性插值，再取平均。

    文字水印是细长笔画、分布在平滑背景上：用左右/上下最近的**干净**像素做线性插值，
    能顺着原有明暗过渡补上。找锚点时会跳过仍比背景亮的像素（水印的抗锯齿光晕），
    否则会拿白光晕当端点，插出一条横贯画面的亮带。
    """
    W, H = region.size
    src = region.load()
    m = mask.load()

    def lum(px):
        return (px[0] * 299 + px[1] * 587 + px[2] * 114) // 1000

    hor = region.copy()
    hpx = hor.load()
    for y in range(H):
        x = 0
        while x < W:
            if m[x, y] > 128:
                start = x
                while x < W and m[x, y] > 128:
                    x += 1
                end = x - 1
                li = start - 1
                k = 0
                while li >= 0 and k < search and lum(src[li, y]) > clean_limit:
                    li -= 1
                    k += 1
                ri = end + 1
                k = 0
                while ri < W and k < search and lum(src[ri, y]) > clean_limit:
                    ri += 1
                    k += 1
                left = src[li, y] if li >= 0 and lum(src[li, y]) <= clean_limit else None
                right = src[ri, y] if ri < W and lum(src[ri, y]) <= clean_limit else None
                span = (ri - li) or 1
                for xi in range(start, end + 1):
                    if left and right:
                        t = (xi - li) / span
                        hpx[xi, y] = tuple(int(round(left[c] * (1 - t) + right[c] * t)) for c in range(3))
                    else:
                        hpx[xi, y] = left or right or (0, 0, 0)
            else:
                x += 1

    ver = region.copy()
    vpx = ver.load()
    for x in range(W):
        y = 0
        while y < H:
            if m[x, y] > 128:
                start = y
                while y < H and m[x, y] > 128:
                    y += 1
                end = y - 1
                ti = start - 1
                k = 0
                while ti >= 0 and k < search and lum(src[x, ti]) > clean_limit:
                    ti -= 1
                    k += 1
                bi = end + 1
                k = 0
                while bi < H and k < search and lum(src[x, bi]) > clean_limit:
                    bi += 1
                    k += 1
                top = src[x, ti] if ti >= 0 and lum(src[x, ti]) <= clean_limit else None
                bottom = src[x, bi] if bi < H and lum(src[x, bi]) <= clean_limit else None
                span = (bi - ti) or 1
                for yi in range(start, end + 1):
                    if top and bottom:
                        t = (yi - ti) / span
                        vpx[x, yi] = tuple(int(round(top[c] * (1 - t) + bottom[c] * t)) for c in range(3))
                    else:
                        vpx[x, yi] = top or bottom or (0, 0, 0)
            else:
                y += 1

    out = region.copy()
    opx = out.load()
    for y in range(H):
        for x in range(W):
            if m[x, y] > 128:
                a, b = hpx[x, y], vpx[x, y]
                opx[x, y] = ((a[0] + b[0]) // 2, (a[1] + b[1]) // 2, (a[2] + b[2]) // 2)
    return out


def patch(img: Image.Image, box: tuple[int, int, int, int]) -> Image.Image:
    W, H = img.size
    x, y, w, h = box
    w = max(24, min(w, W - x))
    h = max(12, min(h, H - y))

    gap = max(8, w // 10)
    sx = x - gap - w
    if sx < 0:
        sx = min(W - w, x + w + gap)
    if sx < 0 or sx + w > W:
        sx = max(0, x - w)
        src = img.crop((sx, y, sx + w, y + h))
    else:
        src = ImageOps.mirror(img.crop((sx, y, sx + w, y + h)))

    ring = 8
    outer = img.crop((max(0, x - ring), max(0, y - ring),
                      min(W, x + w + ring), min(H, y + h + ring))).convert("RGB")
    ring_med = ImageStat.Stat(outer).median
    dark_scene = sum(ring_med) / 3.0 < 10.0

    region = img.crop((x, y, x + w, y + h)).convert("RGB")
    region_l = region.convert("L")
    rh = region_l.histogram()
    total = max(1, sum(rh))
    dark_content = sum(rh[:40]) / total

    if dark_scene:
        # 暗调场景：只修水印那几个字，周围像素一个都不动（方框/亮块/缺口都是这么来的）
        bg_level = ring_med[0]
        # 阈值要压得够低：水印的**抗锯齿边缘**（灰度介于字与底之间）漏掉就会留下"空心字"轮廓。
        # 稍微多框一点无所谓——填充是插值，多框的像素会被邻居正确续上。
        thr = int(min(220, max(38, bg_level + 20)))
        mask = region_l.point(lambda v: 255 if v >= thr else 0).filter(ImageFilter.MaxFilter(15))
        mask_ratio = sum(mask.histogram()[128:]) / total

        if 0.002 < mask_ratio < 0.45:
            fixed = linear_inpaint(region, mask, clean_limit=int(bg_level) + 30)
            out = img.copy()
            soft = mask.filter(ImageFilter.GaussianBlur(3))
            out.paste(fixed, (x, y), soft)
            return out

        # 掩膜没抓住（淡水印/整体压暗）：退回最暗边界条带铺平
        flat = edge_fill(img, (x, y, w, h))
        if flat is None:
            keep = region_l.point(lambda v: 255 if v < 200 else 0)
            try:
                local_mean = ImageStat.Stat(region, mask=keep).mean
            except ZeroDivisionError:
                local_mean = list(ring_med)
            flat = Image.new("RGB", (w, h), tuple(int(round(c)) for c in local_mean))
        src = flat
        flat_patch = True
    else:
        flat_patch = False
        bands = [
            img.crop((x, max(0, y - ring), x + w, y)) if y - ring >= 0 else None,
            img.crop((x, y + h, x + w, min(H, y + h + ring))) if y + h + ring <= H else None,
            img.crop((max(0, x - ring), y, x, y + h)) if x - ring >= 0 else None,
            img.crop((x + w, y, min(W, x + w + ring), y + h)) if x + w + ring <= W else None,
        ]
        bands = [b for b in bands if b and b.width and b.height]
        if bands:
            border_mean = [0.0, 0.0, 0.0]
            for b in bands:
                m = ImageStat.Stat(b.convert("RGB")).mean
                for i in range(3):
                    border_mean[i] += m[i] / len(bands)
            src_mean = ImageStat.Stat(src.convert("RGB")).mean
            src = _shift(src, tuple((border_mean[i] - src_mean[i]) * 0.85 for i in range(3)))

    # 羽化带要窄：太宽会让水印从半透区透出来（"字还在、只是变淡"就是这个原因）
    feather = max(4, min(12, min(w, h) // 9))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rectangle([feather, feather, w - feather, h - feather], fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(feather * 0.5))

    out = img.copy()
    out.paste(src, (x, y), mask)
    if not dark_scene:
        core = out.crop((x, y, x + w, y + h)).filter(ImageFilter.GaussianBlur(0.9))
        out.paste(core, (x, y), mask.filter(ImageFilter.GaussianBlur(max(4, min(w, h) // 9))))
    return out


# ---------- 放大 ----------

COMMON_RATIOS = [(3, 2), (2, 3), (1, 1), (16, 9), (9, 16), (4, 5), (5, 4), (4, 3), (3, 4), (21, 9)]


def snap_ratio(new_w: int, new_h: int, src_w: int, src_h: int) -> tuple[int, int]:
    """源图比例接近常见画幅时，把输出尺寸对齐到标准比例（3:2 的 8K 就是 7680×5120，不是 5119）。"""
    ar = src_w / src_h
    for a, b in COMMON_RATIOS:
        if abs(ar - a / b) / (a / b) < 0.008:
            if new_w >= new_h:
                return new_w, int(round(new_w * b / a))
            return int(round(new_h * a / b)), new_h
    return new_w, new_h


def upscale(img: Image.Image, target_long: int) -> Image.Image:
    W, H = img.size
    if max(W, H) >= target_long:
        return img
    scale = target_long / max(W, H)

    cur = img
    while max(cur.size) * 2 <= target_long:
        cur = cur.resize((cur.width * 2, cur.height * 2), Image.LANCZOS)

    new_w, new_h = snap_ratio(max(1, round(W * scale)), max(1, round(H * scale)), W, H)
    final = cur.resize((new_w, new_h), Image.LANCZOS)

    radius = 1.2 + 1.2 * min(3.0, scale / 4.0)
    return final.filter(ImageFilter.UnsharpMask(radius=radius, percent=85, threshold=3))


# ---------- CLI ----------

def parse_wm(val: str):
    if val.startswith("box:"):
        nums = [int(float(n)) for n in val[4:].split(",")]
        if len(nums) != 4:
            raise SystemExit("[args] --watermark box: 需要 x,y,w,h 四个整数")
        return "box", tuple(nums)
    if val in ("auto", "off"):
        return val, None
    raise SystemExit("[args] --watermark 只接受 auto / off / box:x,y,w,h")


def main() -> int:
    ap = argparse.ArgumentParser(description="低调人像成图体检与收尾")
    ap.add_argument("src")
    ap.add_argument("--out", required=True)
    ap.add_argument("--target", default="8k", choices=["native", "2k", "4k", "8k"])
    ap.add_argument("--watermark", default="auto", help="auto | off | box:x,y,w,h")
    ap.add_argument("--format", default=None, choices=["jpg", "png", "webp"])
    ap.add_argument("--quality", type=int, default=95)
    ap.add_argument("--strict", action="store_true", help="背景不纯时返回退出码 2")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    t0 = time.time()
    src = Path(args.src)
    if not src.exists():
        raise SystemExit(f"[in] 文件不存在：{src}")

    img = open_image(src)
    before = img.size
    bg = check_background(img)

    mode, box = parse_wm(args.watermark)
    wm = {"mode": mode, "applied": False}
    if mode == "off":
        wm["reason"] = "用户关闭"
    else:
        info = detect(img)
        wm["detect"] = info
        if mode == "box":
            target_box = box
        elif info["score"] >= AUTO_MIN_SCORE and info["candidate"]:
            target_box = info["candidate"]
        else:
            target_box = None
            wm["reason"] = (f"自动检测分数 {info['score']} < {AUTO_MIN_SCORE}，判为无水印；"
                            "如确有水印用 --watermark box:x,y,w,h")
        if target_box:
            # 框只比热块外扩一圈，不硬贴右下角（贴角会把主体边缘卷进来，补出一个灰块）；
            # 补完重测一次，若仍有残留（半个字、字尾）再扩框补一次。
            passes = 0
            while target_box and passes < 3:
                img = patch(img, target_box)
                passes += 1
                again = detect(img)
                if again["score"] < AUTO_MIN_SCORE or not again["candidate"]:
                    break
                target_box = again["candidate"]
            wm["applied"] = True
            wm["passes"] = passes

    if args.target != "native":
        img = upscale(img, SIZES[args.target])

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fmt = (args.format or out.suffix.lstrip(".") or "jpg").lower()
    if fmt == "jpeg":
        fmt = "jpg"
    if fmt == "jpg":
        img.convert("RGB").save(out, quality=args.quality, subsampling=0, optimize=True)
    elif fmt == "webp":
        img.save(out, quality=args.quality, method=6)
    else:
        img.save(out, compress_level=6)

    report = {
        "in": src.as_posix(),
        "in_size": list(before),
        "background": bg,
        "watermark": wm,
        "out": out.as_posix(),
        "out_size": list(img.size),
        "target": args.target,
        "format": fmt,
        "bytes": out.stat().st_size,
        "seconds": round(time.time() - t0, 2),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if args.strict and bg.get("verdict") == "impure":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
