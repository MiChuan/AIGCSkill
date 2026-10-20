#!/usr/bin/env python3
"""印章成图体检 + 收尾。

四件事：
  1. 找出印面（不靠色相，靠"与纸色的差异"，所以朱砂红、仿古褐、墨黑都适用）
  2. 按圆形/方形掩膜清掉印面以外的一切（模型常画的桌面、阴影、纸角、水印都在这里被一起带走）
  3. 量化三个指标：印泥覆盖率、色相集中度、灰阶渗漏
  4. 裁正方形、放缩到交付尺寸、可选导出透明底 PNG

依赖：Pillow

用法：
    python sealcut.py raw.png --shape round --out seal_4k.png --size 4096
    python sealcut.py raw.png --check-only
    python sealcut.py raw.png --shape square --paper white --alpha
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

try:
    from PIL import Image, ImageChops, ImageFilter
except ImportError:
    print("[error] 需要 Pillow：pip install pillow", file=sys.stderr)
    raise SystemExit(2)


def saturated_mask(img: Image.Image, thr: int = 64) -> Image.Image:
    """高饱和像素（朱砂/朱膘/西洋红这类印泥）：用来把印面从灰调背景里摘出来。"""
    s = img.convert("HSV").split()[1]
    return s.point(lambda v: 255 if v >= thr else 0)

# 判定阈值
INK_DIFF = 42          # 与纸色的最大通道差超过此值 → 判为印泥
MIN_HUE_CONCENTRATION = 0.80
MAX_GREY_LEAK = 0.40
STAT_SIDE = 600        # 统计用缩略图边长（全分辨率跑循环太慢，指标不需要）

# 印泥覆盖率区间：朱文是红线条（红少白多），白文是红底白字（红多白少），
# 用同一套阈值会把正常的白文判成"糊成一团"。
COVERAGE_RANGE = {
    "zhuwen": (0.06, 0.60),
    "baiwen": (0.30, 0.95),
}


# ---------------------------------------------------------------- 基础

def paper_reference(img: Image.Image) -> tuple[int, int, int]:
    """取画面四条边带的中位色作为"纸色"基准。中位数对贴边的印章不敏感。"""
    W, H = img.size
    band = max(4, int(min(W, H) * 0.04))
    strips = [
        img.crop((0, 0, W, band)),
        img.crop((0, H - band, W, H)),
        img.crop((0, 0, band, H)),
        img.crop((W - band, 0, W, H)),
    ]
    samples: list[tuple[int, int, int]] = []
    for s in strips:
        raw = s.convert("RGB").resize((32, 32), Image.BILINEAR).tobytes()
        samples.extend(tuple(raw[i:i + 3]) for i in range(0, len(raw), 3))
    rgb = sorted(samples, key=lambda p: p[0] * 0.299 + p[1] * 0.587 + p[2] * 0.114)
    return rgb[len(rgb) // 2]


def ink_mask(img: Image.Image, paper: tuple[int, int, int], thr: int = INK_DIFF) -> Image.Image:
    """返回 L 掩膜：255 = 印泥，0 = 纸。"""
    r, g, b = img.convert("RGB").split()
    pr, pg, pb = paper

    def diff(ch: Image.Image, ref: int) -> Image.Image:
        return ch.point(lambda v: abs(v - ref))

    d = Image.new("L", img.size, 0)
    for ch in (diff(r, pr), diff(g, pg), diff(b, pb)):
        d = ImageChops.lighter(d, ch)
    return d.point(lambda v: 255 if v >= thr else 0)


def find_seal_box(mask: Image.Image) -> tuple[int, int, int, int] | None:
    """用行/列投影找印面主体，对零星噪点不敏感。"""
    W, H = mask.size
    px = mask.load()
    step = max(1, min(W, H) // 480)          # 大图抽样扫描，快 10 倍以上
    cols = [0] * W
    rows = [0] * H
    for y in range(0, H, step):
        for x in range(0, W, step):
            if px[x, y]:
                cols[x] += 1
                rows[y] += 1

    def bounds(arr: list[int], total: int) -> tuple[int, int] | None:
        cut = max(2, int(total * 0.012))
        idx = [i for i, v in enumerate(arr) if v >= cut]
        if not idx:
            return None
        return idx[0], idx[-1]

    cb = bounds(cols, H // step)
    rb = bounds(rows, W // step)
    if not cb or not rb:
        return None
    x0, x1 = cb
    y0, y1 = rb
    return x0, y0, x1 - x0 + 1, y1 - y0 + 1


def plausible_box(box: tuple[int, int, int, int], size: tuple[int, int]) -> bool:
    """印面外接框应该近正方形、占画面合理比例——用来否掉被阴影/桌面撑大的框。"""
    W, H = size
    _, _, w, h = box
    if w < 40 or h < 40:
        return False
    ar = w / h
    fill = max(w, h) / max(W, H)
    return 0.72 <= ar <= 1.38 and 0.20 <= fill <= 0.97


def shape_mask(box: tuple[int, int, int, int], shape: str, pad_ratio: float,
               img_size: tuple[int, int]) -> Image.Image:
    """整图尺寸的掩膜，255 = 保留（印面内），0 = 清除（印面外）。"""
    W, H = img_size
    x, y, w, h = box
    m = Image.new("L", (W, H), 0)
    draw_px = m.load()
    cx, cy = x + w / 2.0, y + h / 2.0

    if shape == "round":
        r = min(w, h) / 2.0 * (1 + pad_ratio)
        r2 = r * r
        for yy in range(max(0, int(cy - r)), min(H, int(cy + r) + 1)):
            dy = yy - cy
            span = math.sqrt(max(0.0, r2 - dy * dy))
            xa, xb = int(cx - span), int(cx + span)
            for xx in range(max(0, xa), min(W, xb + 1)):
                draw_px[xx, yy] = 255
    else:  # square
        pw = int(w * pad_ratio / 2)
        ph = int(h * pad_ratio / 2)
        xa, xb = max(0, x - pw), min(W, x + w + pw)
        ya, yb = max(0, y - ph), min(H, y + h + ph)
        for yy in range(ya, yb):
            for xx in range(xa, xb):
                draw_px[xx, yy] = 255

    return m.filter(ImageFilter.GaussianBlur(1.2))


def clean_outside(img: Image.Image, mask: Image.Image,
                  fill: tuple[int, int, int]) -> Image.Image:
    """掩膜外的所有东西统一成纸色：桌面、阴影、纸角、水印都会在这一步消失。"""
    flat = Image.new("RGB", img.size, fill)
    out = Image.composite(img.convert("RGB"), flat, mask)
    # 掩膜边缘再收一遍，避免残留一圈脏边
    return out


def flatten_paper(img: Image.Image, mask: Image.Image, fill: tuple[int, int, int],
                  sat_thr: int = 38, dark_gap: int = 12,
                  radius: float = 26.0) -> tuple[Image.Image, float]:
    """把印面内的"纸白区"归正到纸色，清掉压在印面里的柔和阴影/灰雾。

    只动"低饱和 + 明显暗于纸色"的像素：
      - 朱砂/朱膘/西洋红这类饱和印泥的 S 远高于阈值，不会被碰；
      - 白文印的"字"本来就该是纸白，归正后更干净；
      - 墨色低饱和印泥会被误伤，所以只在高饱和印泥场景启用。

    做法是「换低频、留高频」：把区域内的低频（大半径模糊）替换成纯纸色，
    再叠回原有的纸纹高频。直接填纯色会在纸面上留下一块过分平整的白斑。
    """
    rgb = img.convert("RGB")
    hsv = img.convert("HSV")
    _, s, v = hsv.split()
    paper_v = max(fill)

    dark = v.point(lambda x: 255 if x < paper_v - dark_gap else 0)
    lowsat = s.point(lambda x: 255 if x < sat_thr else 0)
    sel = ImageChops.multiply(ImageChops.multiply(dark, lowsat), mask)

    raw = sel.tobytes()
    hit = sum(1 for b in raw[::7] if b) * 7 / max(1, len(raw))

    # low_freq = 先抹掉印泥再模糊：否则红色会被高斯拖到邻近纸面上，
    # 在笔画边缘提亮出一圈白晕。抹掉印泥后模糊出的低频只含背景明暗。
    ink_hi = saturated_mask(rgb, 64).filter(ImageFilter.MaxFilter(9))
    clean = Image.composite(Image.new("RGB", rgb.size, fill), rgb, ink_hi)
    low = clean.filter(ImageFilter.GaussianBlur(radius))

    # leveled = 原图 + (纸色 - 低频) = 去掉低频明暗、保住纸纹高频
    flat = Image.new("RGB", rgb.size, fill)
    delta = ImageChops.subtract(flat, low, 1.0, 128)
    leveled = ImageChops.add(rgb, delta, 1.0, -128)

    sel = sel.filter(ImageFilter.GaussianBlur(1.4))
    out = Image.composite(leveled, rgb, sel)
    return out, round(hit, 4)


def crop_square(img: Image.Image, box: tuple[int, int, int, int],
                pad_ratio: float, fill: tuple[int, int, int]) -> Image.Image:
    """以印面为中心裁一个正方形，边长 = 长边 ×(1+pad)。超出画面的部分用纸色补。"""
    W, H = img.size
    x, y, w, h = box
    side = int(max(w, h) * (1 + pad_ratio * 2))
    cx, cy = x + w / 2.0, y + h / 2.0
    left = int(cx - side / 2)
    top = int(cy - side / 2)
    canvas = Image.new("RGB", (side, side), fill)
    src = img.crop((left, top, left + side, top + side)) if (
        left >= 0 and top >= 0 and left + side <= W and top + side <= H
    ) else None
    if src is not None:
        canvas.paste(src, (0, 0))
    else:
        # 跨出边界：逐块粘贴，越界处保留纸色
        sx0, sy0 = max(0, left), max(0, top)
        sx1, sy1 = min(W, left + side), min(H, top + side)
        canvas.paste(img.crop((sx0, sy0, sx1, sy1)), (sx0 - left, sy0 - top))
    return canvas


# ---------------------------------------------------------------- 量化

def measure(img: Image.Image, mask_small: Image.Image,
            paper: tuple[int, int, int]) -> dict:
    """在缩略图上统计，兼顾速度和稳定性。"""
    small = img.convert("RGB").resize((STAT_SIDE, STAT_SIDE), Image.BILINEAR)
    m = mask_small.resize((STAT_SIDE, STAT_SIDE), Image.BILINEAR)
    px = small.load()
    mp = m.load()
    hsv = small.convert("HSV").load()

    total = ink = 0
    low_sat_ink = 0
    hue_bins = [0] * 36
    lum_ink: list[int] = []

    for y in range(STAT_SIDE):
        for x in range(STAT_SIDE):
            if mp[x, y] < 128:
                continue                      # 只看印面容差内
            total += 1
            r, g, b = px[x, y]
            diff = max(abs(r - paper[0]), abs(g - paper[1]), abs(b - paper[2]))
            if diff < INK_DIFF:
                continue
            ink += 1
            hh, ss, vv = hsv[x, y]
            lum_ink.append(vv)
            if ss < 38:
                low_sat_ink += 1
            else:
                hue_bins[hh * 36 // 256] += 1

    coverage = ink / max(1, total)
    sat_ink = sum(hue_bins)
    grey_leak = low_sat_ink / max(1, ink)

    hue_conc = None
    hue_mean_deg = None
    if sat_ink >= 40:
        sx = sum(c * math.cos(2 * math.pi * i / 36) for i, c in enumerate(hue_bins))
        sy = sum(c * math.sin(2 * math.pi * i / 36) for i, c in enumerate(hue_bins))
        hue_conc = round(math.hypot(sx, sy) / sat_ink, 3)
        hue_mean_deg = round(math.degrees(math.atan2(sy, sx)) % 360, 1)

    return {
        "ink_coverage": round(coverage, 3),
        "hue_concentration": hue_conc,
        "hue_mean_deg": hue_mean_deg,
        "grey_leak": round(grey_leak, 3),
        "ink_value_median": (sorted(lum_ink)[len(lum_ink) // 2] if lum_ink else None),
        "sampled_px": total,
    }


def to_alpha(img: Image.Image, paper: tuple[int, int, int]) -> Image.Image:
    """把纸色转成透明：印泥越浓越不透明，边缘的淡晕保持半透明。

    alpha 由"与纸色的差"推出，所以对朱砂、仿古、墨色都成立；
    印泥本身的 RGB 保留，贴到任何底上都不会出现纸色的白边。
    """
    rgb = img.convert("RGB")
    d = Image.new("L", rgb.size, 0)
    for ch, ref in zip(rgb.split(), paper):
        d = ImageChops.lighter(d, ch.point(lambda v, r=ref: abs(v - r)))

    # 用 97 分位（而不是最大值）当满值：印泥核心区直接不透明，
    # 只有真正淡的晕边才半透明。用最大值会让整枚印章都偏透。
    hist = d.histogram()
    total = sum(hist)
    acc = 0
    top = 255
    for v in range(255, -1, -1):
        acc += hist[v]
        if acc >= total * 0.03:
            top = v
            break
    top = max(INK_DIFF, top)

    # 死区 12：与纸色差不到 12 的像素（纸纹、残留的极淡阴影）一律全透明，
    # 否则贴到深色底上会浮出一层脏灰。
    floor = 12
    span = max(1, top - floor)
    alpha = d.point(lambda v: 0 if v <= floor
                    else min(255, int(((v - floor) / span) ** 0.85 * 255)))
    out = rgb.convert("RGBA")
    out.putalpha(alpha)
    return out


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="印章成图体检 + 收尾")
    ap.add_argument("input", help="模型出的原始图")
    ap.add_argument("--out", help="输出路径（.png / .jpg）")
    ap.add_argument("--shape", default="auto", choices=["round", "square", "auto"],
                    help="印面形状；auto 只体检不净化")
    ap.add_argument("--seal-type", default="zhuwen", choices=["zhuwen", "baiwen"],
                    help="朱文（默认，红线条）/ 白文（红底白字），决定覆盖率阈值")
    ap.add_argument("--pad", type=float, default=0.015,
                    help="掩膜外扩比例，默认 0.015（保留印泥外溢的晕边）")
    ap.add_argument("--box", help="手动指定印面外接框 x,y,w,h（自动定位不准时用）")
    ap.add_argument("--paper", default="auto", choices=["auto", "white", "keep"],
                    help="印面外的纸面颜色：auto=沿用原纸色，white=纯白，keep=不动")
    ap.add_argument("--flatten-paper", default="auto", choices=["auto", "on", "off"],
                    help="印面内的纸白归正（清掉压在圆里的柔和阴影），默认 auto")
    ap.add_argument("--crop", action="store_true", help="裁成以印面为中心的正方形")
    ap.add_argument("--size", default="native", help="输出边长，如 4096；native 保持原尺寸")
    ap.add_argument("--alpha", action="store_true", help="额外导出透明底 PNG（_alpha.png）")
    ap.add_argument("--check-only", action="store_true", help="只体检，不写图")
    ap.add_argument("--json", help="把体检报告写到指定路径")
    ap.add_argument("--strict", action="store_true", help="有问题时以退出码 2 结束")
    args = ap.parse_args()

    src = Path(args.input)
    if not src.exists():
        print(f"[error] 找不到输入图：{src}", file=sys.stderr)
        return 2

    img = Image.open(src).convert("RGB")
    W, H = img.size
    report: dict = {"input": str(src), "in_size": [W, H], "seal_type": args.seal_type, "problems": []}

    if abs(W - H) > max(8, W * 0.02):
        report["problems"].append(
            f"画布不是正方形（{W}×{H}）：圆形印面会被压成椭圆，建议重出为 1:1"
        )

    paper = paper_reference(img)
    report["paper_color"] = "#%02X%02X%02X" % paper

    mask = ink_mask(img, paper)
    sat = saturated_mask(img)

    box = None
    box_src = None
    if args.box:
        try:
            box = tuple(int(v) for v in args.box.replace("，", ",").split(","))
            if len(box) != 4:
                raise ValueError
        except ValueError:
            print(f"[error] --box 需要 x,y,w,h 四个整数，收到 {args.box}", file=sys.stderr)
            return 2
        box_src = "manual"
    else:
        # 先试高饱和印泥（朱砂/朱膘/西洋红）——能把印面从灰调背景、桌面阴影里摘干净；
        # 低饱和印泥（墨/深褐）摘不出来，退回"与纸色差异"的掩膜。
        for cand, tag in ((sat, "saturated"), (mask, "diff")):
            b = find_seal_box(cand)
            if b and plausible_box(b, (W, H)):
                box, box_src = b, tag
                break
        if box is None:
            box = find_seal_box(mask) or find_seal_box(sat)
        if box is None:
            print("[error] 图里找不到印面：整张图都接近纸色，或印泥与纸色对比不足", file=sys.stderr)
            return 2
        if box_src is None:
            box_src = "fallback"
            report["problems"].append(
                "印面定位不可靠（可能是墨色印泥 + 灰色背景，或印面太靠边）："
                "净化结果请人工确认，必要时用 --box x,y,w,h 手动框定印面"
            )

    x, y, w, h = box
    report["seal_box"] = [x, y, w, h]
    report["seal_box_source"] = box_src
    report["seal_fill_ratio"] = round(max(w, h) / max(W, H), 3)

    shape = args.shape
    if shape == "auto":
        # 用四角是否被印泥占据来猜形状：圆印的四角一定干净
        corners = []
        for cx0, cy0 in ((x, y), (x + w - 1, y), (x, y + h - 1), (x + w - 1, y + h - 1)):
            corners.append(1 if mask.getpixel((cx0, cy0)) else 0)
        shape = "square" if sum(corners) >= 2 else "round"
        report["shape_guessed"] = shape
    report["shape"] = shape

    if shape == "square" and 0.85 < w / max(1, h) < 1.15:
        pass
    elif shape == "round" and not (0.85 < w / max(1, h) < 1.15):
        report["problems"].append(
            f"指定了圆形，但印面外接框是 {w}×{h}（宽高比 {w / h:.2f}）：可能出了椭圆或被裁切"
        )

    smask = shape_mask(box, shape, args.pad, (W, H))
    stats = measure(img, smask, paper)
    report.update(stats)

    cov = stats["ink_coverage"]
    lo, hi = COVERAGE_RANGE[args.seal_type]
    if cov < lo:
        report["problems"].append(
            f"印泥覆盖率只有 {cov:.0%}（{args.seal_type} 下限 {lo:.0%}）：印面接近空白，"
            "图案线条太细或根本没刻出来"
        )
    elif cov > hi:
        report["problems"].append(
            f"印泥覆盖率 {cov:.0%} 超过 {args.seal_type} 上限 {hi:.0%}：印面糊成一团、留白不足"
        )

    hc = stats["hue_concentration"]
    if hc is None:
        report["problems"].append(
            "印泥是低饱和色（墨/深褐），色相检查跳过；请人工确认画面没有混入第二种颜色"
        )
    elif hc < MIN_HUE_CONCENTRATION:
        report["problems"].append(
            f"色相集中度 {hc:.2f} < {MIN_HUE_CONCENTRATION}：印泥不是单色，"
            "画面上混入了其他色相（紫/橙/绿偏移），需要重出"
        )

    if stats["grey_leak"] > MAX_GREY_LEAK:
        report["problems"].append(
            f"印面内低饱和像素占 {stats['grey_leak']:.0%}：混入大量灰阶/白噪点，"
            "通常是把照片的灰阶直接搬进了印面"
        )

    if x <= 2 or y <= 2 or x + w >= W - 2 or y + h >= H - 2:
        report["problems"].append("印面贴到了画布边缘：可能出血被裁，建议重出时让印面完整居中")

    report["verdict"] = "clean" if not report["problems"] else "check"

    # ---- 出图
    out_img = img
    fill = (255, 255, 255) if args.paper == "white" else paper
    if shape != "auto" and args.paper != "keep":
        out_img = clean_outside(out_img, smask, fill)
        report["cleaned_outside"] = True

        # 印面内残留的灰雾/阴影：auto 时只在"高饱和印泥 + 确有暗灰块"时才动手
        do_flat = args.flatten_paper == "on" or (
            args.flatten_paper == "auto" and box_src == "saturated"
        )
        if do_flat:
            # 纯白底时，印面内的纸白也要一起归一成纯白（此时"暗于纸色"这个条件放开）
            gap = -999 if args.paper == "white" else 12
            out_img, hit = flatten_paper(out_img, smask, fill, dark_gap=gap)
            report["flatten_paper_hit"] = hit
            if hit < 0.004 and args.flatten_paper == "on":
                report["problems"].append(
                    f"开了 --flatten-paper 但只命中 {hit:.2%} 的像素：印面本来就干净，或印泥是低饱和色被跳过"
                )

    if args.crop:
        out_img = crop_square(out_img, box, args.pad, fill)
        report["cropped"] = True

    target = args.size
    if target != "native":
        try:
            side = int(target)
        except ValueError:
            print(f"[error] --size 只接受整数或 native，收到 {target}", file=sys.stderr)
            return 2
        if max(out_img.size) != side:
            out_img = out_img.resize((side, side), Image.LANCZOS)
            out_img = out_img.filter(ImageFilter.UnsharpMask(radius=1.4, percent=70, threshold=3))
        report["out_size"] = list(out_img.size)
    else:
        report["out_size"] = list(out_img.size)

    if not args.check_only:
        if not args.out:
            print("[error] 需要 --out 或加 --check-only", file=sys.stderr)
            return 2
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if out_path.suffix.lower() in (".jpg", ".jpeg"):
            out_img.save(out_path, quality=96, subsampling=0)
        else:
            out_img.save(out_path)
        report["output"] = str(out_path)

        if args.alpha:
            alpha_path = out_path.with_name(out_path.stem + "_alpha.png")
            to_alpha(out_img, paper if args.paper != "white" else (255, 255, 255)).save(alpha_path)
            report["alpha_output"] = str(alpha_path)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.strict and report["problems"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
