#!/usr/bin/env python3
"""把结构化 spec 装配成多档提示词，并用质量门拦空泛词。

反推提示词最常见的错误是「写满形容词、不给参数」。这个脚本反着做：
spec 里每一项都必须对应一个可执行的量（光位 / 画幅 / 色值 / 景深），
空泛词（好看 / 电影感 / 高级感）出现就报警。

四档输出对应不同用途：
    short    一句话，40 词内，用于快速重出 / 检索 / 存库
    natural  整句段落，6 段，给吃自然语言的模型（GPT-image / Hy / 豆包 / 即梦）
    mj       逗号短语 + 参数后缀，给 Midjourney
    sd       逗号标签 + negative prompt，给 Stable Diffusion 系

用法：
    python scripts/build_prompt.py --spec specs/dune.json --outdir out/dune
    python scripts/build_prompt.py --spec specs/dune.json --outdir out/dune --probe out/probe/probe.json

依赖：纯标准库（不读图）。--probe 只读 probe.py 的 JSON。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# ------------------------------------------------------------------ 词表

VAGUE = ["好看", "精美", "高级感", "电影感", "氛围感", "艺术感", "唯美", "质感",
         "细节丰富", "大师级", "超清", "画质极致", "栩栩如生", "精美绝伦",
         "cinematic", "masterpiece", "best quality", "highly detailed",
         "beautiful", "stunning", "ultra detailed"]

FILLER = ["稍微", "大概", "或许", "尽量", "可能", "一点", "有点",
          "approximately", "maybe", "a bit", "kind of", "somewhat"]

NEG_HINT = ["不要", "没有", "no ", "without", "avoid", "请勿", "不要有", "不出现"]

STD_ASPECTS = {"1:1", "4:5", "5:4", "2:3", "3:2", "3:4", "4:3",
               "9:16", "16:9", "21:9", "9:21", "16:10", "10:16"}

HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


# ------------------------------------------------------------------ 读取与合并

def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        sys.exit("[error] 文件不存在：%s" % path)
    except json.JSONDecodeError as e:
        sys.exit("[error] JSON 解析失败 %s：%s" % (path, e))


def _short_label(s):
    """probe 的诊断标签带括号说明（「水平带状光源（地平线发光…）」），进提示词只留主词。"""
    return (s or "").split("（")[0].strip()


def merge_probe(spec, probe):
    """probe 的量只填 spec 的空位，绝不覆盖人写的判断。"""
    filled = []
    if not probe:
        return filled

    def put(path, value):
        node = spec
        for key in path[:-1]:
            node = node.setdefault(key, {})
        if node.get(path[-1]) in (None, "", [], {}):
            node[path[-1]] = value
            filled.append(".".join(path))

    light = probe.get("light", {})
    put(("light", "direction"), _short_label(light.get("direction")))
    put(("light", "quality"), _short_label(light.get("quality")))
    put(("light", "key_fill"), "%.1f:1" % light.get("key_fill_ratio", 0))
    put(("light", "contrast"), _short_label(probe.get("brightness", {}).get("label")))
    put(("light", "temp"), _short_label(probe.get("temperature", {}).get("label")))
    put(("camera", "dof"), _short_label(probe.get("dof", {}).get("label")))
    put(("camera", "sharpness"), _short_label(probe.get("sharpness", {}).get("label")))
    put(("composition", "aspect"), probe.get("aspect"))
    put(("composition", "subject_position"), probe.get("subject", {}).get("position"))
    put(("composition", "line_hint"), _short_label(probe.get("composition", {}).get("line_hint")))
    if probe.get("vignette", {}).get("corner_to_edge", 1) < 0.90:
        put(("style", "vignette"), "轻微暗角")
    if probe.get("noise", {}).get("rms", 0) >= 3.0:
        put(("material",), spec.get("material", []) + ["明显颗粒（胶片感）"])
    if not spec.get("color_palette"):
        pal = [p for p in probe.get("palette", []) if p.get("share", 0) >= 0.05][:5]
        if pal:
            spec["color_palette"] = [
                {"name": p["name"], "hex": p["hex"], "share": p["share"]} for p in pal]
            filled.append("color_palette")
    return filled


# ------------------------------------------------------------------ 质量门

def count_words(text):
    en = len(re.findall(r"[A-Za-z0-9']+", text))
    zh = len(re.findall(r"[一-鿿]", text))
    return en + zh // 2


def hex_dist(a, b):
    if not (HEX_RE.match(a) and HEX_RE.match(b)):
        return 765
    pa = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    pb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return sum(abs(x - y) for x, y in zip(pa, pb))


def run_gates(spec, probe, prompts):
    """返回 [{"id","level","msg"}]。E 阻断交付，W 只是提醒。"""
    issues = []

    def e(code, msg):
        issues.append({"id": code, "level": "E", "msg": msg})

    def w(code, msg):
        issues.append({"id": code, "level": "W", "msg": msg})

    sj = spec.get("subject", {}) or {}
    what = (sj.get("what") or "").strip()
    if not what:
        e("E101", "subject.what 为空：没有主体的提示词不是提示词")
    elif len(what) < 4 and not sj.get("detail"):
        e("E102", "subject.what 只有 %d 字且无 detail：「一个人」「一个美女」这种主体等于没写" % len(what))

    cam = spec.get("camera", {}) or {}
    if not (cam.get("shot") or cam.get("lens") or cam.get("angle")):
        e("E103", "camera 全空：不写景别 / 焦段 / 机位，模型会替你随机一个")

    light = spec.get("light", {}) or {}
    if not (light.get("direction") or light.get("quality")):
        e("E104", "light.direction / quality 都空：光是不写就会默认「大平光」的那一项")

    env = spec.get("environment", {}) or {}
    if not env.get("place"):
        e("E105", "environment.place 为空：没有环境，主体就悬在虚空里")

    joined = json.dumps(spec, ensure_ascii=False)
    hits = [t for t in VAGUE if t in joined]
    if hits:
        w("W201", "空泛词 %s：这些词模型不认，换成具体参数（如「电影感」→「2.39:1 + 青橙分离 + 高光柔化」）" % "、".join(hits))
    fillers = [t for t in FILLER if t in joined]
    if fillers:
        e("E106", "不可执行词 %s：提示词里没有「大概 / 可能」的位置" % "、".join(fillers))

    for seg, text in prompts.items():
        pos = text.get("positive", "")
        for hint in NEG_HINT:
            if hint in pos:
                e("E108", "%s 版正向段含「%s」：负面内容请移到 negatives" % (seg, hint.strip()))
        if seg == "short" and count_words(pos) > 45:
            w("W203", "short 版 %d 词，超过 45：short 的价值就是短" % count_words(pos))
        if seg == "natural" and count_words(pos) > 130:
            w("W204", "natural 版 %d 词偏长：超出部分模型会丢，把次要的往后挪" % count_words(pos))

    refs = (spec.get("style", {}) or {}).get("references", []) or []
    if len(refs) > 3:
        w("W202", "风格参考 %d 个超过 3 个：参考太多会互相稀释，留最强的两个" % len(refs))

    asp = (spec.get("composition", {}) or {}).get("aspect")
    if asp:
        a = str(asp).strip().lstrip("~")
        if a not in STD_ASPECTS:
            e("E107", "画幅「%s」不在标准比里：用 1:1 / 3:2 / 2:3 / 16:9 这种标准比" % asp)
    else:
        e("E107", "composition.aspect 为空：画幅是生成参数的第一位")

    if probe:
        pasp = probe.get("aspect")
        if asp and pasp and str(asp).lstrip("~") != pasp:
            w("W206", "spec 画幅 %s 与原图 %s 不一致：若是有意换画幅，请确认" % (asp, pasp))
        pal = probe.get("palette", [])
        for c in spec.get("color_palette", []) or []:
            hx = c.get("hex") if isinstance(c, dict) else c
            if isinstance(hx, str) and HEX_RE.match(hx):
                dmin = min((hex_dist(hx, p["hex"]) for p in pal), default=765)
                if dmin > 60:
                    w("W207", "色板 %s 与原图主色距离 %d：反推的色不该离开原图，除非是再创作" % (hx, dmin))
        pos = probe.get("subject", {}).get("position")
        spos = (spec.get("composition", {}) or {}).get("subject_position")
        if pos and spos and pos not in spos and spos not in pos:
            w("W208", "spec 主体位置「%s」与 probe 判读「%s」不符" % (spos, pos))
    else:
        w("W209", "没跑 probe：spec 里的光位 / 色板 / 画幅全靠肉眼，建议先 python scripts/probe.py")

    return issues


# ------------------------------------------------------------------ 四档装配

def _d(dct, key, default=""):
    v = (dct or {}).get(key)
    if isinstance(v, list):
        return "、".join(str(x) for x in v if x)
    return str(v) if v else default


def palette_text(spec, lang="zh"):
    pal = spec.get("color_palette", []) or []
    parts = []
    for p in pal[:5]:
        if isinstance(p, dict):
            parts.append("%s %s" % (p.get("name", ""), p.get("hex", "")))
        else:
            parts.append(str(p))
    return "、".join(parts)


def build_short(spec):
    sj = spec.get("subject", {})
    cam = spec.get("camera", {})
    light = spec.get("light", {})
    env = spec.get("environment", {})
    parts = [x for x in [
        _d(sj, "what"), _d(spec, "action"),
        _d(env, "place"), _d(light, "time"),
        _d(light, "direction"), _d(cam, "shot"),
        _d(cam, "lens"), _d(spec.get("style", {}), "medium"),
    ] if x]
    return "，".join(parts)


def build_natural(spec):
    sj = spec.get("subject", {})
    env = spec.get("environment", {})
    light = spec.get("light", {})
    cam = spec.get("camera", {})
    style = spec.get("style", {})
    comp = spec.get("composition", {})
    mat = spec.get("material", []) or []

    seg = []
    sub = "，".join(x for x in [_d(sj, "what"), "；".join(sj.get("detail", []) or []),
                                _d(spec, "action")] if x)
    seg.append("【主体】%s。" % sub)
    env_parts = "，".join(x for x in [_d(env, "place"), _d(env, "foreground"),
                                      _d(env, "background")] if x)
    if env_parts:
        seg.append("【环境】%s。" % env_parts)
    light_parts = "，".join(x for x in [_d(light, "direction"), _d(light, "quality"),
                                        _d(light, "time"), _d(light, "temp"),
                                        _d(light, "contrast")] if x)
    if light_parts:
        seg.append("【光】%s。" % light_parts)
    cam_parts = "，".join(x for x in [_d(cam, "shot"), _d(cam, "lens"), _d(cam, "aperture"),
                                      _d(cam, "angle"), _d(cam, "dof")] if x)
    if cam_parts:
        seg.append("【镜头】%s。" % cam_parts)
    if mat:
        seg.append("【材质】%s。" % "；".join(str(m) for m in mat))
    tail = []
    if style.get("medium"):
        tail.append(style["medium"])
    if style.get("render"):
        tail.append(style["render"])
    if style.get("references"):
        tail.append("参考：" + "、".join(style["references"]))
    if style.get("vignette"):
        tail.append(style["vignette"])
    pal = palette_text(spec)
    if pal:
        tail.append("主色：" + pal)
    comp_parts = "，".join(x for x in [_d(comp, "aspect"), _d(comp, "subject_position"),
                                       _d(comp, "line_hint")] if x)
    if comp_parts:
        tail.append("画幅与构图：" + comp_parts)
    if tail:
        seg.append("【风格与画幅】%s。" % "，".join(tail))
    txt = (spec.get("text_in_image") or "").strip()
    if txt:
        seg.append("【图中文字】只出现「%s」，其余位置不出字。" % txt)
    return "\n".join(seg)


def build_mj(spec):
    sj = spec.get("subject", {})
    env = spec.get("environment", {})
    light = spec.get("light", {})
    cam = spec.get("camera", {})
    style = spec.get("style", {})
    comp = spec.get("composition", {})
    parts = [x for x in [
        _d(sj, "what"), _d(sj, "detail"), _d(spec, "action"),
        _d(env, "place"), _d(env, "foreground"), _d(env, "background"),
        _d(light, "direction"), _d(light, "quality"), _d(light, "time"),
        _d(cam, "shot"), _d(cam, "lens"), _d(cam, "angle"), _d(cam, "dof"),
        _d(style, "medium"), _d(style, "render"),
    ] if x]
    pal = palette_text(spec, lang="en")
    if pal:
        parts.append("palette " + pal)
    suffix = []
    asp = _d(comp, "aspect")
    if asp:
        suffix.append("--ar %s" % asp.lstrip("~"))
    if style.get("stylize") is not None:
        suffix.append("--s %s" % style["stylize"])
    return ", ".join(parts) + ("  " + " ".join(suffix) if suffix else "")


def build_sd(spec):
    sj = spec.get("subject", {})
    env = spec.get("environment", {})
    light = spec.get("light", {})
    cam = spec.get("camera", {})
    style = spec.get("style", {})
    parts = [x for x in [
        _d(style, "medium"), _d(sj, "what"), _d(sj, "detail"), _d(spec, "action"),
        _d(env, "place"), _d(env, "foreground"), _d(env, "background"),
        _d(light, "direction"), _d(light, "quality"), _d(light, "time"),
        _d(cam, "shot"), _d(cam, "lens"), _d(cam, "dof"), _d(style, "render"),
    ] if x]
    pal = palette_text(spec)
    if pal:
        parts.append("palette " + pal)
    neg = [str(n) for n in (spec.get("negatives", []) or [])]
    neg += ["watermark", "text", "logo"] if not spec.get("text_in_image") else []
    return ", ".join(parts), ", ".join(dict.fromkeys(neg))


# ------------------------------------------------------------------ 报告与输出

def build_all(spec):
    natural = build_natural(spec)
    sd_pos, sd_neg = build_sd(spec)
    return {
        "short": {"positive": build_short(spec)},
        "natural": {"positive": natural},
        "mj": {"positive": build_mj(spec)},
        "sd": {"positive": sd_pos, "negative": sd_neg},
    }


def write_report(outdir, spec, probe, prompts, issues, filled):
    lines = ["# 提示词装配报告", ""]
    if filled:
        lines.append("probe 自动填入的字段：%s" % "、".join(filled))
        lines.append("")
    lines.append("## 质量门")
    lines.append("")
    if not issues:
        lines.append("- 全过。")
    else:
        for it in issues:
            lines.append("- **%s %s** %s" % (it["level"], it["id"], it["msg"]))
    lines.append("")
    lines.append("## 各档长度")
    lines.append("")
    lines.append("| 档 | 正向词数 | 负面词数 |")
    lines.append("| --- | --- | --- |")
    for name, p in prompts.items():
        lines.append("| %s | %d | %d |" % (name, count_words(p.get("positive", "")),
                                              count_words(p.get("negative", ""))))
    lines.append("")
    lines.append("## 提示词")
    for name, p in prompts.items():
        lines.append("")
        lines.append("### %s" % name)
        lines.append("")
        lines.append("```")
        lines.append(p["positive"])
        lines.append("```")
        if p.get("negative"):
            lines.append("negative：`%s`" % p["negative"])
    Path(outdir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="把结构化 spec 装配成 short / natural / mj / sd 四档提示词，并跑质量门。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python scripts/build_prompt.py --spec specs/dune.json --outdir out/dune\n"
               "  python scripts/build_prompt.py --spec specs/dune.json --outdir out/dune --probe out/probe/probe.json\n")
    ap.add_argument("--spec", required=True, help="spec JSON 路径")
    ap.add_argument("--outdir", default="out/prompt", help="输出目录")
    ap.add_argument("--probe", default=None, help="probe.json 路径：自动补缺 + 一致性校验")
    ap.add_argument("--strict", action="store_true", help="有 E 级问题也照常写出（默认报错退出）")
    args = ap.parse_args(argv)

    spec = load_json(args.spec)
    probe = load_json(args.probe) if args.probe else None

    filled = merge_probe(spec, probe)
    prompts = build_all(spec)
    issues = run_gates(spec, probe, prompts)

    hard = [i for i in issues if i["level"] == "E"]
    for it in issues:
        print("[%s %s] %s" % (it["level"], it["id"], it["msg"]))
    if filled:
        print("[info] probe 已补：%s" % "、".join(filled))

    if hard and not args.strict:
        print("[error] %d 个 E 级问题未过，未写出文件；确认要放行请加 --strict" % len(hard))
        return 2

    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    for name, p in prompts.items():
        (out / ("prompt-%s.txt" % name)).write_text(p["positive"] + "\n", encoding="utf-8")
        if p.get("negative"):
            (out / "prompt-sd-negative.txt").write_text(p["negative"] + "\n", encoding="utf-8")
    meta = {"spec": args.spec, "probe": args.probe,
            "issues": issues, "filled_by_probe": filled,
            "words": {k: count_words(v.get("positive", "")) for k, v in prompts.items()}}
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
                                   encoding="utf-8")
    write_report(out, spec, probe, prompts, issues, filled)
    print("[ok] 写出 %s（%d 档 + report.md）" % (out.resolve(), len(prompts)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
