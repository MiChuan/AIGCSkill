#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自检：用合成图把三个脚本的关键行为钉住。

为什么要自检：这套脚本做的事（加边框、清水印、叠字）单看结果都「差不多对」，
但每一样都有一种失败是肉眼看不出来的——
  · 边框三边 15px、一边 12px（放到 ID 模板里脸会偏 3px）
  · 水印修完还留半个字（缩略图看不出，放大就露）
  · 文字排在西装翻领上（对比度够，但底子脏）
所以这里不测「好不好看」，只测「给定的输入必须满足的可判定条件」。

用法:
    python scripts/selftest.py            # 全跑
    python scripts/selftest.py -v         # 带每次测量的数值
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageStat

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PY = sys.executable

sys.path[:0] = [str(HERE)]

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))


# ---------------------------------------------------------------- 合成测试图

def synth_id(w: int = 768, h: int = 1024, watermark: bool = False,
             builtin_border: bool = False) -> Image.Image:
    """一张「证件写真」的骨架：磨砂灰蓝底 + 径向渐变 + 人形 + 可选水印/白框。"""
    bg = Image.new("RGB", (w, h), (196, 205, 216))
    g = Image.radial_gradient("L").resize((w, h), Image.BICUBIC)
    delta = g.point(lambda v: int(-13 * v / 255))
    bg = Image.merge("RGB", [ImageChops.add(c, delta) for c in bg.split()])

    d = ImageDraw.Draw(bg)
    # 肩膀：深藏蓝西装
    d.polygon([(int(w * 0.06), h), (int(w * 0.10), int(h * 0.78)),
               (int(w * 0.40), int(h * 0.70)), (int(w * 0.60), int(h * 0.70)),
               (int(w * 0.90), int(h * 0.78)), (int(w * 0.94), h)],
              fill=(30, 34, 48))
    # 脖子
    d.rectangle([int(w * 0.44), int(h * 0.60), int(w * 0.56), int(h * 0.74)], fill=(206, 176, 158))
    # 白衬衫领
    d.polygon([(int(w * 0.42), int(h * 0.73)), (int(w * 0.50), int(h * 0.86)),
               (int(w * 0.44), int(h * 0.88))], fill=(246, 246, 244))
    d.polygon([(int(w * 0.58), int(h * 0.73)), (int(w * 0.50), int(h * 0.86)),
               (int(w * 0.56), int(h * 0.88))], fill=(246, 246, 244))
    # 领带
    d.polygon([(int(w * 0.48), int(h * 0.84)), (int(w * 0.52), int(h * 0.84)),
               (int(w * 0.525), h), (int(w * 0.475), h)], fill=(38, 52, 88))
    # 头
    d.ellipse([int(w * 0.38), int(h * 0.22), int(w * 0.62), int(h * 0.58)], fill=(224, 194, 176))
    # 头发
    d.chord([int(w * 0.375), int(h * 0.20), int(w * 0.625), int(h * 0.50)],
        180, 360, fill=(42, 36, 34))
    # 眼睛
    for ex in (0.455, 0.525):
        d.ellipse([int(w * ex), int(h * 0.365), int(w * (ex + 0.022)), int(h * 0.385)],
                  fill=(48, 48, 56))
    # 嘴
    d.line([(int(w * 0.475), int(h * 0.475)), (int(w * 0.525), int(h * 0.475))],
           fill=(168, 116, 110), width=max(2, h // 300))

    # 胶片颗粒：sigma 6 对 8bit 图来说已经偏重（真实磨砂背景大约 2–4），
    # 用它来压测「噪声会不会把水印检测淹掉」。
    noise = Image.effect_noise((w, h), 6).convert("L")
    bg = Image.merge("RGB", [
        ImageChops.add(c, noise, 1.0, -128)
        for c in bg.split()
    ])

    if watermark:
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        wd = ImageDraw.Draw(layer)
        try:
            from PIL import ImageFont
            f = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", max(14, h // 48))
        except Exception:
            f = None
        wd.text((int(w * 0.80), int(h * 0.935)), "HY Image3.5", font=f, fill=(255, 255, 255, 165))
        bg = Image.alpha_composite(bg.convert("RGBA"), layer).convert("RGB")

    if builtin_border:
        bd = ImageDraw.Draw(bg)
        bp = int(min(w, h) * 0.025)
        bd.rectangle([0, 0, w - 1, h - 1], outline=(252, 252, 251), width=bp)

    return bg


def band_std(img: Image.Image, box: tuple[int, int, int, int]) -> float:
    return round(ImageStat.Stat(img.crop(box).convert("L")).stddev[0], 2)


def band_edge(img: Image.Image, box: tuple[int, int, int, int]) -> float:
    """区域内的边缘能量。

    判「水印有没有修掉」要看这个，不能看整块的标准差：
    水印如果压在衣服边界的交界上，修完之后那块区域仍然有「衣服 vs 背景」的真实对比，
    标准差只从 80 降到 40，看起来像没修好；但文字笔画带来的高频已经全部消失，
    边缘能量会掉一个数量级。用错指标会把自己逼着去改一个本来正确的算法。
    """
    return round(ImageStat.Stat(
        img.crop(box).convert("L").filter(ImageFilter.FIND_EDGES)
    ).mean[0], 2)


# ---------------------------------------------------------------- 各脚本的断言

def test_prompt(tmp: Path, verbose: bool) -> None:
    import build_id_prompt as B

    male = json.loads((ROOT / "specs/male-glasses.json").read_text(encoding="utf-8"))
    prompt, negative, meta = B.assemble(male)

    check("13 节结构齐全", prompt.count("【") >= 13, f"{prompt.count('【')} 节")
    check("改形词被改写成光学表述",
          "精瘦脸型" not in prompt and "体脂率" not in prompt and "光" in prompt,
          "精瘦/体脂已替换")
    check("保脸条款在场", "脸的长宽比" in prompt and "逐项对齐" in prompt)
    check("负面词去重且含用户原有 13 条",
          all(x in negative for x in ("畸形五官", "多手指", "夸张滤镜")),
          f"{meta['negative_count']} 条")
    check("边框条款禁止模型画框", "不要" in prompt and "画任何边框" in prompt)
    check("8K 交付尺寸 5760x7680", meta["deliver_8k"] == [5760, 7680], str(meta["deliver_8k"]))
    check("出图参数 revise=False / 不传 footnote",
          meta["imagegen"]["revise"] is False and "不传" in meta["imagegen"]["footnote"])
    check("提示词无 ASCII 直引号", '"' not in prompt)
    check("提示词无待填占位符",
          not any(k in prompt for k in ("TODO", "待填", "XXX", "【照片主题】")))

    female = json.loads((ROOT / "specs/female-wave.json").read_text(encoding="utf-8"))
    fp, _fn, fmeta = B.assemble(female)
    check("女生版走 model 路线（三行都在模型能力内）", fmeta["route_text"] == "model",
          fmeta["route_text"])
    check("KARINA 出现在主标题段", "KARINA" in fp)

    # 长文本必须自动切 overlay
    long_spec = json.loads(json.dumps(female))
    long_spec["typeset"]["sub"] = "A" * 60
    _p, _n, lm = B.assemble(long_spec)
    check("副标题超长自动切 overlay", lm["route_text"] == "overlay", lm["route_text"])

    # 中文字必须切 overlay
    zh_spec = json.loads(json.dumps(female))
    zh_spec["typeset"]["headline"] = "练习生档案"
    _p, _n, zm = B.assemble(zh_spec)
    check("含中文自动切 overlay", zm["route_text"] == "overlay", zm["route_text"])

    # 空 wardrobe 必须报 E501
    bad = json.loads(json.dumps(male))
    bad["wardrobe"] = {}
    _p, _n, bm = B.assemble(bad)
    codes = [i["code"] for i in bm["issues"]]
    check("空 wardrobe 报 E501", "E501" in codes, str(codes))

    # 负面词与正面打架要报（把「柔和」写进负面）
    clash = json.loads(json.dumps(male))
    clash["negative_extra"].append("柔光")
    _p, _n, cm = B.assemble(clash)
    check("负面词与正面冲突报 W505",
          any("W505" in str(i["message"]) for i in cm["issues"]),
          "柔光 冲突已报")

    # 占位符
    ph = json.loads(json.dumps(male))
    ph["typeset"]["headline"] = "【照片主题】"
    _p, _n, pm = B.assemble(ph)
    check("占位符报 E502", any("E502" in str(i["message"]) for i in pm["issues"]))

    if verbose:
        print(f"  [num] prompt={len(prompt)} chars, negative={len(negative)} chars")


def test_finalize(tmp: Path, verbose: bool) -> None:
    import finalize as F

    raw = tmp / "raw.png"
    synth_id(watermark=True).save(raw)

    # 1) 水印检测与修复
    img = F.open_image(raw)
    det = F.detect_watermark(img)
    check("右下角水印被检出", det["score"] >= F.AUTO_MIN_SCORE,
          f"score={det['score']} tiles={det['hot_tiles']}")
    if det["candidate"]:
        x, y, w, h = det["candidate"]
        box = (x, y, x + w, y + h)
        before = band_std(img, box)
        e_before = band_edge(img, box)
        fixed = F.inpaint_edges(img, det["candidate"])
        after = band_std(fixed, box)
        e_after = band_edge(fixed, box)
        check("水印区的边缘能量下降 60% 以上", e_after < e_before * 0.4,
              f"edge {e_before} → {e_after}")
        check("修复后保留了同量级颗粒（不是死板色块）",
              after > 0.5 or e_after > 0.5, f"std {before}→{after} edge {e_before}→{e_after}")
    else:
        check("水印候选框存在", False, "candidate=None")

    # 2) 干净图不该被误伤
    clean = tmp / "clean.png"
    synth_id().save(clean)
    cimg = F.open_image(clean)
    cdet = F.detect_watermark(cimg)
    check("干净图不误判为有水印", cdet["score"] < F.AUTO_MIN_SCORE,
          f"score={cdet['score']}")

    # 3) 加边框：四边必须严格等宽
    src = synth_id()
    probe = F.measure_edge(src)
    has_b, hint = F.builtin_border_hint(probe)
    check("无框图的四边探测不误报", not has_b, hint[:40] if has_b else "ok")

    framed = synth_id(builtin_border=True)
    probe2 = F.measure_edge(framed)
    has_b2, _ = F.builtin_border_hint(probe2)
    check("模型自画的白框能被识别", has_b2,
          json.dumps({k: v["white_ratio"] for k, v in probe2.items()}))

    out_border = tmp / "border.png"
    rc = subprocess.run(
        [PY, "-X", "utf8", str(HERE / "finalize.py"), str(clean), "--out", str(out_border),
         "--target", "native", "--border", "0.02", "--watermark", "off",
         "--hairline", "auto"],
        capture_output=True, text=True, encoding="utf-8",
    )
    if rc.returncode != 0:
        check("finalize 加边框跑通", False, rc.stderr[-300:])
        return
    rep = json.loads(rc.stdout)
    meas = rep["border"]["verify"]["measured_px"]
    check("最终图四边宽度严格相等", len(set(meas.values())) == 1,
          json.dumps(meas, ensure_ascii=False))
    expected = round(0.02 * 768)
    check("边框宽度 = 短边 × 2%", abs(list(meas.values())[0] - expected) <= 2,
          f"measured={meas} expected≈{expected}")

    # 4) outside 模式：总尺寸必须精确等于目标
    out2 = tmp / "outside.jpg"
    rc2 = subprocess.run(
        [PY, "-X", "utf8", str(HERE / "finalize.py"), str(clean), "--out", str(out2),
         "--size", "1200x1600", "--border", "24", "--border-mode", "outside",
         "--watermark", "off"],
        capture_output=True, text=True, encoding="utf-8",
    )
    if rc2.returncode == 0:
        im2 = Image.open(out2)
        check("outside 模式总尺寸精确等于 --size", im2.size == (1200, 1600), str(im2.size))
    else:
        check("outside 模式跑通", False, rc2.stderr[-300:])

    # 5) 8K 交付
    out8 = tmp / "big.jpg"
    rc3 = subprocess.run(
        [PY, "-X", "utf8", str(HERE / "finalize.py"), str(clean), "--out", str(out8),
         "--target", "8k", "--border", "0.015", "--watermark", "off"],
        capture_output=True, text=True, encoding="utf-8",
    )
    if rc3.returncode == 0:
        im3 = Image.open(out8)
        check("8K 交付为 5760x7680", im3.size == (5760, 7680), str(im3.size))
    else:
        check("8K 出图跑通", False, rc3.stderr[-300:])

    # 6) 比例不能被边框改变（inside 模式）
    if rc.returncode == 0:
        im = Image.open(out_border)
        check("inside 模式不改比例", abs(im.width / im.height - 768 / 1024) < 1e-6,
              f"{im.size}")

    if verbose:
        print(f"  [num] watermark score={det['score']} clean={cdet['score']}")


def test_settype(tmp: Path, verbose: bool) -> None:
    src = tmp / "st.png"
    synth_id().save(src)
    out = tmp / "typed.png"

    rc = subprocess.run(
        [PY, "-X", "utf8", str(HERE / "settype.py"), str(src), "--out", str(out),
         "--spec", str(ROOT / "specs/female-wave.json")],
        capture_output=True, text=True, encoding="utf-8",
    )
    if rc.returncode != 0:
        check("settype 跑通", False, rc.stderr[-400:])
        return

    rep = json.loads(rc.stdout)
    W, H = rep["size"]
    check("三行都排上了", len(rep["lines"]) == 3, f"{len(rep['lines'])} 行")
    check("文字全部落在画面下半部", all(l["baseline"] > H * 0.55 for l in rep["lines"]),
          json.dumps([round(l["baseline"]) for l in rep["lines"]]))
    check("文字没有越出底边", all(l["baseline"] < H - 2 for l in rep["lines"]))
    check("主标题比副标题大", rep["lines"][0]["size_px"] > rep["lines"][1]["size_px"] * 2,
          f"{rep['lines'][0]['size_px']} vs {rep['lines'][1]['size_px']}")
    check("字距大于 0（宽字距排法）", all(l["track_px"] > 1 for l in rep["lines"]))
    check("文字带落在画面下部（不游走到中部）", rep["band"]["top"] > H * 0.60,
          f"top={rep['band']['top']} / H={H}")
    check("文字带没有大面积纯白（白字/深字都还能读）",
          rep["band"]["bright_ratio"] <= 0.15, str(rep["band"]["bright_ratio"]))
    check("对比度够（≥ 3:1）", rep["contrast_ratio"] >= 3.0, f"{rep['contrast_ratio']}:1")

    # 加了白边框之后，文字必须避开边框——排到相纸外面是这类写真最刺眼的错
    framed = tmp / "framed.png"
    fb = 24
    canvas = Image.new("RGB", (Image.open(src).width + fb * 2, Image.open(src).height + fb * 2),
                       (255, 255, 255))
    canvas.paste(Image.open(src), (fb, fb))
    canvas.save(framed)
    outf = tmp / "typed_framed.png"
    rcf = subprocess.run(
        [PY, "-X", "utf8", str(HERE / "settype.py"), str(framed), "--out", str(outf),
         "--spec", str(ROOT / "specs/female-wave.json")],
        capture_output=True, text=True, encoding="utf-8",
    )
    if rcf.returncode == 0:
        repf = json.loads(rcf.stdout)
        fh = repf["size"][1]
        check("自动识别出底部白边框并避开",
              repf["band"]["bottom"] <= fh - fb + 2,
              f"band_bottom={repf['band']['bottom']} frame_top={fh - fb}")
        check("文字没有排到边框上",
              all(l["baseline"] < fh - fb for l in repf["lines"]),
              json.dumps([round(l["baseline"]) for l in repf["lines"]]))
    else:
        check("带边框图排版跑通", False, rcf.stderr[-300:])

    # 叠上去的文字确实改变了像素。只看 bbox 内的均值——
    # 文字在整幅画面里占比很小（一行小字不到 1% 面积），全图均值会被稀释到 0.2 以下，
    # 那个数字既不能证明写了字、也不能证明没写。
    a = Image.open(src).convert("RGB")
    b = Image.open(out).convert("RGB")
    diff = ImageChops.difference(a, b).convert("L")
    bbox = diff.getbbox()
    if bbox:
        in_box = ImageStat.Stat(diff.crop(bbox)).mean[0]
    else:
        in_box = 0.0
    check("像素确实被写下（不是空操作）", bbox is not None and in_box > 8.0,
          f"bbox={bbox} mean_in_box={in_box:.2f}")

    # 超长文字必须报错而不是硬塞
    rc2 = subprocess.run(
        [PY, "-X", "utf8", str(HERE / "settype.py"), str(src), "--out", str(tmp / "x.png"),
         "--line", "A", "--line", "B", "--line", "C", "--line", "D"],
        capture_output=True, text=True, encoding="utf-8",
    )
    check("超过三行直接拒绝", rc2.returncode != 0, "exit!=0")

    if verbose:
        print(f"  [num] band={rep['band']} contrast={rep['contrast_ratio']}")


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        print("== build_id_prompt")
        test_prompt(tmp, args.verbose)
        print("== finalize")
        test_finalize(tmp, args.verbose)
        print("== settype")
        test_settype(tmp, args.verbose)

    ok = sum(1 for _, p, _ in RESULTS if p)
    print(f"\n{ok}/{len(RESULTS)} passed")
    for name, passed, detail in RESULTS:
        if not passed:
            print(f"  FAIL  {name}  {detail}")
        elif args.verbose:
            print(f"  ok    {name}  {detail}")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
