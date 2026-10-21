#!/usr/bin/env python3
"""回归自测：全部用内存合成的图，不需要任何外部素材。

跑法：
    python tests/test_scripts.py

覆盖：imgkit 基础量、probe 的光位 / 色板 / 景深 / 主体 / 文字嫌疑判读、
build_prompt 质量门、verify 的评分方向。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from PIL import Image, ImageDraw, ImageFilter  # noqa: E402

import imgkit as K  # noqa: E402
import probe as P  # noqa: E402
import build_prompt as B  # noqa: E402
import verify as V  # noqa: E402

FAILS = []
COUNT = [0]


def check(name, cond, detail=""):
    COUNT[0] += 1
    if cond:
        print("[pass] %s" % name)
    else:
        FAILS.append(name)
        print("[FAIL] %s  %s" % (name, detail))


# ------------------------------------------------------------ 合成测试图

def solid(color, size=(480, 320)):
    return Image.new("RGB", size, color)


def hgrad(left, right, size=(480, 320)):
    w, h = size
    im = Image.new("RGB", size)
    px = im.load()
    for x in range(w):
        f = x / float(w - 1)
        c = tuple(int(l + (r - l) * f) for l, r in zip(left, right))
        for y in range(h):
            px[x, y] = c
    return im


def center_spot(size=(480, 320)):
    im = solid((30, 30, 34), size)
    dr = ImageDraw.Draw(im)
    w, h = size
    dr.ellipse([w * 0.38, h * 0.30, w * 0.62, h * 0.70], fill=(240, 235, 220))
    return im


def mid_band(size=(480, 320)):
    im = solid((30, 30, 40), size)
    dr = ImageDraw.Draw(im)
    w, h = size
    dr.rectangle([0, int(h * 0.33), w, int(h * 0.66)], fill=(235, 200, 150))
    return im


def red_blue(size=(480, 320)):
    im = solid((200, 40, 40), size)
    dr = ImageDraw.Draw(im)
    w, h = size
    dr.rectangle([w // 2, 0, w, h], fill=(40, 60, 200))
    return im


def dof_image(size=(480, 320)):
    base = Image.effect_noise(size, 30).convert("RGB").filter(ImageFilter.GaussianBlur(4))
    dr = ImageDraw.Draw(base)
    w, h = size
    for i in range(0, 12):
        for j in range(0, 8):
            c = (255, 255, 255) if (i + j) % 2 else (0, 0, 0)
            dr.rectangle([w * 0.32 + i * 10, h * 0.30 + j * 10,
                          w * 0.32 + (i + 1) * 10, h * 0.30 + (j + 1) * 10], fill=c)
    return base


def vignette_image(size=(480, 320)):
    im = solid((170, 170, 170), size)
    mask = Image.new("L", size, 0)
    px = mask.load()
    w, h = size
    for y in range(h):
        for x in range(w):
            nx = (x / float(w - 1) - 0.5) * 2
            ny = (y / float(h - 1) - 0.5) * 2
            r = min(1.0, (nx * nx + ny * ny) ** 0.5 / 1.3)
            px[x, y] = int(255 * (1 - 0.55 * r * r))
    return Image.composite(im, solid((0, 0, 0), size), mask)


def text_bottom_image(size=(480, 320)):
    im = solid((150, 150, 150), size)
    dr = ImageDraw.Draw(im)
    w, h = size
    for x in range(0, w, 6):
        dr.rectangle([x, int(h * 0.90), x + 3, int(h * 0.97)], fill=(0, 0, 0))
    return im


def subject_image(size=(480, 320)):
    im = solid((200, 200, 200), size)
    dr = ImageDraw.Draw(im)
    dr.rectangle([200, 120, 280, 200], fill=(20, 20, 20))
    return im


def probe_of(im):
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "t.png"
        im.save(p)
        d, _ = P.analyze(p)
    return d


# ------------------------------------------------------------ imgkit 基础

def test_imgkit():
    check("aspect 2:3", K.aspect_label(800, 1200) == "2:3")
    check("aspect 4:5", K.aspect_label(1638, 2048) == "4:5")
    check("aspect 1:1", K.aspect_label(2048, 2048) == "1:1")
    check("aspect 16:9", K.aspect_label(1920, 1080) == "16:9")
    check("color 红", K.color_name(255, 0, 0) == "红")
    check("color 黑", K.color_name(20, 20, 20) == "黑")
    check("color 米", K.color_name(228, 214, 184) == "米")

    im, work = K.load_rgb_from(solid((128, 128, 128))) if hasattr(K, "load_rgb_from") else (None, None)
    check("load_rgb 接口存在", callable(K.load_rgb))


# ------------------------------------------------------------ probe 判读

def test_probe():
    d = probe_of(solid((128, 128, 128)))
    check("纯色亮度", abs(d["brightness"]["mean"] - 128 / 255.0) < 0.02,
          str(d["brightness"]["mean"]))
    check("纯色反差", d["brightness"]["contrast"] < 0.05, str(d["brightness"]["contrast"]))
    check("纯色光位=漫射", "漫射" in d["light"]["direction"], d["light"]["direction"])
    check("纯色单主色", d["palette"][0]["share"] > 0.85, str(d["palette"][0]))
    check("纯色主体=无", d["subject"]["area_share"] < 0.05, str(d["subject"]["area_share"]))
    check("纯色噪声=干净", "干净" in d["noise"]["label"], d["noise"]["label"])

    d = probe_of(hgrad((245, 245, 245), (10, 10, 10)))
    check("左亮右暗→左侧光", "左" in d["light"]["direction"], d["light"]["direction"])

    d = probe_of(center_spot())
    check("中心亮斑→聚光", "聚光" in d["light"]["direction"] or "中心" in d["light"]["direction"],
          d["light"]["direction"])
    check("中心亮斑→候选在中心", d["subject"]["centroid"] is not None and
          0.4 < d["subject"]["centroid"][0] < 0.6, str(d["subject"]))

    d = probe_of(mid_band())
    check("中排亮带→水平带状光源", "带状" in d["light"]["direction"], d["light"]["direction"])

    d = probe_of(red_blue())
    hexes = [p["hex"] for p in d["palette"][:2]]
    check("红蓝双色板", any(h.startswith("#c") or h.startswith("#d") or h.startswith("#a") for h in hexes)
          and any(h.endswith("c8") or "3c" in h for h in hexes), str(hexes))
    shares = sorted((p["share"] for p in d["palette"][:2]), reverse=True)
    check("双色各半", shares[0] - shares[1] < 0.08, str(shares))

    d = probe_of(dof_image())
    check("中心锐利→浅景深", d["dof"]["center_edge_ratio"] > 1.3,
          "%.2f" % d["dof"]["center_edge_ratio"])

    d = probe_of(vignette_image())
    check("压暗四角→暗角", "暗角" in d["vignette"]["label"], d["vignette"]["label"])

    d = probe_of(subject_image())
    check("中心方块→主体候选", d["subject"]["area_share"] > 0.01 and
          "中心" in d["subject"]["position"], str(d["subject"]))

    d = probe_of(text_bottom_image())
    check("底部条纹→文字嫌疑", d["text_suspect"]["flag"], str(d["text_suspect"]))


# ------------------------------------------------------------ build_prompt 门禁

GOOD_SPEC = {
    "subject": {"what": "独行旅人剪影", "detail": ["长风衣"]},
    "action": "静立于沙丘脊线上",
    "environment": {"place": "黄昏沙漠"},
    "light": {"direction": "左侧光", "quality": "硬光"},
    "camera": {"shot": "远景全身", "lens": "35mm"},
    "style": {"medium": "写实摄影"},
    "composition": {"aspect": "3:2"},
    "negatives": ["文字"],
}


def test_gates():
    spec = dict(GOOD_SPEC)
    prompts = B.build_all(spec)
    issues = B.run_gates(spec, None, prompts)
    check("好 spec 无 E 级", not any(i["level"] == "E" for i in issues),
          json.dumps(issues, ensure_ascii=False))
    check("无 probe 时提醒 W209", any(i["id"] == "W209" for i in issues))

    spec = {"subject": {"what": ""}, "camera": {}, "light": {}, "environment": {}}
    issues = B.run_gates(spec, None, B.build_all(spec))
    ids = {i["id"] for i in issues}
    check("空主体 E101", "E101" in ids)
    check("空镜头 E103", "E103" in ids)
    check("空光 E104", "E104" in ids)
    check("空环境 E105", "E105" in ids)
    check("空画幅 E107", "E107" in ids)

    spec = dict(GOOD_SPEC)
    spec["style"] = {"medium": "写实摄影，好看，电影感"}
    issues = B.run_gates(spec, None, B.build_all(spec))
    check("空泛词 W201", any(i["id"] == "W201" for i in issues))

    spec = dict(GOOD_SPEC)
    spec["action"] = "大概站在沙丘上"
    issues = B.run_gates(spec, None, B.build_all(spec))
    check("不可执行词 E106", any(i["id"] == "E106" for i in issues))

    spec = dict(GOOD_SPEC)
    spec["composition"] = {"aspect": "7:3"}
    issues = B.run_gates(spec, None, B.build_all(spec))
    check("非法画幅 E107", any(i["id"] == "E107" for i in issues))

    spec = dict(GOOD_SPEC)
    spec["environment"] = {"place": "沙漠，不要有人"}
    prompts = B.build_all(spec)
    issues = B.run_gates(spec, None, prompts)
    check("正向段混入负面 E108", any(i["id"] == "E108" for i in issues))

    spec = dict(GOOD_SPEC)
    probe = {"light": {"direction": "右侧 90° 侧光（示例）", "quality": "硬光（示例）",
                       "key_fill_ratio": 6.0},
             "brightness": {"label": "高反差"}, "temperature": {"label": "微暖"},
             "dof": {"label": "浅景深（背景明显虚化）"},
             "sharpness": {"label": "正常"},
             "aspect": "3:2", "subject": {"position": "左三分 / 下三分"},
             "composition": {"line_hint": "明显水平线（地平线）"},
             "vignette": {"corner_to_edge": 0.97},
             "noise": {"rms": 1.0},
             "palette": [{"hex": "#956a46", "share": 0.3, "name": "棕"}]}
    filled = B.merge_probe(spec, probe)
    check("probe 补空位", "camera.dof" in filled and "light.key_fill" in filled, str(filled))
    check("probe 不覆盖人写的 direction", spec["light"]["direction"] == "左侧光"
          and "light.direction" not in filled)
    check("probe 不覆盖人写的", spec["light"]["quality"] == "硬光", spec["light"]["quality"])
    check("probe 补画幅不冲突", spec["composition"]["aspect"] == "3:2")
    check("probe 色板注入", spec["color_palette"][0]["hex"] == "#956a46")
    check("括号标签被截短", spec["camera"]["dof"] == "浅景深", spec["camera"]["dof"])

    spec = dict(GOOD_SPEC)
    prompts = B.build_all(spec)
    check("四档齐全", set(prompts) == {"short", "natural", "mj", "sd"})
    check("sd 有 negative", "negative" in prompts["sd"] and "watermark" in prompts["sd"]["negative"])
    check("short 不超 45 词", B.count_words(prompts["short"]["positive"]) <= 45)
    check("natural 含六段", prompts["natural"]["positive"].count("【") >= 5)
    check("mj 带 --ar", "--ar 3:2" in prompts["mj"]["positive"])


# ------------------------------------------------------------ verify 评分

def test_verify():
    with tempfile.TemporaryDirectory() as td:
        a = Path(td) / "a.png"
        b = Path(td) / "b.png"
        c = Path(td) / "c.png"
        subject_image().save(a)
        subject_image().save(b)
        solid((20, 30, 60), (320, 480)).save(c)

        v, _, _ = V.compare(a, b)
        check("同图满分", v["score"] == 100, str(v["score"]))

        v, _, _ = V.compare(a, c)
        check("异图低分", v["score"] < 60, str(v["score"]))
        check("异图扣分指向画幅", any(x["item"] == "画幅" and x["loss"] > 0 for x in v["checks"]))

        v, _, _ = V.compare(a, a)
        check("通过判定", "通过" in v["verdict"])


def main():
    test_imgkit()
    test_probe()
    test_gates()
    test_verify()
    print("")
    print("=" * 48)
    if FAILS:
        print("FAIL %d / %d" % (len(FAILS), COUNT[0]))
        for f in FAILS:
            print("  -", f)
        return 1
    print("ALL PASS %d 项" % COUNT[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
