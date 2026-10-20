#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""底部英文排版叠字：模型写不好小字时的保底方案。

为什么需要它：
  图像模型对小字号英文的失败率远高于大标题——会漏字母、把两个词粘成一个、
  自行编造不存在的单词、字距忽宽忽窄、行基线歪。这些错误在 8K 成图上比在缩略图上
  更明显，因为观众会放大看。而「干净高级」这四个字有一半是靠排版的规整度撑起来的。
  所以能叠就叠：字距、字号、对齐、位置全部由数字决定，不靠模型的手感。

用法:
    python settype.py raw.png --out out/typed.png --spec specs/female-wave.json
    python settype.py raw.png --out out/typed.png --headline KARINA --sub "STILL CHASING THE LIGHT"
    python settype.py raw.png --out out/typed.png --line "THE ACADEMY" --line "PORTRAIT STUDY NO.02" \\
        --font C:/Windows/Fonts/georgia.ttf --zone 0.12

设计要点见 references/typography.md。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageStat
except ImportError:  # pragma: no cover
    sys.stderr.write("[deps] 缺少 Pillow。安装：python -m pip install pillow\n")
    raise SystemExit(2)

FONT_CANDIDATES = [
    "C:/Windows/Fonts/georgia.ttf",
    "C:/Windows/Fonts/times.ttf",
    "C:/Windows/Fonts/cambria.ttc",
    "C:/Windows/Fonts/constan.ttf",
    "/System/Library/Fonts/Times.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
]

# 三档字号与字距（相对画面高度 / 相对自身字号）。
# 字距必须比常规正文宽很多——宽字距是这类型写真「不像 PPT」的关键，
# 也是模型最容易画错的地方（它会用「拉开空格」冒充 tracking）。
ROLE = {
    0: {"size": 0.0300, "track": 0.140},
    1: {"size": 0.0110, "track": 0.260},
    2: {"size": 0.0090, "track": 0.220},
}


def pick_font(explicit: str | None) -> str:
    if explicit:
        p = Path(explicit)
        if not p.exists():
            raise SystemExit(f"[font] 指定的字体不存在：{p}")
        return str(p)
    for c in FONT_CANDIDATES:
        if Path(c).exists():
            return c
    raise SystemExit("[font] 找不到可用的衬线字体，用 --font 指定一个 .ttf/.ttc")


def luminance(img: Image.Image) -> Image.Image:
    return img.convert("L")


def band_stats(img: Image.Image, top: int, bottom: int) -> dict:
    band = img.crop((0, top, img.width, bottom)).convert("L")
    st = ImageStat.Stat(band)
    return {"mean": round(st.mean[0], 1), "stddev": round(st.stddev[0], 2)}


def detect_bottom_bar(img: Image.Image, max_frac: float = 0.05) -> int:
    """测出画面底部那条纯色边（后期加的白边框）有多高。

    排版必须避开它：白边框本身就是「最安静的一条带」，不排除的话文字会排到
    边框上——看起来像贴在相纸外面，是这类写真最容易被一眼看出的错误。
    """
    lum = img.convert("L")
    W, H = img.size
    px = lum.load()
    x0, x1 = int(W * 0.20), int(W * 0.80)

    def row_std(y: int) -> float:
        vals = [px[x, y] for x in range(x0, x1, max(1, (x1 - x0) // 160))]
        m = sum(vals) / len(vals)
        return (sum((v - m) ** 2 for v in vals) / len(vals)) ** 0.5

    n = 0
    for y in range(H - 1, max(0, H - int(H * max_frac)) - 1, -1):
        if row_std(y) < 3.0:
            n += 1
        else:
            break
    return n if n >= max(2, int(H * 0.004)) else 0


def choose_band(img: Image.Image, want: float, skip_bottom: int = 0,
                max_up: int = 3) -> tuple[int, int, dict]:
    """在画面底部选一条能排字的带子。

    和「找全图最安静的横带」不同——那个策略会把文字排到画面中部，
    而这类写真的文字必须在下三分之一。所以这里的做法是：
    **从底部往上试，第一块「没有大面积高光」的就用它**。
    判据只看一件事：带子里接近纯白（>245）的像素不能超过 12%，
    否则白字会和白衬衫/白边框糊在一起。真要不行才往上挪，最多 max_up 格。
    """
    W, H = img.size
    h = max(24, int(H * want))
    x0, x1 = int(W * 0.16), int(W * 0.84)
    bottom_limit = H - skip_bottom - max(2, int(H * 0.012))

    best = None
    y_bottom = bottom_limit
    for step in range(max_up + 1):
        y1 = y_bottom - step * int(h * 0.45)
        y0 = max(0, y1 - h)
        if y0 < H * 0.45:
            break
        band = img.crop((x0, y0, x1, y1)).convert("L")
        st = ImageStat.Stat(band)
        hist = band.histogram()
        total = max(1, sum(hist))
        bright = sum(hist[245:]) / total
        stats = {
            "mean": round(st.mean[0], 1),
            "stddev": round(st.stddev[0], 2),
            "bright_ratio": round(bright, 3),
        }
        if best is None:
            best = (y0, y1, stats)
        if bright <= 0.12:
            return y0, y1, stats
    y0, y1, stats = best  # type: ignore[misc]
    return y0, y1, stats


def tracked_width(text: str, font, track: float) -> float:
    if not text:
        return 0.0
    w = sum(font.getlength(ch) for ch in text)
    return w + track * max(0, len(text) - 1)


def draw_tracked(draw: ImageDraw.ImageDraw, x: float, baseline: float, text: str,
                 font, track: float, fill, shadow: bool) -> None:
    cx = x
    for ch in text:
        if shadow:
            draw.text((cx + max(1.0, track * 0.18), baseline + max(1.0, track * 0.18)),
                      ch, font=font, fill=(0, 0, 0, 70), anchor="ls")
        draw.text((cx, baseline), ch, font=font, fill=fill, anchor="ls")
        cx += font.getlength(ch) + track


def contrast_ratio(c1, c2) -> float:
    def lin(v: float) -> float:
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    l1 = 0.2126 * lin(c1[0]) + 0.7152 * lin(c1[1]) + 0.0722 * lin(c1[2])
    l2 = 0.2126 * lin(c2[0]) + 0.7152 * lin(c2[1]) + 0.0722 * lin(c2[2])
    hi, lo = max(l1, l2), min(l1, l2)
    return round((hi + 0.05) / (lo + 0.05), 2)


def main() -> int:
    ap = argparse.ArgumentParser(description="底部英文排版叠字")
    ap.add_argument("src")
    ap.add_argument("--out", required=True)
    ap.add_argument("--spec", default=None, help="从 spec 的 typeset 段读文字")
    ap.add_argument("--line", action="append", default=[], help="逐行文字，按顺序当主标题/副标题/页脚")
    ap.add_argument("--headline", default=None)
    ap.add_argument("--sub", default=None)
    ap.add_argument("--third", default=None)
    ap.add_argument("--font", default=None)
    ap.add_argument("--zone", type=float, default=0.12, help="文字带高度占画面高度的比例")
    ap.add_argument("--skip-bottom", type=float, default=None,
                    help="跳过底部多少比例（自动识别白边框；识别不准时手动给，如 0.015）")
    ap.add_argument("--color", default=None, help="字色 RRGGBB；默认按底色自动选深/浅")
    ap.add_argument("--shadow", action="store_true", help="加极轻的投影（默认不加）")
    ap.add_argument("--opacity", type=float, default=0.94)
    ap.add_argument("--align", default="center", choices=["center", "left"])
    ap.add_argument("--scale", type=float, default=1.0, help="整体字号缩放")
    ap.add_argument("--quality", type=int, default=95)
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    src = Path(args.src)
    if not src.exists():
        raise SystemExit(f"[in] 文件不存在：{src}")
    img = Image.open(src)
    if img.mode not in ("RGB",):
        img = img.convert("RGB")
    W, H = img.size

    lines: list[str] = []
    if args.spec:
        import json as _json
        spec = _json.loads(Path(args.spec).read_text(encoding="utf-8"))
        tx = spec.get("typeset") or {}
        for k in ("headline", "sub", "third"):
            v = str(tx.get(k, "")).strip()
            if v:
                lines.append(v)
    if args.headline:
        lines = [args.headline] + [x for x in lines[1:] if x]
    if args.sub:
        while len(lines) < 2:
            lines.append("")
        lines[1] = args.sub
    if args.third:
        while len(lines) < 3:
            lines.append("")
        lines[2] = args.third
    for extra in args.line:
        if extra.strip():
            lines.append(extra.strip())

    lines = [x for x in lines if x.strip()]
    if not lines:
        raise SystemExit("[args] 没有任何文字：用 --spec / --headline / --line 给")
    if len(lines) > 3:
        raise SystemExit("[args] 最多三行：行数再多，底部会挤成一团")

    font_path = pick_font(args.font)
    skip = args.skip_bottom if args.skip_bottom is not None else detect_bottom_bar(img)
    top, bottom, bstat = choose_band(img, args.zone, skip)
    band_h = bottom - top

    # 字色：先按文字带亮度选，再算对比度；低于 3:1 就报警——「看不清」不是高级，是废片
    if args.color:
        color = tuple(int(args.color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    else:
        color = (38, 42, 48) if bstat["mean"] > 140 else (248, 248, 246)
    bg = (bstat["mean"],) * 3
    cr = contrast_ratio(color, bg)

    # 字号：三档按 ROLE 递减，但整体要能塞进文字带
    sizes = [max(10, int(H * ROLE[i]["size"] * args.scale)) for i in range(len(lines))]
    fonts = [ImageFont.truetype(font_path, s) for s in sizes]
    tracks = [sizes[i] * ROLE[min(i, 2)]["track"] for i in range(len(lines))]

    # 行高与总块高
    metrics = [f.getmetrics() for f in fonts]
    line_h = [max(sizes[i] * 1.15, sum(metrics[i])) for i in range(len(lines))]
    gap = max(4, int(band_h * 0.09))
    block_h = sum(line_h) + gap * (len(lines) - 1)

    # 垂直定位：块居中于文字带；块比带子高时贴底排，别顶到画面中间
    if block_h <= band_h:
        y = top + (band_h - block_h) / 2
    else:
        y = bottom - block_h - int(H * 0.02)
        y = max(int(H * 0.55), y)

    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    fill = (*color, int(255 * max(0.05, min(1.0, args.opacity))))

    placed = []
    cur_y = y
    for i, text in enumerate(lines):
        font = fonts[i]
        track = tracks[i]
        tw = tracked_width(text, font, track)
        ascent, _descent = metrics[i]
        baseline = cur_y + ascent
        x = (W - tw) / 2 if args.align == "center" else max(W * 0.06, (W - tw) / 2 - W * 0.30)
        draw_tracked(draw, x, baseline, text, font, track, fill, args.shadow)
        placed.append({
            "text": text, "size_px": sizes[i], "track_px": round(track, 1),
            "x": round(x, 1), "baseline": round(baseline, 1), "width": round(tw, 1),
        })
        cur_y += line_h[i] + gap

    if args.shadow:
        layer = layer.filter(ImageFilter.GaussianBlur(0.6))

    out = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = (out_path.suffix.lstrip(".") or "jpg").lower()
    if fmt in ("jpg", "jpeg"):
        out.save(out_path, quality=args.quality, subsampling=0, optimize=True)
    elif fmt == "webp":
        out.save(out_path, quality=args.quality, method=6)
    else:
        out.save(out_path, compress_level=6)

    report = {
        "in": src.as_posix(),
        "out": out_path.as_posix(),
        "size": [W, H],
        "font": font_path,
        "band": {"top": top, "bottom": bottom, "height": band_h, **bstat},
        "skipped_bottom_px": skip,
        "color": "#%02X%02X%02X" % color,
        "contrast_ratio": cr,
        "lines": placed,
        "block_height": round(block_h, 1),
        "warnings": [],
    }
    if cr < 3.0:
        report["warnings"].append(
            f"文字与底色的对比度只有 {cr}:1（低于 3:1）：在手机上会糊成一片灰。"
            "换 --color、抬高 --zone 让字落在更暗的位置，或者把字加大"
        )
    if band_h < H * 0.06:
        report["warnings"].append(
            f"找到的文字带只有 {band_h}px（画面高度的 {band_h / H:.1%}）："
            "这块地方本来就不够空，叠字会压到衣服或肩膀上。出图时把构图再往下留一点"
        )
    if bstat["stddev"] > 40:
        report["warnings"].append(
            f"文字带的方差 {bstat['stddev']} 很高：文字背后有强边缘（衣领、领带、肩膀线），"
            "字会有点脏。把 --zone 调小让字排得更低，或出图时把底部构图留得更空一点"
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
