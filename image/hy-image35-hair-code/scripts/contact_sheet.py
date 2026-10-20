#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""方案对比图：参考图 + N 张试发图拼成一张，统一比例、统一标注（需要 Pillow）。

为什么值得单独写一个脚本：三张方案如果各自尺寸不同、裁切不同、标注也没有，
你只能来回切图「感觉一下」，感觉是被构图和大小骗的。拼在一张里、裁到同一个头肩比例，
差别才只剩发型本身。

用法:
    python scripts/contact_sheet.py --ref assets/face.jpg \\
        --img out/s1/x.png out/s2/x.png out/s3/x.png \\
        --labels "参考/现状" "1 自然中分" "2 轻层次中长" "3 三七分" \\
        --crop head --out out/sheet.jpg

    # 只拼不裁（全身或半身照）
    python scripts/contact_sheet.py --ref a.jpg --img b.jpg --out sheet.jpg --crop none
"""

from __future__ import annotations

import argparse
from pathlib import Path

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
]


def find_font():
    from PIL import ImageFont
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, 34), p
            except OSError:
                continue
    return None, None


def fit_font(draw, text, font, max_w, start=34):
    from PIL import ImageFont
    size = start
    while size > 12:
        w = draw.textlength(text, font=font)
        if w <= max_w:
            break
        size -= 2
        try:
            font = font.font_variant(size=size)  # Pillow >= 10
        except AttributeError:
            font = ImageFont.truetype(getattr(font, "path", ""), size)
    return font


def head_crop(im, frac=0.62):
    """只留上半部分（头 + 肩），让三张图的头肩比例一致。"""
    h = max(1, int(im.height * frac))
    return im.crop((0, 0, im.width, h))


def to_tile(im, w, h, mode):
    from PIL import Image
    if mode == "head":
        im = head_crop(im)
    # 等比缩放到铺满瓦片，再居中裁掉溢出部分
    scale = max(w / im.width, h / im.height)
    nw, nh = max(w, round(im.width * scale)), max(h, round(im.height * scale))
    im = im.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - w) // 2, (nh - h) // 2
    return im.crop((left, top, left + w, top + h))


def main() -> int:
    ap = argparse.ArgumentParser(description="试发方案对比图")
    ap.add_argument("--ref", required=True, help="参考图（原照片）")
    ap.add_argument("--img", nargs="+", required=True, help="生成结果，按顺序拼接")
    ap.add_argument("--labels", nargs="*", default=None, help="每个瓦片的标注，数量要对得上（含参考图）")
    ap.add_argument("--out", default="out/sheet.jpg", help="输出路径")
    ap.add_argument("--crop", default="head", choices=["head", "none"], help="head=只取上半部分")
    ap.add_argument("--tile", default="760x950", help="瓦片尺寸 WxH")
    ap.add_argument("--gap", type=int, default=14, help="瓦片间距")
    ap.add_argument("--title", default="", help="顶部标题")
    args = ap.parse_args()

    try:
        from PIL import Image, ImageDraw
    except ImportError:
        raise SystemExit("需要 Pillow：pip install pillow")

    tw, th = (int(x) for x in args.tile.lower().split("x"))
    paths = [Path(args.ref)] + [Path(p) for p in args.img]
    for p in paths:
        if not p.exists():
            raise SystemExit(f"找不到图片：{p}")

    labels = args.labels or []
    if labels and len(labels) != len(paths):
        raise SystemExit(f"--labels 给了 {len(labels)} 个，但瓦片有 {len(paths)} 个（参考图也算一个）")
    if not labels:
        labels = ["参考"] + [f"方案 {i}" for i in range(1, len(paths))]

    font, font_path = find_font()
    ascii_only = font is None
    bar = 62 if font else 40
    pad = args.gap
    title_h = 84 if args.title else 0

    W = pad + (tw + pad) * len(paths)
    H = title_h + pad + th + bar + pad
    sheet = Image.new("RGB", (W, H), (18, 18, 20))
    d = ImageDraw.Draw(sheet)

    if args.title:
        if ascii_only:
            d.text((pad + 4, 24), args.title, fill=(235, 235, 235))
        else:
            d.text((pad + 4, 22), args.title, font=font, fill=(235, 235, 235))

    for i, (p, label) in enumerate(zip(paths, labels)):
        im = Image.open(p).convert("RGB")
        tile = to_tile(im, tw, th, args.crop)
        x = pad + i * (tw + pad)
        y = title_h + pad
        sheet.paste(tile, (x, y))
        d.rectangle([x, y, x + tw - 1, y + th - 1], outline=(70, 70, 76), width=2)

        if ascii_only and any(ord(c) > 127 for c in label):
            label = f"{i:02d}"
        if ascii_only:
            d.text((x + 12, y + th + 14), label, fill=(220, 220, 220))
        else:
            f = fit_font(d, label, font, tw - 24)
            d.text((x + 12, y + th + 12), label, font=f, fill=(232, 232, 232))
        if i == 0:
            d.rectangle([x, y, x + tw - 1, y + th - 1], outline=(240, 196, 96), width=3)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, quality=92)
    print(f"sheet -> {out.as_posix()}  {sheet.width}x{sheet.height}  {len(paths)} 格  crop={args.crop}")
    if ascii_only:
        print("提示：没找到中文字体，标注改成了 ASCII；想要中文标注请装 Noto Sans CJK 或改 FONT_CANDIDATES")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
