#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自检：不依赖任何外部图片，现场合成测试图，验证提示词质量门与调色链是否按设计工作。

跑法：
    python scripts/selftest.py

全部通过返回 0，任一失败返回 1。
这个脚本存在的理由：调色链的正确性没法靠肉眼判断（人会适应任何色偏），
只能靠「给定输入 A，输出必须满足条件 B」这种断言。
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import grade  # noqa: E402
import build_ng_prompt as bnp  # noqa: E402

from PIL import Image, ImageDraw, ImageFilter  # noqa: E402

W, H = 640, 432
FAILS: list[str] = []
PASSED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  [ok]   {name}")
    else:
        FAILS.append(f"{name}  {detail}")
        print(f"  [FAIL] {name}  {detail}")


# ---------------------------------------------------------------- 合成测试图

def scene() -> Image.Image:
    """一个可复现的合成场景：天空 / 远山 / 草地 / 土路 / 人脸 / 暗部。"""
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(0, 200):
        t = y / 200
        d.line([(0, y), (W, y)], fill=(int(40 + 120 * t), int(90 + 110 * t), int(160 + 70 * t)))
    d.polygon([(0, 200), (150, 110), (290, 200)], fill=(120, 130, 145))
    d.polygon([(180, 200), (360, 92), (560, 200)], fill=(135, 145, 158))
    d.polygon([(330, 118), (360, 92), (392, 118), (372, 112), (360, 120), (344, 111)],
              fill=(238, 242, 248))
    for y in range(200, H):
        t = (y - 200) / (H - 200)
        d.line([(0, y), (W, y)], fill=(int(120 + 30 * t), int(140 - 10 * t), int(62 + 20 * t)))
    rnd = random.Random(7)
    for _ in range(1500):
        x, y = rnd.randrange(W), rnd.randrange(200, H)
        g = rnd.randint(-30, 30)
        d.point((x, y), fill=(max(0, min(255, 132 + g)), max(0, min(255, 148 + g)),
                              max(0, min(255, 70 + g // 2))))
    d.polygon([(270, H), (400, H), (356, 200), (330, 200)], fill=(152, 132, 108))
    d.ellipse([470, 235, 530, 305], fill=(196, 148, 120))
    d.ellipse([482, 258, 490, 266], fill=(60, 45, 40))
    d.ellipse([512, 258, 520, 266], fill=(60, 45, 40))
    d.polygon([(455, 305), (545, 305), (560, 420), (440, 420)], fill=(72, 54, 46))
    d.polygon([(0, 168), (130, 108), (230, 185), (0, 190)], fill=(52, 56, 62))
    return img.filter(ImageFilter.GaussianBlur(0.6))


def milky(img: Image.Image) -> Image.Image:
    """低对比 + 抬黑：网红滤镜 / 假 HDR 的色阶（跨度约 60 级）。"""
    return Image.eval(img, lambda c: max(0, min(255, int(c * 0.72 + 60))))


def crushed(img: Image.Image) -> Image.Image:
    """暗部压死 + 高光顶上：对比拉过头。"""
    def f(c: int) -> int:
        v = (c - 78) * 1.85
        return 0 if v < 8 else 255 if v > 250 else int(v)

    return Image.eval(img, f)


def well_exposed(img: Image.Image) -> Image.Image:
    """色阶铺满、曝光正确：用来验证「本来就好好的图不会被拆坏」。"""
    return Image.eval(img, lambda c: max(0, min(255, int(12 + (c - 40) * 234 / 200))))


# ---------------------------------------------------------------- 1. 调色链

def test_pipeline() -> None:
    print("\n[1] 调色链")
    base = scene()
    preset = grade.scale_preset(grade.PRESETS["travel"], 1.0)

    # 1a 雾化图必须被救回来
    bad = milky(base)
    m0 = grade.analyze(bad)
    fixed, info = grade.apply_pipeline(bad, preset)
    m1 = grade.analyze(fixed)
    check("雾化图：色阶被拉开", info["levels"]["applied"] and info["levels"]["gain"] > 1.0,
          f"levels={info['levels']}")
    check("雾化图：中段对比显著提升",
          m1["tone"]["contrast_p90_p10"] - m0["tone"]["contrast_p90_p10"] >= 40,
          f"{m0['tone']['contrast_p90_p10']} → {m1['tone']['contrast_p90_p10']}")
    check("雾化图：暗部从提亮状态回到暗部",
          m1["tone"]["shadow_luma"] < m0["tone"]["shadow_luma"] - 15,
          f"{m0['tone']['shadow_luma']} → {m1['tone']['shadow_luma']}")
    check("雾化图：饱和加成被拉伸倍数收回",
          info.get("sat_compensation", 1.0) < 0.95, f"k={info.get('sat_compensation')}")
    rows, hard = grade.check(m1)
    check("雾化图：调完不压死不曝死", hard == 0,
          str([r["metric"] for r in rows if r["level"] == "fail"]))

    # 1b 压死的图必须被判死
    dead = crushed(base)
    m2 = grade.analyze(dead)
    _, hard2 = grade.check(m2)
    check("压死图：体检判 FAIL 并返回 1", hard2 >= 1, f"hard={hard2}")

    # 1c 已经铺满色阶的图不能被重复拉伸
    ramp_l = Image.linear_gradient("L").resize((640, 432))
    ramp = Image.merge("RGB", (ramp_l, ramp_l, ramp_l.point(lambda c: int(c * 0.96))))
    lv_ramp = grade.auto_levels(ramp)[1]
    check("满色阶图：不再拉伸", not lv_ramp["applied"], f"span={lv_ramp.get('span')}")

    # 1d 曝光正确的图不能被拆坏
    good = well_exposed(base)
    m3 = grade.analyze(good)
    fixed2, _ = grade.apply_pipeline(good, preset)
    m4 = grade.analyze(fixed2)
    check("正常图：两端仍有余量",
          3 <= m4["tone"]["p5"] <= 32 and m4["tone"]["p99"] <= 254,
          f"p5={m4['tone']['p5']} p99={m4['tone']['p99']}")
    check("正常图：没有新增裁切",
          m4["tone"]["crushed_ratio"] <= 0.02 and m4["tone"]["clipped_ratio"] <= 0.01,
          f"crush={m4['tone']['crushed_ratio']} clip={m4['tone']['clipped_ratio']}")
    check("正常图：色相锚点没被调偏",
          abs((m4["color"]["hue_sky"] or 0) - (m3["color"]["hue_sky"] or 0)) < 8,
          f"{m3['color']['hue_sky']} → {m4['color']['hue_sky']}")

    # 1d 肤色保护：土色区推饱和时，肤色像素的饱和增量必须小于土色
    skin = Image.new("RGB", (240, 120), (150, 110, 82))     # 皮肤色 S≈0.45
    soil = Image.new("RGB", (240, 120), (150, 108, 62))     # 土色 S≈0.59
    tile = Image.new("RGB", (480, 120))
    tile.paste(skin, (0, 0))
    tile.paste(soil, (240, 0))
    graded, _ = grade.apply_pipeline(tile, preset, levels="off")
    s_ch = graded.convert("HSV").split()[1]
    px = s_ch.load()
    s_skin = px[120, 60]
    s_soil = px[360, 60]
    check("肤色被排除在分色之外，土色照常提饱和",
          (s_soil / 255.0) > 0.60 and (s_skin / 255.0) < 0.50,
          f"肤色 {s_skin / 255:.2f}（原 0.45）  土色 {s_soil / 255:.2f}（原 0.59）")

    # 1e 放大到 8K
    big = grade.upscale(base, 7680)
    check("8K 重采样长边正确", max(big.size) == 7680, str(big.size))


# ---------------------------------------------------------------- 2. 质量门

def spec_base() -> dict:
    return {
        "mode": "travel",
        "style_anchor": {"raw": "《国家地理》经典纪实摄影调色风格"},
        "region": "四川甘孜 · 塔公草原东缘",
        "scene": "清晨的牧道，土路斜插进草原，中景有牦牛群，远景是雪山",
        "subject": "赶牛的牧民",
        "people": {"count": 1, "desc": "中年牧民，旧藏袍"},
        "camera": {"lens": "35mm", "layers": ["前景草", "中景人", "远景山"]},
        "light": {"time": "日出后 40 分钟"},
        "palette": {"elements": ["天空", "植被", "大地"]},
        "aspect": "3:2",
        "size": "2048x1365",
    }


def expect_error(name: str, spec: dict, code: str) -> None:
    try:
        bnp.build(spec)
    except ValueError as exc:
        check(name, str(exc).startswith(code), f"期望 {code}，实际 {str(exc)[:40]}")
        return
    check(name, False, f"期望抛出 {code}")


def test_gates() -> None:
    print("\n[2] 提示词质量门")

    spec = spec_base()
    prompt, issues = bnp.build(spec)
    errs = [m for lv, m in issues if lv == "error"]
    check("正常 spec 无硬错误", not errs, str(errs))
    check("品牌名被翻译，不留在提示词里", "国家地理" not in prompt,
          "提示词里仍出现品牌名")
    check("自动写入禁止边框与字标",
          "黄色矩形边框" in prompt and "杂志排版" in prompt)
    check("有主要人物时自动补肤色条款", "肤色必须与大地色分离" in prompt)

    s = spec_base(); s["scene"] = ""
    expect_error("空场景被拦（E301）", s, "E301")

    s = spec_base(); s["region"] = "某地"
    expect_error("占位符被拦（E302）", s, "E302")

    s = spec_base(); s["aspect"] = "3:2"; s["size"] = "2048x2048"
    expect_error("画幅与尺寸不一致（E304）", s, "E304")

    s = spec_base(); s["size"] = "8192x5461"
    expect_error("尺寸超 4096（E305）", s, "E305")

    s = spec_base(); s["detail"] = "加上 HDR 效果更有冲击力"
    try:
        _, iss = bnp.build(s)
        lv = [l for l, _ in iss if l == "error"]
        check("高危词 HDR 被拦（E306）", bool(lv), "未报错")
    except ValueError:
        check("高危词 HDR 被拦（E306）", False, "抛了异常而不是返回 error")

    s = spec_base(); s["light"] = {"time": "正午 12:00，顶光"}
    _, iss = bnp.build(s)
    check("正午硬光被提示（W305）", any(m.startswith("W305") for _, m in iss))
    check("提示词正文不含 ASCII 双引号（中文用「」）", '"' not in prompt)


# ---------------------------------------------------------------- 3. 命令行

def test_cli() -> None:
    print("\n[3] 命令行")
    import json
    import subprocess
    import tempfile

    py = sys.executable
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "in.png"
        scene().save(src)

        r = subprocess.run([py, "-X", "utf8", str(ROOT / "scripts" / "grade.py"),
                            str(src), "--check"],
                           capture_output=True, text=True, encoding="utf-8")
        check("--check 能独立运行", r.returncode in (0, 1), f"rc={r.returncode} {r.stderr[:80]}")
        check("--check 输出六轴指标", "contrast_p90_p10" in r.stdout and "shadow_open" in r.stdout)

        out = tmp / "out.jpg"
        rep = tmp / "r.json"
        r = subprocess.run([py, "-X", "utf8", str(ROOT / "scripts" / "grade.py"),
                            str(src), "--out", str(out), "--preset", "nature",
                            "--report", str(rep)],
                           capture_output=True, text=True, encoding="utf-8")
        check("调色出图成功", r.returncode == 0 and out.exists(), r.stderr[:120])
        if out.exists():
            check("出图尺寸与原图一致", Image.open(out).size == (W, H), str(Image.open(out).size))
        if rep.exists():
            data = json.loads(rep.read_text(encoding="utf-8"))
            check("报告含 before/after 两组读数",
                  "before" in data and "after" in data and "levels" in data)

        r = subprocess.run([py, "-X", "utf8", str(ROOT / "scripts" / "grade.py"),
                            str(tmp / "nope.png"), "--check"],
                           capture_output=True, text=True, encoding="utf-8")
        check("缺文件时给出明确报错", r.returncode != 0 and "不存在" in (r.stderr or ""))


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    print("hy-image35-nationalgeo 自检")
    test_pipeline()
    test_gates()
    test_cli()

    total = PASSED + len(FAILS)
    print(f"\n{'=' * 52}\n通过 {PASSED}/{total}")
    if FAILS:
        print("失败项：")
        for f in FAILS:
            print("  -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
