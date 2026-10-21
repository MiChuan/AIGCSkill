#!/usr/bin/env python3
"""还原校验：按反推出来的提示词重新出一张图，跟原图比，给分。

「这段提示词能不能还原这张图」是反推的唯一硬判据。
不比不知道：你觉得自己写全了，模型出图后光位错了、色温反了、景深没开——
这脚本把每一项偏差量化出来，告诉你回去改 spec 的哪一段。

用法：
    python scripts/verify.py --ref assets/demo-ref.png --gen out/dune/raw/1.png --outdir out/verify
    python scripts/verify.py --ref a.png --gen b.png --outdir out/v --json-only

评分：100 起扣，>=80 通过，60–79 勉强（看扣分项决定要不要重出），<60 不通过。
依赖：Pillow。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image  # noqa: E402

import imgkit as K  # noqa: E402
import probe as P  # noqa: E402


def luma_hist(luma, bins=32):
    h = [0] * bins
    for v in luma:
        h[min(bins - 1, int(v * bins))] += 1
    t = float(sum(h)) or 1.0
    return [c / t for c in h]


def rgb_dist(a, b):
    return sum(abs(x - y) for x, y in zip(a, b))


def palette_dist(p1, p2):
    """加权贪心匹配：原图主色在生成图里找最近色，按占比加权。"""
    if not p1 or not p2:
        return 765.0
    total = 0.0
    wsum = 0.0
    for a in p1:
        dmin = min(rgb_dist(a["rgb"], b["rgb"]) for b in p2)
        w = max(0.05, a.get("share", 0.1))
        total += dmin * w
        wsum += w
    return total / wsum if wsum else 765.0


def compare(ref_path, gen_path, k=5):
    d_ref, rw = P.analyze(ref_path, k=k)
    d_gen, gw = P.analyze(gen_path, k=k)

    r, g, b = K.channels(rw)
    l_ref = K.luma_of(r, g, b)
    r, g, b = K.channels(gw)
    l_gen = K.luma_of(r, g, b)
    h_ref = luma_hist(l_ref)
    h_gen = luma_hist(l_gen)
    hist_l1 = sum(abs(a - c) for a, c in zip(h_ref, h_gen))

    aspect_gap = abs(d_ref["aspect_float"] - d_gen["aspect_float"]) / max(1e-6, d_ref["aspect_float"])
    pal_d = palette_dist(d_ref["palette"][:k], d_gen["palette"][:k])
    luma_gap = abs(d_ref["brightness"]["mean"] - d_gen["brightness"]["mean"])
    sat_gap = abs(d_ref["saturation"]["mean"] - d_gen["saturation"]["mean"])
    temp_gap = abs(d_ref["temperature"]["warmth"] - d_gen["temperature"]["warmth"])
    sharp_gap = abs(d_ref["sharpness"]["score"] - d_gen["sharpness"]["score"])
    dof_a = d_ref["dof"]["center_edge_ratio"]
    dof_b = d_gen["dof"]["center_edge_ratio"]
    dof_flip = (dof_a > 1.3) != (dof_b > 1.3)
    ctr_gap = abs(d_ref["brightness"]["contrast"] - d_gen["brightness"]["contrast"])

    checks = [
        {
            "item": "画幅", "ref": d_ref["aspect"], "gen": d_gen["aspect"],
            "value": round(aspect_gap, 3), "loss": 20 if aspect_gap > 0.02 else 0,
            "fix": "提示词画幅段与 --ar / size 参数不一致，或模型没认；改 composition.aspect",
        },
        {
            "item": "整体亮度", "ref": round(d_ref["brightness"]["mean"], 3),
            "gen": round(d_gen["brightness"]["mean"], 3),
            "value": round(luma_gap, 3),
            "loss": 10 if luma_gap > 0.08 else (5 if luma_gap > 0.04 else 0),
            "fix": "亮度差 %.2f：改 light.contrast / light.time，或在负面加「过曝 / 欠曝」" % luma_gap,
        },
        {
            "item": "明暗分布（直方图 L1）", "ref": "—", "gen": "—",
            "value": round(hist_l1, 3),
            "loss": 15 if hist_l1 > 0.30 else (7 if hist_l1 > 0.18 else 0),
            "fix": "明暗分布差 %.2f：多半是光位或反差不对，核对 light.direction / quality" % hist_l1,
        },
        {
            "item": "色板距离（加权 RGB 差）", "ref": " / ".join(p["hex"] for p in d_ref["palette"][:4]),
            "gen": " / ".join(p["hex"] for p in d_gen["palette"][:4]),
            "value": round(pal_d, 1),
            "loss": 15 if pal_d > 40 else (7 if pal_d > 22 else 0),
            "fix": "色板偏差 %.0f：把 spec.color_palette 的 hex 直接写进提示词，别只写颜色名" % pal_d,
        },
        {
            "item": "饱和度", "ref": d_ref["saturation"]["mean"], "gen": d_gen["saturation"]["mean"],
            "value": round(sat_gap, 3),
            "loss": 10 if sat_gap > 0.15 else (5 if sat_gap > 0.08 else 0),
            "fix": "饱和度差 %.2f：在 style 里加「低饱和 / 胶片淡彩」或「高饱和」" % sat_gap,
        },
        {
            "item": "色温（R-B）", "ref": d_ref["temperature"]["warmth"],
            "gen": d_gen["temperature"]["warmth"],
            "value": round(temp_gap, 3),
            "loss": 10 if temp_gap > 0.08 else (5 if temp_gap > 0.04 else 0),
            "fix": "色温差 %.3f：写清「暖调金光 / 冷调蓝阴影」，不要写「自然色温」" % temp_gap,
        },
        {
            "item": "景深方向", "ref": "%.2f" % dof_a, "gen": "%.2f" % dof_b,
            "value": "翻转" if dof_flip else "一致",
            "loss": 15 if dof_flip else 0,
            "fix": "景深翻转：原图 %s，生成 %s；camera.dof 一段写反了或没写" % (
                d_ref["dof"]["label"], d_gen["dof"]["label"]),
        },
        {
            "item": "锐度", "ref": d_ref["sharpness"]["score"], "gen": d_gen["sharpness"]["score"],
            "value": sharp_gap,
            "loss": 5 if sharp_gap > 25 else 0,
            "fix": "锐度差 %d：生成图偏软加「清晰细节、边缘利落」，偏硬加「柔光、无锐化」" % sharp_gap,
        },
        {
            "item": "反差", "ref": d_ref["brightness"]["contrast"],
            "gen": d_gen["brightness"]["contrast"],
            "value": round(ctr_gap, 3),
            "loss": 5 if ctr_gap > 0.25 else 0,
            "fix": "反差差 %.2f：核对 light.contrast（低反差 / 中间调饱满 / 硬调）" % ctr_gap,
        },
    ]

    score = 100 - sum(c["loss"] for c in checks)
    verdict = "通过" if score >= 80 else ("勉强：看扣分项决定是否重出" if score >= 60 else "不通过：建议重出")
    fails = [c for c in checks if c["loss"] > 0]

    return {
        "ref": str(ref_path),
        "gen": str(gen_path),
        "score": score,
        "verdict": verdict,
        "checks": checks,
        "top_fix": fails[0]["fix"] if fails else "全部通过",
        "ref_summary": {
            "aspect": d_ref["aspect"], "luma": d_ref["brightness"]["mean"],
            "light": d_ref["light"]["direction"], "temp": d_ref["temperature"]["label"],
            "dof": d_ref["dof"]["label"], "sharp": d_ref["sharpness"]["score"],
        },
        "gen_summary": {
            "aspect": d_gen["aspect"], "luma": d_gen["brightness"]["mean"],
            "light": d_gen["light"]["direction"], "temp": d_gen["temperature"]["label"],
            "dof": d_gen["dof"]["label"], "sharp": d_gen["sharpness"]["score"],
        },
    }, d_ref, d_gen


def write_md(v, outdir):
    lines = ["# 还原校验：%s" % Path(v["gen"]).name, ""]
    lines.append("**总分 %d / 100 —— %s**" % (v["score"], v["verdict"]))
    lines.append("")
    lines.append("| 项 | 原图 | 生成图 | 差值 | 扣分 | 怎么改 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for c in v["checks"]:
        lines.append("| %s | %s | %s | %s | %s | %s |" %
                     (c["item"], c["ref"], c["gen"], c["value"], c["loss"],
                      c["fix"] if c["loss"] else "—"))
    lines.append("")
    if v["score"] < 100:
        lines.append("> 改一处重出一处：一次只动一个字段，才能知道是哪项生效。")
    Path(outdir / "verify.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def side_by_side(ref_path, gen_path, d_ref, d_gen, out_path, target_h=360):
    imgs = []
    for p in (ref_path, gen_path):
        im = Image.open(p).convert("RGB")
        f = target_h / float(im.height)
        imgs.append(im.resize((max(1, int(im.width * f)), target_h), Image.Resampling.LANCZOS))
    gap = 8
    w = imgs[0].width + imgs[1].width + gap
    h = target_h + 28 + 40
    canvas = Image.new("RGB", (w, h), (250, 250, 250))
    canvas.paste(imgs[0], (0, 28))
    canvas.paste(imgs[1], (imgs[0].width + gap, 28))
    from PIL import ImageDraw
    dr = ImageDraw.Draw(canvas)
    dr.text((6, 6), "REF  %s | %s | %s" % (d_ref["aspect"], d_ref["light"]["direction"],
                                             d_ref["temperature"]["label"]), fill=(20, 20, 20))
    dr.text((imgs[0].width + gap + 6, 6), "GEN  %s | %s | %s" % (d_gen["aspect"], d_gen["light"]["direction"],
                                                                  d_gen["temperature"]["label"]), fill=(20, 20, 20))
    bw = w / 8.0
    for i, p in enumerate(d_ref["palette"][:4]):
        dr.rectangle([i * bw, target_h + 28, (i + 1) * bw, target_h + 48], fill=tuple(p["rgb"]))
    for i, p in enumerate(d_gen["palette"][:4]):
        dr.rectangle([(i + 4) * bw, target_h + 28, (i + 5) * bw, target_h + 48], fill=tuple(p["rgb"]))
    dr.line([(4 * bw, target_h + 28), (4 * bw, target_h + 48)], fill=(20, 20, 20))
    canvas.save(out_path)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="对比原图与按提示词重出的图，给还原分并指出改哪一段。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python scripts/verify.py --ref assets/demo-ref.png --gen out/dune/raw/1.png --outdir out/verify\n"
               "  python scripts/verify.py --ref a.png --gen b.png --outdir out/v --json-only\n")
    ap.add_argument("--ref", required=True, help="原图路径")
    ap.add_argument("--gen", required=True, help="重出图路径")
    ap.add_argument("--outdir", default="out/verify", help="输出目录")
    ap.add_argument("--json-only", action="store_true", help="只写 JSON")
    ap.add_argument("--fail-under", type=int, default=80, help="低于该分数退出码为 1（默认 80）")
    args = ap.parse_args(argv)

    v, d_ref, d_gen = compare(args.ref, args.gen)
    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    K.dump_json(v, out / "verify.json")

    if not args.json_only:
        write_md(v, out)
        side_by_side(args.ref, args.gen, d_ref, d_gen, out / "verify.png")

    print("[verify] score %d —— %s" % (v["score"], v["verdict"]))
    for c in v["checks"]:
        if c["loss"]:
            print("  -%2d %s（%s）" % (c["loss"], c["item"], c["fix"]))
    if v["score"] == 100:
        print("  全过")
    print("[ok] 输出 %s" % out.resolve())
    return 0 if v["score"] >= args.fail_under else 1


if __name__ == "__main__":
    sys.exit(main())
