#!/usr/bin/env python3
"""给"无字印章底"叠刻文字（模型写不对字时的保底路线）。

为什么需要它：图像模型渲染汉字是"画形状"，不是"排字"。
3–4 字还有救，6 字以上几乎必出错字、缺笔或造字。这时改成两步走：
    1. 出图时提示词里不要文字，只要图案 + 留白
    2. 用本脚本把文字以印泥色刻进留白区——字 100% 正确，质感靠下面几步模拟

质感怎么来（不是简单贴一层纯色字）：
    - 腐蚀出实心核心 + 膨胀出边缘带，边缘带用噪声筛掉一部分 → 笔画边缘的崩口
    - 边缘再随机擦掉几个小圆 → 石刻的磕碰缺口
    - 整层叠一层缓变浓淡 → 印泥厚薄不均
    - 0.6px 微模糊 → 印泥在纸纤维上的轻微洇开

依赖：Pillow

用法：
    python stamp_text.py seal_blank.png --text "山亭晚照" --out seal_done.png
    python stamp_text.py seal_blank.png --text "山亭晚照" --box 0.60,0.10,0.30,0.78 --rough 1.4
"""

from __future__ import annotations

import argparse
import math
import random
import sys
from pathlib import Path

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont
except ImportError:
    print("[error] 需要 Pillow：pip install pillow", file=sys.stderr)
    raise SystemExit(2)

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from sealcut import find_seal_box, plausible_box, saturated_mask
except ImportError:
    find_seal_box = None

DEFAULT_BOX = (0.60, 0.10, 0.30, 0.78)   # 兜底：右侧一列竖排留白


def auto_box(img: Image.Image, shape: str, ratio: float = 0.32,
             cols: int = 1) -> tuple[float, float, float, float] | None:
    """按印面实际位置算文字区。

    直接给"右侧 30%"这种固定比例，对圆形印章会翻车：矩形的四角在圆外，
    字会被圆环切掉。所以圆形要退到圆的内接正方形里再切一列。
    """
    if find_seal_box is None:
        return None
    box = find_seal_box(saturated_mask(img)) or find_seal_box(
        img.convert("L").point(lambda v: 255 if v < 150 else 0))
    if not box or not plausible_box(box, img.size):
        return None

    W, H = img.size
    x, y, w, h = box
    cx, cy = x + w / 2.0, y + h / 2.0

    side = max(w, h) * (0.70 if shape == "round" else 0.94)
    left, top = cx - side / 2.0, cy - side / 2.0

    cols = max(1, cols)
    total_w = min(side * ratio * cols, side * 0.68)      # 多列时整体收在印面内
    bx = (left + side - total_w - side * 0.05) / W
    by = (top + side * 0.05) / H
    bw = (total_w * 0.94) / W
    bh = (side * 0.90) / H
    return (round(bx, 4), round(by, 4), round(bw, 4), round(bh, 4))


# ---------------------------------------------------------------- 印泥取样

def sample_ink(img: Image.Image) -> tuple[int, int, int]:
    """取画面里饱和度最高的一撮像素的中位色，就是印泥色。"""
    small = img.convert("RGB").resize((160, 160), Image.BILINEAR)
    px = small.load()
    hsv = small.convert("HSV").load()
    picks = []
    for y in range(160):
        for x in range(160):
            if hsv[x, y][1] > 70:                 # 饱和度够高才算印泥
                picks.append(px[x, y])
    if len(picks) < 40:
        print("[warn] 图里高饱和像素太少，印泥色退回默认朱砂 #B23A2E", file=sys.stderr)
        return (178, 58, 46)
    picks.sort(key=lambda p: p[0] * 0.299 + p[1] * 0.587 + p[2] * 0.114)
    return picks[len(picks) // 2]


# ---------------------------------------------------------------- 刻字

def render_glyphs(size: tuple[int, int], text: str, font_path: str, box: tuple[float, float, float, float],
                  cols: int, tracking: float) -> Image.Image:
    """把文字排成竖排（自上而下、列自右向左），返回 L 掩膜。"""
    W, H = size
    bx, by, bw, bh = (int(box[0] * W), int(box[1] * H), int(box[2] * W), int(box[3] * H))
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)

    chars = [c for c in text if not c.isspace()]
    per_col = math.ceil(len(chars) / cols)
    col_w = bw / cols

    # 字号同时受"每格高度"和"列宽"约束，否则多列时字会互相压叠
    head = bh / per_col
    font_px = max(12, int(min(head * tracking, col_w * 0.92)))
    font = ImageFont.truetype(font_path, font_px)

    for i, ch in enumerate(chars):
        col = i // per_col
        row = i % per_col
        cx = bx + bw - col_w * (col + 0.5)          # 第一列在最右
        cy = by + head * (row + 0.5)
        draw.text((cx, cy), ch, font=font, fill=255, anchor="mm")
    return mask


def roughen(mask: Image.Image, rough: float, seed: int) -> Image.Image:
    """把规整的字体边缘弄成刻石的样子。"""
    rnd = random.Random(seed)

    core = mask.filter(ImageFilter.MinFilter(3))          # 腐蚀：笔画中心，必定保留
    outer = mask.filter(ImageFilter.MaxFilter(3))         # 膨胀
    band = ImageChops.subtract(outer, core)               # 边缘带

    noise = Image.effect_noise(mask.size, 90).filter(ImageFilter.GaussianBlur(1.2))
    # rough 越大，边缘被噪声吃掉的越多（崩口越碎）
    cut = int(96 + 18 * min(3.0, max(0.0, rough)))
    band = ImageChops.multiply(band, noise.point(lambda v: 255 if v >= cut else 0))
    out = ImageChops.lighter(core, band)

    # 随机磕碰缺口：沿笔画边缘擦掉几个小圆
    if rough > 0.4:
        holes = int(6 * rough)
        bbox = mask.getbbox()
        if bbox:
            bx0, by0, bx1, by1 = bbox
            layer = ImageDraw.Draw(out)
            for _ in range(holes):
                hx = rnd.randint(bx0, bx1)
                hy = rnd.randint(by0, by1)
                r = rnd.randint(2, max(3, int(4 * rough)))
                layer.ellipse([hx - r, hy - r, hx + r, hy + r], fill=0)

    return out.filter(ImageFilter.GaussianBlur(0.6))


def density_layer(size: tuple[int, int], seed: int) -> Image.Image:
    """印泥浓淡：低频缓变，让字有的地方实、有的地方淡。"""
    rnd = random.Random(seed)
    small = Image.new("L", (48, 48), 0)
    d = ImageDraw.Draw(small)
    for _ in range(26):
        x, y = rnd.randrange(48), rnd.randrange(48)
        r = rnd.randint(4, 12)
        d.ellipse([x, y, x + r, y + r], fill=rnd.randint(0, 70))
    small = small.filter(ImageFilter.GaussianBlur(5)).resize(size, Image.BILINEAR)
    return small.point(lambda v: max(150, 255 - v))


def main() -> int:
    ap = argparse.ArgumentParser(description="给无字印章底叠刻文字")
    ap.add_argument("input", help="无字印章底图")
    ap.add_argument("--out", required=True, help="输出路径")
    ap.add_argument("--text", required=True, help="要刻的字")
    ap.add_argument("--font", default="", help="字体文件；默认试系统楷体")
    ap.add_argument("--box", default="", help="文字区域相对坐标 x,y,w,h（默认自动按印面算）")
    ap.add_argument("--shape", default="round", choices=["round", "square"], help="印面形状，决定文字区怎么切")
    ap.add_argument("--cols", type=int, default=1, help="竖排列数（自右向左）")
    ap.add_argument("--ink", default="", help="印泥色 #RRGGBB；默认从图里采样")
    ap.add_argument("--rough", type=float, default=1.0, help="崩口程度 0–3，默认 1.0")
    ap.add_argument("--tracking", type=float, default=0.82, help="字号占每格高度比例，默认 0.82")
    ap.add_argument("--seed", type=int, default=7, help="随机种子，换一个就换一种崩法")
    args = ap.parse_args()

    src = Path(args.input)
    if not src.exists():
        print(f"[error] 找不到输入图：{src}", file=sys.stderr)
        return 2
    img = Image.open(src).convert("RGB")
    W, H = img.size

    font_path = args.font
    if not font_path:
        for cand in ("C:/Windows/Fonts/simkai.ttf", "C:/Windows/Fonts/simsun.ttc",
                     "/System/Library/Fonts/Supplemental/Songti.ttc",
                     "/usr/share/fonts/truetype/arphic/uming.ttc"):
            if Path(cand).exists():
                font_path = cand
                break
    if not font_path or not Path(font_path).exists():
        print("[error] 找不到可用字体：用 --font 指定一个 .ttf/.ttc", file=sys.stderr)
        return 2

    if args.box:
        try:
            box = tuple(float(v) for v in args.box.replace("，", ",").split(","))
            if len(box) != 4:
                raise ValueError
        except ValueError:
            print(f"[error] --box 需要 x,y,w,h 四个 0–1 的小数，收到 {args.box}", file=sys.stderr)
            return 2
    else:
        box = auto_box(img, args.shape, cols=args.cols)
        if box is None:
            print("[warn] 没定位到印面，退回默认的右侧一列；如位置不对请用 --box 指定",
                  file=sys.stderr)
            box = DEFAULT_BOX

    ink = sample_ink(img) if not args.ink else tuple(
        int(args.ink.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)
    )

    mask = render_glyphs((W, H), args.text, font_path, box, max(1, args.cols), args.tracking)
    if not mask.getbbox():
        print("[error] 文字没渲染出来：字号算得太小或字体不支持这些字", file=sys.stderr)
        return 2

    mask = roughen(mask, args.rough, args.seed)
    mask = ImageChops.multiply(mask, density_layer((W, H), args.seed + 1))

    layer = Image.new("RGB", (W, H), ink)
    out = Image.composite(layer, img, mask)
    out.save(args.out)
    print(f"[ok] {args.out}  文字={args.text}  印泥=#{ink[0]:02X}{ink[1]:02X}{ink[2]:02X}  "
          f"box={box}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
