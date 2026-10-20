#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""合成一张演示用参考原图，让 examples 与 specs 可以零素材跑通。

这不是给你用的「素材库」——它的唯一作用是让 extract_palette.py 与
build_print_prompt.py 有一条任何人都能复现的输入。真实使用时换成你自己的照片。

画的是 specs/ink-print-lake.json 描述的那处景物：三层山、水岸、左岸两株树、
水面。刻意做成「信息量中等」——有渐变、有鳞光、有碎石，这样 extract_palette
的「剔除细碎纹理」才有东西可剔。

    python make_demo_ref.py --out assets/demo-ref.png
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFilter
except ImportError:  # pragma: no cover
    import sys
    sys.stderr.write("[deps] 缺少 Pillow：python -m pip install pillow\n")
    raise SystemExit(2)


def build(w: int = 1024, h: int = 1280) -> Image.Image:
    img = Image.new("RGB", (w, h), (226, 228, 222))
    d = ImageDraw.Draw(img)

    # 天空：淡淡的冷暖渐变（真实照片都有，平涂重构时要把它扔掉）
    for y in range(int(h * 0.42)):
        t = y / (h * 0.42)
        d.line([(0, y), (w, y)], fill=(int(226 - 18 * t), int(230 - 14 * t), int(226 - 10 * t)))

    # 远山三层，越远越浅
    for i, (col, base, amp) in enumerate((
        ((186, 192, 186), 0.34, 0.10),
        ((150, 160, 152), 0.40, 0.13),
        ((104, 116, 110), 0.46, 0.09),
    )):
        rnd = random.Random(i * 7 + 3)
        pts = [(0, int(h * (base + 0.04)))]
        x = 0
        while x <= w:
            pts.append((x, int(h * (base + amp * rnd.uniform(-1, 1)))))
            x += 70
        pts += [(w, int(h * 0.60)), (0, int(h * 0.60))]
        d.polygon(pts, fill=col)

    # 水面 + 鳞光
    d.rectangle([0, int(h * 0.60), w, h], fill=(214, 216, 208))
    rnd = random.Random(11)
    for _ in range(40):
        y = int(h * (0.62 + 0.36 * rnd.random()))
        x0 = rnd.randint(0, w - 200)
        d.line([(x0, y), (x0 + rnd.randint(60, 220), y)],
               fill=(198, 202, 196), width=rnd.choice([1, 1, 2]))

    # 左岸的赭色土带（就是提示词里那 2% 的点缀色来源）
    d.polygon([(0, int(h * 0.585)), (int(w * 0.30), int(h * 0.575)),
               (int(w * 0.44), int(h * 0.60)), (0, int(h * 0.61))], fill=(160, 132, 104))

    # 两株树
    for x0, hh in ((int(w * 0.16), 0.16), (int(w * 0.22), 0.12)):
        d.line([(x0, int(h * 0.60)), (x0, int(h * (0.60 - hh)))], fill=(58, 68, 62), width=6)
        d.ellipse([x0 - 46, int(h * (0.60 - hh)) - 40, x0 + 46, int(h * (0.60 - hh)) + 34],
                  fill=(74, 88, 80))

    return img.filter(ImageFilter.GaussianBlur(0.6))


def main() -> int:
    ap = argparse.ArgumentParser(description="合成演示用参考原图")
    ap.add_argument("--out", default="assets/demo-ref.png")
    ap.add_argument("--size", default="1024x1280")
    args = ap.parse_args()

    w, h = (int(v) for v in args.size.lower().split("x"))
    img = build(w, h)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    print(f"[write] {out}  {w}x{h}")
    print("[next]  python extract_palette.py %s --outdir out/pal --k 6 --top 0.62" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
