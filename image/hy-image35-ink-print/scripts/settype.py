#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""排版保底：把底部两行英文（或中文）精确叠到成图上。

什么时候用：
  · 模型把标题排糊了、漏了描述行、多排了一行；
  · 标题必须 100% 正确（品牌名、人名、专有名词）；
  · 标题是中文 —— 图像模型渲染汉字是画形状不是排字，几乎必错。

做法：先用纸色把文字带刷回干净的米白，再用真字体逐字排上去。
因为是逐字绘制，字距、字号比、居中都能算准，比模型排的还整齐。

依赖 Pillow + 一个本地字体文件。

用法：
    # 英文明信片式排版（自动找系统字体）
    python settype.py out/demo/raw/x.png --title "Still Water" \
        --desc "a quiet study of mountains and reflection" --out out/demo/typed.png

    # 指定字体与位置
    python settype.py x.png --title "Still Water" --desc "..." \
        --title-font "C:/Windows/Fonts/pala.ttf" --desc-font "C:/Windows/Fonts/segoeuil.ttf" \
        --title-y 0.80 --desc-y 0.875 --out typed.png

    # 中文标题（需要中文字体）
    python settype.py x.png --title "山 水 清 音" --desc "..." \
        --title-font "C:/Windows/Fonts/STZHONGS.TTF" --out typed_cn.png
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover
    sys.stderr.write("[deps] 缺少 Pillow。安装：\n  python -m pip install pillow\n")
    raise SystemExit(2)

# 常见系统的字体候选：先「优雅衬线」，再「细体无衬线」
SERIF_CANDIDATES = [
    "C:/Windows/Fonts/pala.ttf",          # Palatino Linotype：字腔开阔，接近图录用字
    "C:/Windows/Fonts/palai.ttf",
    "C:/Windows/Fonts/georgia.ttf",
    "C:/Windows/Fonts/georgiai.ttf",
    "C:/Windows/Fonts/cambria.ttc",
    "C:/Windows/Fonts/times.ttf",
    "/System/Library/Fonts/Supplemental/Palatino.ttc",
    "/System/Library/Fonts/Supplemental/Georgia.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
]
SANS_CANDIDATES = [
    "C:/Windows/Fonts/segoeuil.ttf",      # Segoe UI Light
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/System/Library/Fonts/Supplemental/Avenir Next.ttc",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def find_font(explicit: str | None, candidates: list[str]) -> str | None:
    if explicit:
        return explicit if Path(explicit).exists() else None
    for c in candidates:
        if Path(c).exists():
            return c
    return None


def load(path: str, size: int):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return None


def text_width(font, text: str, tracking: float) -> float:
    if not text:
        return 0.0
    w = 0.0
    for i, ch in enumerate(text):
        w += font.getlength(ch)
        if i < len(text) - 1:
            w += tracking * font.size
    return w


def draw_tracked(d: ImageDraw.ImageDraw, xy, text: str, font, fill, tracking: float) -> float:
    """逐字绘制以支持字距。返回实际宽度。"""
    x, y = xy
    for i, ch in enumerate(text):
        d.text((x, y), ch, font=font, fill=fill)
        x += font.getlength(ch) + (tracking * font.size if i < len(text) - 1 else 0)
    return x - xy[0]


def fit_size(path: str, text: str, target_w: float, tracking: float,
             hi: int = 400, lo: int = 10) -> tuple[int, object]:
    """二分找字号，使文字宽度接近 target_w。"""
    best = (lo, load(path, lo))
    while lo <= hi:
        mid = (lo + hi) // 2
        f = load(path, mid)
        if f is None:
            break
        w = text_width(f, text, tracking)
        if w <= target_w:
            best = (mid, f)
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def sample_paper(img: Image.Image) -> tuple[int, int, int]:
    """从画面上缘中带取纸色（那里几乎一定是纯纸白）。"""
    w, h = img.size
    box = (int(w * 0.35), int(h * 0.01), int(w * 0.65), max(int(h * 0.01) + 6, int(h * 0.03)))
    crop = img.crop(box)
    px = crop.load()
    cw, ch = crop.size
    vals = [px[x, y] for y in range(ch) for x in range(cw)]
    vals.sort(key=lambda p: p[0] + p[1] + p[2])
    mid = vals[len(vals) // 2]
    return mid


def main() -> int:
    ap = argparse.ArgumentParser(description="把两行文字精确叠到成图上（排版保底）")
    ap.add_argument("image")
    ap.add_argument("--title", required=True)
    ap.add_argument("--desc", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("--paper", default="", help="纸色 #RRGGBB；不填则从画面上缘取样")
    ap.add_argument("--ink", default="#2E2C27", help="文字墨色（默认暖黑，不用纯黑）")
    ap.add_argument("--title-font", default=None)
    ap.add_argument("--desc-font", default=None)
    ap.add_argument("--title-y", type=float, default=0.80, help="标题行中心（画幅高度的比例）")
    ap.add_argument("--desc-y", type=float, default=0.875, help="描述行中心")
    ap.add_argument("--title-width", type=float, default=0.46, help="标题宽度占画幅宽度的比例")
    ap.add_argument("--ratio", type=float, default=2.4, help="标题字号 / 描述字号（默认 2.4）")
    ap.add_argument("--tracking-title", type=float, default=0.01, help="标题字距（相对字号）")
    ap.add_argument("--tracking-desc", type=float, default=0.16, help="描述字距（相对字号）")
    ap.add_argument("--clear-from", type=float, default=None,
                    help="从这个高度往下刷成纸色（默认 title_y - 0.055）")
    ap.add_argument("--meta", default=None, help="读 build_print_prompt.py 的 meta.json 取 layout")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    src = Path(args.image)
    if not src.exists():
        print(f"[error] 成图不存在：{src}", file=sys.stderr)
        return 2

    img = Image.open(src).convert("RGB")
    W, H = img.size

    if args.meta and Path(args.meta).exists():
        meta = json.loads(Path(args.meta).read_text(encoding="utf-8"))
        lo = meta.get("layout") or {}
        args.title_y = float(lo.get("title_y", args.title_y))
        args.desc_y = float(lo.get("desc_y", args.desc_y))

    tfont_path = find_font(args.title_font, SERIF_CANDIDATES)
    dfont_path = find_font(args.desc_font, SANS_CANDIDATES)
    if not tfont_path:
        print("[error] 找不到可用的衬线字体。用 --title-font 指定一个 .ttf/.ttc 路径", file=sys.stderr)
        return 2
    if args.desc and not dfont_path:
        print("[error] 找不到可用的无衬线字体。用 --desc-font 指定", file=sys.stderr)
        return 2

    paper = tuple(int(args.paper.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)) if args.paper else sample_paper(img)
    ink = tuple(int(args.ink.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))

    # 1) 把文字带刷回干净纸白
    clear_y = int(H * (args.clear_from if args.clear_from is not None else args.title_y - 0.055))
    d = ImageDraw.Draw(img)
    d.rectangle([0, clear_y, W, H], fill=paper)

    # 2) 标题：按目标宽度二分字号
    target_w = W * args.title_width
    tsize, tfont = fit_size(tfont_path, args.title, target_w, args.tracking_title)
    tw = text_width(tfont, args.title, args.tracking_title)
    t_h = tfont.getbbox("Hg")[3] - tfont.getbbox("Hg")[1]
    ty = int(H * args.title_y) - t_h // 2
    draw_tracked(d, ((W - tw) / 2, ty), args.title, tfont, ink, args.tracking_title)

    # 3) 描述：字号 = 标题 / ratio；宽度超出画幅 80% 时往下缩，
    #    缩到 ratio=3.4（版面体检能接受的上限）就到底——再小就不是注脚，是免责声明了。
    info = {"title_size": tsize, "title_width_ratio": round(tw / W, 3)}
    if args.desc:
        dsize = max(8, int(round(tsize / args.ratio)))
        floor = max(8, int(round(tsize / 3.4)))
        dfont = load(dfont_path, dsize)
        if dfont is None:
            print(f"[error] 字体加载失败：{dfont_path}", file=sys.stderr)
            return 2
        max_w = W * 0.80
        while text_width(dfont, args.desc, args.tracking_desc) > max_w and dsize > floor:
            dsize -= 1
            dfont = load(dfont_path, dsize)
        dw = text_width(dfont, args.desc, args.tracking_desc)
        if dw > max_w:
            print(f"[warn] 描述行仍超出画幅 80%：{args.desc!r} 太长。"
                  "缩到 ratio=3.4 就到底了，建议把描述压到 12 个词以内，"
                  "或者调大 --title-width", file=sys.stderr)
        d_h = dfont.getbbox("Hg")[3] - dfont.getbbox("Hg")[1]
        dy = int(H * args.desc_y) - d_h // 2
        draw_tracked(d, ((W - dw) / 2, dy), args.desc, dfont, ink, args.tracking_desc)
        info.update({
            "desc_size": dsize,
            "desc_width_ratio": round(dw / W, 3),
            "real_ratio": round(tsize / dsize, 2),
        })

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, quality=95)
    print(f"[write] {out}")
    print(f"[paper] {paper}   [ink] {ink}")
    print(f"[type]  标题 {info['title_size']}px / 宽 {info['title_width_ratio'] * 100:.0f}%"
          + (f"  描述 {info.get('desc_size')}px / 宽 {info.get('desc_width_ratio', 0) * 100:.0f}%"
             f"  实比 {info.get('real_ratio')}" if args.desc else ""))
    print("[note] 字是逐字排的，字距与居中都是精确值；如需再调，改 --title-width / --ratio 即可")
    return 0


if __name__ == "__main__":
    sys.exit(main())
