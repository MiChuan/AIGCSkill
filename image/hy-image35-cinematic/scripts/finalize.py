#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""成图收尾：清右下角水印 + 重采样到 8K 交付尺寸。

依赖 Pillow（pip install pillow）。

用法:
    python finalize.py in.png --out out.jpg --target 8k --watermark auto
    python finalize.py in.png --out out.jpg --target 4k --watermark off
    python finalize.py in.png --out out.jpg --watermark box:1620,980,400,150
    python finalize.py in.png --out out.png --format png

原理与局限见 references/postprocess.md。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps, ImageStat
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "[deps] 缺少 Pillow。安装：\n"
        "  python -m pip install pillow\n"
        "（或用本机已有的 Python 环境跑，脚本零其他依赖）\n"
    )
    raise SystemExit(2)

SIZES = {"2k": 2048, "4k": 3840, "8k": 7680}

# 水印扫描窗口（相对坐标）：右下角约 25% × 14%
SCAN_BOX = (0.75, 0.86, 1.0, 1.0)

# 自动模式的应用阈值：低于此分数视为无水印，不补片（避免无谓破坏画面）
AUTO_MIN_SCORE = 0.30


def open_image(path: Path) -> Image.Image:
    img = Image.open(path)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    return img


# ---------- 水印检测 ----------

def scan_rect(W: int, H: int) -> tuple[int, int, int, int]:
    """右下角扫描窗口的像素坐标 (x, y, w, h)。"""
    x0 = int(W * SCAN_BOX[0])
    y0 = int(H * SCAN_BOX[1])
    x1 = int(W * SCAN_BOX[2])
    y1 = int(H * SCAN_BOX[3])
    return (x0, y0, x1 - x0, y1 - y0)


def snap_corner(box: tuple[int, int, int, int], size: tuple[int, int]) -> tuple[int, int, int, int]:
    """把候选框扩到右下角：水印总是贴角，宁可多补一圈，也别留下一半字。"""
    W, H = size
    x, y, w, h = box
    x2 = max(0, min(x, W - 1))
    y2 = max(0, min(y, H - 1))
    return (x2, y2, W - x2, H - y2)


def detect(img: Image.Image) -> dict:
    """在右下角窗口里用「分块边缘能量」找水印叠加层。

    水印是文字/logo，局部边缘能量远高于同窗口的云/天空渐变；
    云层与渐变在同一窗口内能量均匀。所以用块能量的中位数做基线，
    高出基线数倍的块连成的区域就是候选。
    """
    W, H = img.size
    wx, wy, ww, wh = scan_rect(W, H)
    box = (wx, wy, wx + ww, wy + wh)
    region = img.crop(box).convert("L")
    edges = region.filter(ImageFilter.FIND_EDGES)

    cols, rows = 12, 5
    tw, th = max(1, ww // cols), max(1, wh // rows)
    energies: list[tuple[int, int, float]] = []
    for r in range(rows):
        for c in range(cols):
            tile = edges.crop((c * tw, r * th, (c + 1) * tw, (r + 1) * th))
            energies.append((c, r, ImageStat.Stat(tile).mean[0]))

    vals = sorted(e for _, _, e in energies)
    median = vals[len(vals) // 2] or 1e-6
    threshold = max(median * 2.2, 8.0)

    hot = [(c, r) for c, r, e in energies if e >= threshold]
    ratio = len(hot) / len(energies)
    score = min(1.0, ratio * 2.4)

    cand = None
    if 0 < len(hot) < len(energies) * 0.45:
        c0 = min(c for c, _ in hot)
        c1 = max(c for c, _ in hot)
        r0 = min(r for _, r in hot)
        r1 = max(r for _, r in hot)
        pad_x, pad_y = tw, th
        cx0 = max(0, wx + c0 * tw - pad_x)
        cy0 = max(0, wy + r0 * th - pad_y)
        cx1 = min(W, wx + (c1 + 1) * tw + pad_x)
        cy1 = min(H, wy + (r1 + 1) * th + pad_y)
        cand = (cx0, cy0, cx1 - cx0, cy1 - cy0)

    return {
        "score": round(score, 3),
        "hot_tiles": len(hot),
        "tile_median_edge": round(median, 2),
        "threshold": round(threshold, 2),
        "scan_box": list(box),
        "candidate": cand,
    }


# ---------- 补片 ----------

def _shift(img: Image.Image, offset: tuple[float, float, float]) -> Image.Image:
    """整体平移每个通道的亮度（查表实现，不受 255 钳位的影响）。"""
    if all(abs(v) < 0.5 for v in offset):
        return img
    chans = img.convert("RGB").split()
    out = []
    for ch, o in zip(chans, offset):
        oi = int(round(o))
        if oi:
            ch = ch.point([max(0, min(255, v + oi)) for v in range(256)])
        out.append(ch)
    return Image.merge("RGB", out)


def patch(img: Image.Image, box: tuple[int, int, int, int]) -> Image.Image:
    """用同高度、紧邻左侧的干净区域镜像过来，配色对齐后羽化融合。"""
    W, H = img.size
    x, y, w, h = box
    w = max(24, min(w, W - x))
    h = max(12, min(h, H - y))

    gap = max(8, w // 10)
    sx = x - gap - w
    if sx < 0:                      # 左侧太窄，改从右侧（水印在中间时才会走到）
        sx = min(W - w, x + w + gap)
    if sx < 0 or sx + w > W:
        sx = max(0, x - w)          # 退化为左邻拼接，不镜像
        src = img.crop((sx, y, sx + w, y + h))
    else:
        src = ImageOps.mirror(img.crop((sx, y, sx + w, y + h)))

    # 配色对齐：把补片均值拉到目标框四周边缘带的均值，消掉接缝色差
    ring = 8
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
        offset = tuple((border_mean[i] - src_mean[i]) * 0.85 for i in range(3))
        src = _shift(src, offset)

    # 掩膜：内部大面积实心，只在边缘留一圈羽化带，保证水印不会从半透处透出来
    feather = max(5, min(w, h) // 7)
    mask = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(mask)
    draw.rectangle([feather, feather, w - feather, h - feather], fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(feather * 0.55))

    out = img.copy()
    out.paste(src, (x, y), mask)

    # 交界处轻微柔化，避免补片纹理与周围突变
    core = out.crop((x, y, x + w, y + h)).filter(ImageFilter.GaussianBlur(0.9))
    soft = mask.filter(ImageFilter.GaussianBlur(max(4, min(w, h) // 9)))
    out.paste(core, (x, y), soft)
    return out


# ---------- 重采样 ----------

def upscale(img: Image.Image, target_long: int) -> Image.Image:
    W, H = img.size
    long_side = max(W, H)
    if long_side >= target_long:
        return img
    scale = target_long / long_side

    cur = img
    # 分步 2x 递增，减少一次性大倍率放大的糊感
    while True:
        long_now = max(cur.size)
        if long_now * 2 > target_long:
            break
        cur = cur.resize((cur.width * 2, cur.height * 2), Image.LANCZOS)

    new_w = max(1, round(W * scale))
    new_h = max(1, round(H * scale))
    final = cur.resize((new_w, new_h), Image.LANCZOS)

    radius = 1.2 + 1.2 * min(3.0, scale / 4.0)
    return final.filter(ImageFilter.UnsharpMask(radius=radius, percent=85, threshold=3))


# ---------- CLI ----------

def parse_wm(val: str) -> tuple[str, tuple[int, int, int, int] | None]:
    if val.startswith("box:"):
        nums = [int(float(n)) for n in val[4:].split(",")]
        if len(nums) != 4:
            raise SystemExit("[args] --watermark box: 需要 x,y,w,h 四个整数")
        return "box", tuple(nums)
    if val in ("auto", "off"):
        return val, None
    raise SystemExit("[args] --watermark 只接受 auto / off / box:x,y,w,h")


def main() -> int:
    ap = argparse.ArgumentParser(description="清右下角水印 + 重采样到交付尺寸")
    ap.add_argument("src")
    ap.add_argument("--out", required=True)
    ap.add_argument("--target", default="8k", choices=["native", "2k", "4k", "8k"])
    ap.add_argument("--watermark", default="auto", help="auto | off | box:x,y,w,h")
    ap.add_argument("--format", default=None, choices=["jpg", "png", "webp"])
    ap.add_argument("--quality", type=int, default=95, help="jpg/webp 质量，默认 95")
    ap.add_argument("--force-region", action="store_true",
                    help="检测不到也强行补默认右下角窗口（背景干净时可用）")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    t0 = time.time()
    src_path = Path(args.src)
    if not src_path.exists():
        raise SystemExit(f"[in] 文件不存在：{src_path}")

    img = open_image(src_path)
    before = img.size

    mode, box = parse_wm(args.watermark)
    report: dict = {"in": src_path.as_posix(), "in_size": list(before), "watermark_mode": mode}

    if mode == "off":
        report["watermark"] = {"applied": False, "reason": "用户关闭"}
    else:
        info = detect(img)
        report["watermark"] = {"detect": info, "applied": False}
        if mode == "box":
            target_box = box
        elif info["score"] >= AUTO_MIN_SCORE:
            target_box = snap_corner(info["candidate"], img.size) if info["candidate"] else None
        else:
            target_box = None
            report["watermark"]["reason"] = (
                f"自动检测分数 {info['score']} < {AUTO_MIN_SCORE}，判为无水印；"
                "如图上确有水印（浅色背景上的淡水印容易漏检），用 --watermark box:x,y,w,h 指定"
            )
        if target_box is None and args.force_region:
            target_box = scan_rect(*img.size)
            report["watermark"]["reason"] = "未达阈值，按 --force-region 补默认窗口"
        if target_box:
            img = patch(img, target_box)
            report["watermark"]["applied"] = True
            report["watermark"]["box"] = list(target_box)

    if args.target != "native":
        img = upscale(img, SIZES[args.target])

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = (args.format or out_path.suffix.lstrip(".") or "jpg").lower()
    if fmt in ("jpeg",):
        fmt = "jpg"

    if fmt == "jpg":
        img.convert("RGB").save(out_path, quality=args.quality, subsampling=0, optimize=True)
    elif fmt == "webp":
        img.save(out_path, quality=args.quality, method=6)
    else:
        img.save(out_path, compress_level=6)

    report.update({
        "out": out_path.as_posix(),
        "out_size": list(img.size),
        "target": args.target,
        "format": fmt,
        "bytes": out_path.stat().st_size,
        "seconds": round(time.time() - t0, 2),
    })
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
