#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从参考原图提取印刷色板（米白纸 + 水墨平涂配色）。

这个脚本解决的是本 skill 唯一无法靠文字说清的事：**颜色到底取哪几个**。

做三件事：
  1. 只取画面上半部分（--top 默认 0.55）。下半部分通常是地面、倒影、杂物，
     留着只会把色板拉脏。
  2. k-means 聚出 5–8 个主色簇，按面积排序，给出占比。占比低于 3% 的簇标成
     drop —— 那就是原图里的「细碎纹理」，正是要剔除的东西。
  3. 把原色改写成**能印在米白纸上**的颜色（flat）：色相一个不动，饱和度乘 0.55
     （上限 0.42）、明度抬到 0.34–0.96。原图的深绿和纯黑直接搬到纸上，会和纸白
     打架、并且看起来像屏幕截图而不像印刷品。

输出 palette.json（给 build_print_prompt.py 用）+ 一张色卡 PNG（给人看）。
纯 Pillow + 标准库，无 numpy。

用法：
    python extract_palette.py assets/ref.jpg --outdir out/pal
    python extract_palette.py assets/ref.jpg --outdir out/pal --k 7 --top 0.5
    python extract_palette.py assets/ref.jpg --outdir out/pal --no-crop   # 取全图
"""

from __future__ import annotations

import argparse
import colorsys
import json
import math
import random
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover
    sys.stderr.write("[deps] 缺少 Pillow。安装：\n  python -m pip install pillow\n")
    raise SystemExit(2)

# 米白哑光特种纸的纸白。不用纯白：纯白会让整张图变成「PPT 背景」。
PAPER = {"hex": "#F4EFE4", "rgb": (244, 239, 228)}

# 平涂改写区间。
# 饱和度只降不升：给一个 0.06 的下界会把本来近中性的浅色硬推成彩灰，
# 那会让「大面积留白的纸」失去中性的骨架。上界 0.42 是「淡彩」与「彩色印刷」的分界。
# 明度下界 0.34 是「墨色能站住」的底线（再低纸白会吃掉形状），上界留给纸白自己。
FLAT_S_LO, FLAT_S_HI = 0.0, 0.42
FLAT_V_LO, FLAT_V_HI = 0.34, 0.96

# 低于这个占比的色簇视为「细碎纹理」，标注 drop
NOISE_SHARE = 0.03


# ---------------------------------------------------------------- 颜色工具

def hex2rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def rgb2hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*[max(0, min(255, int(round(v)))) for v in rgb])


def rgb2hsv(rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    """返回 (色相°, HSV 饱和度, 明度值)。

    饱和度用 HSV 而不是 HLS：HLS 的 S 对浅色极敏感，米白 (244,239,228) 在 HLS 里
    S=0.42，在 HSV 里只有 0.065。用 HLS 会把一张米白纸判成「高饱和色」。
    """
    r, g, b = [v / 255.0 for v in rgb]
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    return h * 360.0, s, v


def hsv2rgb(h: float, s: float, v: float) -> tuple[int, int, int]:
    r, g, b = colorsys.hsv_to_rgb((h % 360.0) / 360.0, s, v)
    return (int(round(r * 255)), int(round(g * 255)), int(round(b * 255)))


def luma(rgb: tuple[float, float, float]) -> float:
    r, g, b = [v / 255.0 for v in rgb]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def dist(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    # 加权欧氏：人眼对绿最敏感，对蓝最钝。不做色彩空间转换也够分簇。
    dr, dg, db = a[0] - b[0], a[1] - b[1], a[2] - b[2]
    return math.sqrt(2.0 * dr * dr + 4.0 * dg * dg + 3.0 * db * db)


def flatten(rgb: tuple[int, int, int]) -> dict:
    """原色 → 能印在米白纸上的平涂色。色相不动，S/V 改写。"""
    h, s, v = rgb2hsv(rgb)
    s2 = min(max(s * 0.55, FLAT_S_LO), FLAT_S_HI)
    v2 = min(FLAT_V_LO + (FLAT_V_HI - FLAT_V_LO) * v, FLAT_V_HI)
    out = hsv2rgb(h, s2, v2)
    return {
        "hex": rgb2hex(out),
        "rgb": list(out),
        "h": round(h, 1),
        "s": round(s2, 3),
        "v": round(v2, 3),
        "note": _flat_note(h, s, v, s2, v2),
    }


def _flat_note(h: float, s: float, v: float, s2: float, v2: float) -> str:
    """给提示词用的一句人话，说明这个色在原图里是什么、被改成了什么。"""
    parts = []
    if v < 0.22:
        parts.append("原图近黑，改写为中深墨色（保留色相偏向）")
    elif v > 0.90:
        parts.append("原图近白，只做极浅的灰调，不与纸白抢位")
    if s > 0.60 and s2 <= 0.42:
        parts.append("原图高饱和，压到淡彩")
    if h < 20 or h > 340:
        parts.append("暖红/褐系")
    elif h < 45:
        parts.append("土黄褐系")
    elif h < 70:
        parts.append("黄绿系")
    elif h < 170:
        parts.append("青绿系")
    elif h < 200:
        parts.append("青系")
    elif h < 255:
        parts.append("蓝系")
    elif h < 290:
        parts.append("紫系")
    else:
        parts.append("品红系")
    parts.append(f"V {v:.2f}→{v2:.2f}，S {s:.2f}→{s2:.2f}")
    return "；".join(parts)


# ---------------------------------------------------------------- k-means

def sample_pixels(img: Image.Image, limit: int = 6000, seed: int = 7) -> list[tuple[float, float, float]]:
    """等距抽样。等距比随机更能代表全图，且可复现。"""
    w, h = img.size
    total = w * h
    step = max(1, total // limit)
    px = img.load()
    out: list[tuple[float, float, float]] = []
    i = 0
    for y in range(h):
        for x in range(w):
            i += 1
            if i % step:
                continue
            out.append(px[x, y])
            if len(out) >= limit:
                return out
    return out


def kmeans(points: list[tuple[float, float, float]], k: int, iters: int = 14, seed: int = 7):
    rnd = random.Random(seed)
    # k-means++ 初始化：第一个随机，之后按「离已选中心最远」的概率抽
    centers = [points[rnd.randrange(len(points))]]
    while len(centers) < k:
        d2 = []
        for p in points:
            m = min(dist(p, c) for c in centers)
            d2.append(m * m)
        total = sum(d2) or 1.0
        r = rnd.random() * total
        acc = 0.0
        pick = points[-1]
        for p, w in zip(points, d2):
            acc += w
            if acc >= r:
                pick = p
                break
        centers.append(pick)

    assign = [0] * len(points)
    for _ in range(iters):
        moved = False
        for i, p in enumerate(points):
            best, bd = 0, float("inf")
            for j, c in enumerate(centers):
                d = dist(p, c)
                if d < bd:
                    best, bd = j, d
            if assign[i] != best:
                assign[i] = best
                moved = True
        sums = [[0.0, 0.0, 0.0, 0] for _ in centers]
        for p, a in zip(points, assign):
            s = sums[a]
            s[0] += p[0]
            s[1] += p[1]
            s[2] += p[2]
            s[3] += 1
        for j, s in enumerate(sums):
            if s[3]:
                centers[j] = (s[0] / s[3], s[1] / s[3], s[2] / s[3])
        if not moved:
            break

    counts = [0] * len(centers)
    for a in assign:
        counts[a] += 1
    n = len(points)
    clusters = [
        {
            "rgb": tuple(int(round(v)) for v in c),
            "share": counts[j] / n,
        }
        for j, c in enumerate(centers)
        if counts[j] > 0
    ]
    clusters.sort(key=lambda c: -c["share"])
    return clusters


# ---------------------------------------------------------------- 色卡

def draw_swatch(pal: dict, path: Path, width: int = 880) -> None:
    rows = len(pal["clusters"]) + 1
    row_h = 78
    head = 96
    height = head + rows * row_h + 26
    img = Image.new("RGB", (width, height), PAPER["rgb"])
    d = ImageDraw.Draw(img)

    title_f = _font(26)
    mono = _font(19)
    small = _font(16)

    d.text((46, 30), "INK PRINT PALETTE", font=title_f, fill=(43, 42, 38))
    d.text((46, 62), "paper #F4EFE4 · flat = 能印在纸上的改写色 · src = 原图实测色",
           font=small, fill=(120, 114, 102))

    y = head
    for i, c in enumerate(pal["clusters"]):
        flat = c["flat"]
        # 左：原色；右：平涂改写色
        d.rectangle([46, y + 8, 46 + 150, y + 62], fill=tuple(c["rgb"]), outline=(210, 204, 192))
        d.rectangle([206, y + 8, 206 + 150, y + 62], fill=tuple(flat["rgb"]), outline=(210, 204, 192))

        role = c["role"]
        line1 = f"{i + 1:>2}. {c['hex']}  →  {flat['hex']}   {c['share'] * 100:5.1f}%   {role}"
        d.text((386, y + 12), line1, font=mono, fill=(43, 42, 38))
        d.text((386, y + 38), flat["note"], font=small, fill=(120, 114, 102))
        y += row_h

    ink = pal["ink"]
    d.rectangle([46, y + 8, 46 + 150, y + 62], fill=tuple(ink["rgb"]), outline=(210, 204, 192))
    d.rectangle([206, y + 8, 206 + 150, y + 62], fill=PAPER["rgb"], outline=(210, 204, 192))
    d.text((386, y + 12), f"ink  {ink['hex']}   用于文字与最深的一层墨", font=mono, fill=(43, 42, 38))
    d.text((386, y + 38), "文字不用纯黑：纯黑在米白纸上像屏幕像素，不像墨", font=small, fill=(120, 114, 102))

    img.save(path)


def _font(size: int):
    # 色卡里既有英文也有中文注释，所以要找带 CJK 字形的字体。
    # segoeui / arial 都不含汉字，用了中文会变成一排方块。
    for name in ("msyh.ttc", "msyhl.ttc", "simhei.ttf", "simsun.ttc",
                 "PingFang.ttc", "NotoSansCJK-Regular.ttc",
                 "segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10
        return ImageFont.load_default()


# ---------------------------------------------------------------- 主流程

def pick_ink(clusters: list[dict]) -> dict:
    """墨色 = 最暗那一簇的色相 + 被压到几乎中性的低明度暖黑。"""
    darkest = min(clusters, key=lambda c: luma(c["rgb"]))
    h, s, _ = rgb2hsv(darkest["rgb"])
    out = hsv2rgb(h, min(s, 0.12), 0.155)
    return {"hex": rgb2hex(out), "rgb": list(out), "from": darkest["hex"]}


def merge_similar(clusters: list[dict], tol: float = 22.0) -> list[dict]:
    """合并几乎同色的簇。

    真实照片的天空、水面、雾会在一整片同一色相里被 k-means 切成三四段，
    每段占 10%–30%，色值差不到 20 级。把这些都写进提示词，等于给模型
    「五个几乎一样的灰绿」——它只会挑一个用，剩下四个变成噪音。
    合并后按面积重新排序，色板才和「主色/第二层/支持层」的说法对得上。
    """
    keep: list[dict] = []
    for c in clusters:
        for k in keep:
            if dist(tuple(c["rgb"]), tuple(k["rgb"])) < tol:
                k["share"] += c["share"]
                k["merged"] = k.get("merged", 1) + 1
                break
        else:
            keep.append(dict(c))
    keep.sort(key=lambda c: -c["share"])
    return keep


def assign_roles(clusters: list[dict]) -> None:
    """按面积与明度分配 role，并把细碎簇标成 drop。"""
    for i, c in enumerate(clusters):
        _, s, v = rgb2hsv(c["rgb"])
        if c["share"] < NOISE_SHARE:
            c["role"] = "drop"
        elif i == 0:
            c["role"] = "ground" if v > 0.74 else "primary"
        elif i == 1:
            c["role"] = "secondary"
        elif c["share"] >= 0.12:
            c["role"] = "support"
        elif s > 0.25:
            c["role"] = "accent"
        else:
            c["role"] = "support"


def main() -> int:
    ap = argparse.ArgumentParser(description="从参考原图提取水墨平涂印刷色板")
    ap.add_argument("image", help="参考原图路径")
    ap.add_argument("--outdir", default="out/pal", help="输出目录")
    ap.add_argument("--k", type=int, default=6, help="色簇数量，5–8 之间最好（默认 6）")
    ap.add_argument("--top", type=float, default=0.55, help="只取画面上半部分的比例（默认 0.55）")
    ap.add_argument("--no-crop", action="store_true", help="取全图，不裁上半部分")
    ap.add_argument("--paper", default=PAPER["hex"], help="纸白（默认米白 #F4EFE4）")
    ap.add_argument("--no-swatch", action="store_true", help="不生成色卡 PNG")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    src = Path(args.image)
    if not src.exists():
        print(f"[error] 参考图不存在：{src}", file=sys.stderr)
        return 2

    img = Image.open(src).convert("RGB")
    W, H = img.size
    if not args.no_crop and 0 < args.top < 1:
        img = img.crop((0, 0, W, max(1, int(H * args.top))))
        crop_note = f"画面上半部分 0–{int(args.top * 100)}%"
    else:
        crop_note = "全图"

    # 分析前缩到 640 长边：k-means 不需要高分辨率，反而更抗噪点
    small = img.copy()
    small.thumbnail((640, 640), Image.LANCZOS)
    pts = sample_pixels(small, limit=6000)

    k = max(3, min(10, args.k))
    clusters = kmeans(pts, k)
    clusters = merge_similar(clusters)
    for c in clusters:
        c["hex"] = rgb2hex(c["rgb"])
        c["rgb"] = list(c["rgb"])
        c["flat"] = flatten(tuple(c["rgb"]))
        c["luma"] = round(luma(c["rgb"]), 3)
    assign_roles(clusters)

    keep = [c for c in clusters if c["role"] != "drop"]
    used_share = sum(c["share"] for c in keep)
    ink = pick_ink(keep or clusters)

    pal = {
        "source": str(src),
        "source_size": [W, H],
        "crop": crop_note,
        "paper": {"hex": args.paper, "rgb": list(hex2rgb(args.paper))},
        "clusters": clusters,
        "ink": ink,
        "keep_count": len(keep),
        "kept_share": round(used_share, 3),
        "note": (
            f"取自{crop_note}，k={k}。keep={len(keep)} 个色簇覆盖 {used_share * 100:.1f}% 的面积；"
            f"其余 {len(clusters) - len(keep)} 个标 drop —— 这些就是原图里的细碎纹理，"
            "平涂重构时不要它们。flat 字段是改写后能印在纸上的颜色，色相来自原图，"
            "饱和度与明度被重写。"
        ),
    }

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "palette.json").write_text(json.dumps(pal, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[write] {outdir / 'palette.json'}")

    if not args.no_swatch:
        p = outdir / "palette.png"
        draw_swatch(pal, p)
        print(f"[write] {p}")

    print(f"\n[crop] {crop_note}   [{len(clusters)} 簇，保留 {len(keep)}]")
    for i, c in enumerate(clusters):
        mark = "  " if c["role"] != "drop" else "× "
        print(f"{mark}{i + 1}. {c['hex']} → {c['flat']['hex']}  {c['share'] * 100:5.1f}%  {c['role']:<9} {c['flat']['note']}")
    print(f"   ink {ink['hex']}（取自 {ink['from']}）")

    if pal["kept_share"] < 0.80:
        print(
            f"\n[warn] 保留色簇只覆盖 {pal['kept_share'] * 100:.0f}% 的面积：原图比较碎。"
            "要么 --k 调小（5），要么 --top 再往下裁一点，只取最干净的那一段。",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
