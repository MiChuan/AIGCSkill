#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自检：现场合成测试图，验证版面体检与提示词质量门是否符合设计。

不需要任何外部图片、不需要出图、不联网。跑完会告诉你每一条判据是否按设计工作。

    python selftest.py

合成 1 张合格图 + 7 张「每种错误各犯一次」的图，逐条断言 printcheck 能认出来；
再对 6 个 spec 断言 build_print_prompt 的质量门能拦住对应的错误。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    from PIL import Image, ImageDraw, ImageFilter
except ImportError:  # pragma: no cover
    sys.stderr.write("[deps] 缺少 Pillow：python -m pip install pillow\n")
    raise SystemExit(2)

import printcheck  # noqa: E402
import build_print_prompt as bpp  # noqa: E402

PAPER = (244, 239, 228)
INK = (46, 44, 39)
BLUES = [(214, 218, 210), (150, 164, 155), (92, 108, 100), (58, 70, 66)]
W, H = 819, 1024   # 4:5，分析宽 760 时够用

fails: list[str] = []
checks = 0


def ok(cond: bool, label: str, detail: str = "") -> None:
    global checks
    checks += 1
    if not cond:
        fails.append(f"{label}{(' — ' + detail) if detail else ''}")


def base(paper=PAPER) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (W, H), paper)
    return img, ImageDraw.Draw(img)


def draw_art(d: ImageDraw.ImageDraw, top=0.09, bottom=0.63, left=0.12, right=0.88) -> None:
    """三个叠压的几何面 + 一条山脊线，模拟重构后的插画块。"""
    t, b = int(H * top), int(H * bottom)
    l, r = int(W * left), int(W * right)
    mid = int(b - (b - t) * 0.52)
    # 底层（大面积浅色）
    d.polygon([(l, mid), (r, mid - 10), (r, b), (l, b)], fill=BLUES[0])
    # 中层（山脊折线）
    d.polygon([(l, mid), (l + int(W * 0.16), int(H * 0.20)), (l + int(W * 0.34), mid - 6),
               (r - int(W * 0.10), int(H * 0.17)), (r, mid)], fill=BLUES[1])
    # 上层（最重的一块）
    d.polygon([(l + int(W * 0.05), mid + 4), (r - int(W * 0.18), mid - 2),
               (r - int(W * 0.06), b - 6), (l, b - 6)], fill=BLUES[2])


def draw_words(d: ImageDraw.ImageDraw, cx: float, cy: float, width: float, size: int, gap: int = 4) -> None:
    """用连排的小方块模拟一行字（不用字体，避免环境差异影响判定）。"""
    total = int(W * width)
    x = int(W * cx) - total // 2
    y = int(H * cy) - size // 2
    word_w, space = 7, 4
    while x + word_w * size < int(W * cx) + total // 2:
        d.rectangle([x, y, x + size * word_w, y + size], fill=INK)
        x += size * word_w + gap + size * 3
    _ = space


def make_good() -> Image.Image:
    img, d = base()
    draw_art(d)
    draw_words(d, 0.5, 0.80, 0.30, 22)
    draw_words(d, 0.5, 0.875, 0.40, 9)
    return img


def tmp(name: str, img: Image.Image) -> Path:
    p = Path(_TMP) / name
    img.save(p)
    return p


def run(img: Image.Image, name: str, layout=None, palette=None):
    p = tmp(name, img)
    report, rc = printcheck.check(p, layout, palette)
    return report, rc


_TMP = tempfile.mkdtemp(prefix="inkprint-selftest-")
LAYOUT = {"title_y": 0.80, "desc_y": 0.875}

# ---------------------------------------------------------------- 正例

print("[1] 合格图：应该全部通过")
good = make_good()
good.save(Path(_TMP) / "good.png")
rep, rc = run(good, "good.png", LAYOUT)
print(f"    paper {rep['paper']['hex']} white {rep['whitespace'] * 100:.1f}% "
      f"cy {rep['centroid_y']} lines {rep['text_lines']} ratio {rep['size_ratio']} "
      f"gap {rep['gap_to_text']} noise {rep['paper_noise']} "
      f"edge_peak {rep['edge_peak']} score {rep['score']}")
for i in rep["issues"]:
    print(f"      {i}")
ok(rc == 0, "正例应通过", f"rc={rc}")
ok(rep["paper"]["ok"], "正例纸色应合格")
ok(rep["text_lines"] == 2, "正例应识别出 2 行文字", f"实际 {rep['text_lines']}")
ok(rep["whitespace"] >= 0.40, "正例留白应达标", f"{rep['whitespace']:.3f}")
ok(rep["size_ratio"] and 1.6 <= rep["size_ratio"] <= 4.8, "正例字高比应在区间", str(rep["size_ratio"]))

# ---------------------------------------------------------------- 负例：纸色

print("[2] 冷白底：应该判为纸色不合格")
img, d = base((250, 250, 252))
draw_art(d)
draw_words(d, 0.5, 0.80, 0.30, 22)
draw_words(d, 0.5, 0.875, 0.40, 9)
rep, rc = run(img, "cold.png", LAYOUT)
ok(rc == 1, "冷白底应返回 1", f"rc={rc}")
ok(not rep["paper"]["ok_warm"], "冷白底的 R-B 应判不合格", str(rep["paper"]["warm_r_minus_b"]))

print("[3] 灰底：亮度应该压到区间外")
img, d = base((214, 210, 202))
draw_art(d)
draw_words(d, 0.5, 0.80, 0.30, 22)
draw_words(d, 0.5, 0.875, 0.40, 9)
rep, rc = run(img, "gray.png", LAYOUT)
ok(rc == 1, "灰底应返回 1", f"rc={rc}")
ok(not rep["paper"]["ok_luma"], "灰底亮度应不合格", str(rep["paper"]["luma"]))

# ---------------------------------------------------------------- 负例：版面

print("[4] 满幅插画：留白应该不够")
img, d = base()
draw_art(d, top=0.03, bottom=0.97, left=0.02, right=0.98)
for i, y in enumerate((0.14, 0.34, 0.54, 0.74, 0.90)):
    d.rectangle([int(W * 0.03), int(H * (y - 0.06)), int(W * 0.97), int(H * (y + 0.06))], fill=BLUES[i % 4])
draw_words(d, 0.5, 0.80, 0.30, 22, )
rep, rc = run(img, "full.png", LAYOUT)
ok(rc == 1, "满幅图应返回 1", f"rc={rc}")
ok(rep["whitespace"] < 0.40, "满幅图留白应不足", f"{rep['whitespace']:.3f}")

print("[5] 插画压到文字：间距应该不足")
img, d = base()
draw_art(d, top=0.10, bottom=0.78)
draw_words(d, 0.5, 0.80, 0.30, 22)
draw_words(d, 0.5, 0.875, 0.40, 9)
rep, rc = run(img, "tight.png", LAYOUT)
ok(rc == 1, "间距不足应返回 1", f"rc={rc}")
ok(any("间距" in i for i in rep["issues"]), "应报出间距问题", str(rep["issues"][:3]))
ok(rep["gap_to_text"] is not None and rep["gap_to_text"] < 0.045, "间距数值应偏小", str(rep["gap_to_text"]))

# ---------------------------------------------------------------- 负例：文字

print("[6] 只有一行字：应判为结构缺失")
img, d = base()
draw_art(d)
draw_words(d, 0.5, 0.82, 0.34, 20)
rep, rc = run(img, "oneline.png", LAYOUT)
ok(rc == 1, "单行应返回 1", f"rc={rc}")
ok(rep["text_lines"] == 1, "应识别为 1 行", f"{rep['text_lines']}")
ok(any("1 行" in i for i in rep["issues"]), "应报出「只有 1 行」", str(rep["issues"]))

print("[7] 没有文字：应判为文字缺失")
img, d = base()
draw_art(d)
rep, rc = run(img, "notext.png", LAYOUT)
ok(rc == 1, "无文字应返回 1", f"rc={rc}")
ok(rep["text_share"] < 0.001, "文字带应该几乎没有墨", f"{rep['text_share']:.4f}")

# ---------------------------------------------------------------- 负例：干净度

print("[8] 纸面噪点：应该判为纸不干净")
img, d = base()
draw_art(d)
draw_words(d, 0.5, 0.80, 0.30, 22)
draw_words(d, 0.5, 0.875, 0.40, 9)
# 全画面的纸区都加抖动（模拟纸纹），色块内部不动
px = img.load()
seed = 12345
for y in range(H):
    for x in range(W):
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        v = (seed % 41) - 20
        r, g, b = px[x, y]
        if abs(r - PAPER[0]) + abs(g - PAPER[1]) + abs(b - PAPER[2]) < 12:
            px[x, y] = (max(0, min(255, r + v)), max(0, min(255, g + v)), max(0, min(255, b + v)))
rep, rc = run(img, "noisy.png", LAYOUT)
ok(rc == 1, "纸纹噪点应返回 1", f"rc={rc}")
ok(any("标准差" in i for i in rep["issues"]), "应报出纸面噪声", str(rep["issues"]))

print("[9] 整体高斯模糊：应该判为发虚")
img = make_good().filter(ImageFilter.GaussianBlur(3.2))
rep, rc = run(img, "blur.png", LAYOUT)
ok(rc == 1, "模糊图应返回 1", f"rc={rc}")
ok(any("发虚" in i for i in rep["issues"]), "应报出「发虚」", str(rep["issues"]))

# ---------------------------------------------------------------- 提示词质量门

print("[10] 提示词装配器与质量门")

SPEC_OK = {
    "name": "selftest",
    "style_anchor": {"raw": "水墨扁平重构插画"},
    "source": {"path": "assets/ref.jpg", "crop": "画面上半 55%",
               "keep": "远山脊线、湖岸、两株树的轮廓", "drop": "碎石、栏杆、水面粼光"},
    "structure": {
        "skeleton": "一条从左高向右低的斜折线贯穿画面，折点在山体最窄处",
        "planes": [
            {"name": "远山", "form": "压成坡度 12 度的梯形", "role": "主面"},
            {"name": "近岸", "form": "一条横向的窄带", "role": "第二层"},
            {"name": "水面", "form": "一个浅色的大矩形", "role": "底"},
            {"name": "树", "form": "两根竖线加一片扁椭圆", "role": "点缀"},
        ],
        "seam": "山体与水面之间用一条比纸略深的横带收口",
    },
    "palette": {"from": "pal.json", "accent": "一点赭红，面积不超过 2%"},
    "wash": {"places": ["山脚与水面交接处"], "density": "极淡，8%–15%",
             "stroke": "一笔横拖，起笔略重收笔渐轻"},
    "type": {"title": "Still Water", "desc": "a quiet study of mountains and reflection"},
    "layout": {"whitespace": 0.52, "block_top": 0.10, "block_bottom": 0.60,
               "block_width": 0.68, "title_y": 0.80, "desc_y": 0.875, "gap": 0.08},
    "aspect": "4:5",
    "size": "1638x2048",
}

PAL = {
    "paper": {"hex": "#F4EFE4", "rgb": [244, 239, 228]},
    "ink": {"hex": "#2E2C27", "rgb": [46, 44, 39]},
    "clusters": [
        {"hex": "#7C8C84", "rgb": [124, 140, 132], "share": 0.42, "role": "primary", "flat": {"hex": "#9DABA3"}},
        {"hex": "#C9CEC2", "rgb": [201, 206, 194], "share": 0.26, "role": "secondary", "flat": {"hex": "#D3D8CC"}},
        {"hex": "#3D4844", "rgb": [61, 72, 68], "share": 0.14, "role": "support", "flat": {"hex": "#5F6C67"}},
        {"hex": "#B08560", "rgb": [176, 133, 96], "share": 0.05, "role": "accent", "flat": {"hex": "#BC9878"}},
    ],
}


def gate(spec: dict, pal: dict | None = PAL, name: str = "selftest") -> list[tuple[str, str]]:
    d = Path(_TMP) / name
    d.mkdir(exist_ok=True)
    if pal is not None:
        (d / "pal.json").write_text(json.dumps(pal, ensure_ascii=False), encoding="utf-8")
    s = json.loads(json.dumps(spec))
    s["_spec_dir"] = str(d)
    _, issues = bpp.build(s)
    return issues


def has(issues, code: str) -> bool:
    return any(code in m for _, m in issues)


def errors(issues) -> list[str]:
    return [m for lv, m in issues if lv == "error"]


iss = gate(SPEC_OK)
ok(not errors(iss), "合格 spec 不应有硬错误", "；".join(errors(iss))[:200])
ok(has(iss, "已翻译成可执行条款"), "「水墨」应被翻译成条款并给出 info")
prompt, _ = bpp.build({**SPEC_OK, "_spec_dir": str(Path(_TMP) / "selftest")})
ok(prompt.count("【") >= 10, "提示词应包含至少 10 个结构段", str(prompt.count("【")))
ok("#7C8C84" in prompt, "色板 hex 应写进提示词")
ok("Still Water" in prompt, "标题应写进提示词")

s = json.loads(json.dumps(SPEC_OK))
s["type"]["title"] = "山 水"
iss = gate(s)
ok(has(iss, "E112"), "中文标题应被拦下（E112）", "；".join(errors(iss))[:160])

s = json.loads(json.dumps(SPEC_OK))
s["palette"] = {}
iss = gate(s)
ok(has(iss, "E124"), "没有色板应被拦下（E124）", "；".join(errors(iss))[:160])

s = json.loads(json.dumps(SPEC_OK))
s["notes"] = "加一点渐变会更高级"
iss = gate(s)
ok(has(iss, "E130"), "高危词「渐变」应被拦下（E130）", "；".join(errors(iss))[:160])

s = json.loads(json.dumps(SPEC_OK))
s["layout"]["block_bottom"] = 0.78
iss = gate(s)
ok(has(iss, "E106"), "插画压到标题应被拦下（E106）", "；".join(errors(iss))[:160])

s = json.loads(json.dumps(SPEC_OK))
s["size"] = "2048x2048"
iss = gate(s)
ok(has(iss, "E122"), "画幅与尺寸不一致应被拦下（E122）", "；".join(errors(iss))[:160])

s = json.loads(json.dumps(SPEC_OK))
s["structure"]["planes"] = [{"name": f"面{i}", "form": "一个色块"} for i in range(11)]
iss = gate(s)
ok(has(iss, "W104"), "几何面超过 9 个应给出警告（W104）")

s = json.loads(json.dumps(SPEC_OK))
s["layout"]["whitespace"] = 0.2
iss = gate(s)
ok(has(iss, "W106"), "留白不足应给出警告（W106）")

s = json.loads(json.dumps(SPEC_OK))
s["wash"] = {}
iss = gate(s)
ok(has(iss, "W105"), "没写晕染位置应给出警告（W105）")

s = json.loads(json.dumps(SPEC_OK))
s["type"]["title"] = "A Study of the Quiet Surface of Water at Dawn"
iss = gate(s)
ok(has(iss, "W111"), "标题过长应给出警告（W111）")

# ---------------------------------------------------------------- 汇总

print()
if fails:
    print(f"FAILED  {len(fails)}/{checks}")
    for f in fails:
        print("  ✗ " + f)
    print(f"\n临时目录：{_TMP}")
    sys.exit(1)

print(f"PASSED  {checks}/{checks} 断言全部通过")
print(f"临时目录：{_TMP}")
sys.exit(0)
