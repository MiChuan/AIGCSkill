#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hy Image3.5 低调电影感人像 · 提示词装配器。

把一份 portrait spec（JSON）装配成结构化中文提示词，并做这道题专属的质量门：
发色锁、伦勃朗三角光斑、光比、拖影三铁律、纯黑背景、黑衣黑底可见性冲突。

纯标准库，Python 3.9+。

用法:
    python build_portrait_prompt.py --spec specs/lowkey-rembrandt.json --outdir out/lowkey
    python build_portrait_prompt.py --spec specs/lowkey-rembrandt.json --check
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

SIZE_MAP = {
    "3:2": ("2048x1365", "1536x1024", (7680, 5120)),
    "2:3": ("1365x2048", "1024x1536", (5120, 7680)),
    "4:5": ("1638x2048", "1229x1536", (6144, 7680)),
    "16:9": ("2048x1152", "1536x864", (7680, 4320)),
    "9:16": ("1152x2048", "864x1536", (4320, 7680)),
    "1:1": ("2048x2048", "1536x1536", (7680, 7680)),
}

DEFAULT_LOCKS = [
    "脸型骨相与颧骨-下颌线条",
    "五官轮廓与比例",
    "眼型、眼距与双眼皮形态",
    "鼻梁高度与鼻头形状",
    "唇形与唇厚",
]

TRAIL_COLOR_ALIASES = {"衣物", "衣服", "服装", "costume", "wardrobe", "outfit"}

TAIL_FORBIDDEN = [
    "正面平光", "蝴蝶光", "环形光", "大柔光",
    "背景灰雾", "背景渐变", "光斑", "杂物",
    "拖影覆盖人物或脸部", "彩色拖影", "发光拖影", "多种颜色并存",
    "磨皮塑料感", "过度 HDR", "卡通化", "CG 渲染感", "AI 涂抹",
    "胡须", "发色改变", "脸型改变",
]


def _end(text: str) -> str:
    """补齐中文句末标点，避免分节之间粘连。"""
    return text if text and text[-1] in "。！？；" else text + "。"


def _drop_notes(text: str) -> str:
    """去掉括号内的补充说明，用于判断衣物真实颜色（避免「深灰而非纯黑」被当成黑衣物）。"""
    import re
    prev = None
    while prev != text:
        prev = text
        text = re.sub(r"[（(][^）)]*[）)]", "", text)
    return text

TASK_DEFAULT = (
    "低调电影感人像摄影（low-key cinematic photography），超写实原生摄影，"
    "专业影棚布光，非插画、非3D渲染、非CG感。"
)

HAIR_DEFAULT = "发色、发根色、发型走向、发际线必须与参考照片一致，受光面只提亮不改色相。"

GRADE_DEFAULT = (
    "低饱和、低曝光、高质感：整体冷调中性，肤色不过暖不过橙；"
    "黑位压实，中间调收紧；细腻均匀的胶片颗粒（不结块）；"
    "整体气质{mood}，呈现{poster}。"
)

QUALITY_DEFAULT = (
    "8K 级超写实原生摄影质感：sharp focus, cinematic, high detail, professional studio photography；"
    "真实发丝（含碎发与受光高光）、睫毛、毛孔与皮肤纹理、布料纤维与缝线清晰可辨；"
    "无涂抹感、无过度锐化 halo。"
)


# ---------- 各段落 ----------

def identity_block(char: dict) -> tuple[str, str, list[str]]:
    """返回 (身份锁, 发色锁, 警告)"""
    warns: list[str] = []
    locks = char.get("identity_locks") or DEFAULT_LOCKS

    identity = (
        "以参考照片中的人物为唯一身份来源："
        + "、".join(locks)
        + "必须与参考照片完全一致；不得换脸、不得改变脸型胖瘦、不得美颜磨皮、不得改年龄气质。"
    )

    hair = str(char.get("hair_lock", "")).strip()
    if not hair:
        warns.append(
            "W101 缺发色锁 character.hair_lock：只写「发色不变」不够——伦勃朗光会在头发上打出一条高光带，"
            "模型很容易顺手把整头发色提亮或改浅。请写明发色+发根色+受光区色泽+发型走向+发际线。"
        )
        hair = HAIR_DEFAULT
    return identity, hair, warns


def state_block(char: dict) -> str:
    parts: list[str] = []
    age = str(char.get("apparent_age", "")).strip()
    if age:
        parts.append(f"年龄观感：{age}")
    skin = str(char.get("skin", "")).strip() or "皮肤保留真实纹理与毛孔，无磨皮、无塑料感"
    parts.append(skin)
    beard = str(char.get("beard", "")).strip()
    if beard:
        parts.append(beard)
    expr = str(char.get("expression", "")).strip() or "神情克制，视线不笑，嘴角放松"
    parts.append(f"表情：{expr}")
    return "；".join(parts) + "。"


def light_block(light: dict) -> tuple[str, list[str]]:
    warns: list[str] = []
    scheme = str(light.get("scheme", "Rembrandt")).strip()
    ratio = str(light.get("ratio", "")).strip()
    if not ratio:
        warns.append(
            "W102 缺 lighting.ratio：不写光比，模型容易给成「一侧亮、一侧略暗」的普通侧光，不是 low-key"
        )
        ratio = "1:8"

    if scheme.lower() in ("rembrandt", "伦勃朗"):
        text = (
            "伦勃朗布光（Rembrandt lighting）：单一强硬侧光作为唯一主光，"
            "位置在人物前侧约 45°、高于眼位约 45°；"
            "主光照亮靠近镜头的一侧脸，在远侧脸颊形成标志性的三角形光斑"
            "（由鼻影与脸颊阴影交出一小块受光三角，这是伦勃朗光的识别特征）；"
            "另一半脸落入深邃阴影；"
            f"明暗光比约 {ratio}；"
            "不使用补光、不使用蝴蝶光/环形光/大柔光、不加轮廓灯与背景灯；"
            "高光不溢出，暗部保留结构与皮肤质感，可压到接近死黑但不得糊成一片灰雾。"
        )
    else:
        warns.append(f"lighting.scheme「{scheme}」不是伦勃朗，会走通用低调布光，三角光斑不会出现")
        text = (
            f"低调布光（low-key），方案：{scheme}；单侧硬光为主光，光比约 {ratio}；"
            "不使用补光与背景灯，高光不溢出，暗部保留结构。"
        )
    return text, warns


def trail_block(trail: dict, wardrobe: str) -> tuple[str, list[str], list[str]]:
    warns: list[str] = []
    errors: list[str] = []
    ratio_raw = trail.get("length_ratio", 0.4)
    try:
        ratio = float(ratio_raw)
    except (TypeError, ValueError):
        ratio = 0.4
        warns.append(f"W103 trail.length_ratio 不是数字（{ratio_raw}），按 0.4 处理")

    if ratio > 0.6:
        warns.append(f"W103 拖影长度 {ratio:.0%} 超过主体宽度的 60%：会抢掉人物，改到 30%–50%")
    elif ratio < 0.15:
        warns.append(f"W103 拖影长度 {ratio:.0%} 太短，看不出速度感，改到 30%–50%")

    color_src = str(trail.get("color_source", "衣物")).strip()
    if color_src.lower() not in TRAIL_COLOR_ALIASES:
        errors.append(
            f"E201 trail.color_source「{color_src}」不符合要求：本题要求拖影颜色严格等于衣物颜色（填「衣物」）"
        )

    side = str(trail.get("side", "身后左侧")).strip()
    intensity = str(trail.get("intensity", "轻微")).strip()

    text = (
        f"在人物{side}添加{intensity}的横向动态拖影（horizontal motion blur trail）："
        "拖影自人物轮廓水平向左延展，"
        f"长度约为人物宽度的 {int(round(ratio * 100))}%，随距离逐渐消散、不留硬边；"
        "拖影颜色与人物衣物的颜色严格统一（同一色相、同一明度，取衣物受光面的颜色），"
        "不得出现彩虹色散、不得发光、不得出现第二种颜色；"
        "人物本体保持清晰锐利，拖影只存在于身后轮廓之外，不得覆盖脸部、头发与身体。"
    )

    if "黑" in _drop_notes(wardrobe) and color_src.lower() in TRAIL_COLOR_ALIASES:
        warns.append(
            "W104 衣物描述含「黑」，背景又是纯黑：拖影取衣物色后几乎不可见。"
            "建议衣物改成深炭灰/深藏青（仍属 low-key），或明确拖影取衣物的受光高光色。"
        )
    return text, warns, errors


def background_block(bg: dict) -> str:
    extras = str(bg.get("extras", "")).strip()
    text = (
        "背景为完全纯黑的摄影棚背景（#000000），无杂物、无渐变、无光斑、无烟雾、无飞尘；"
        "人物与背景之间没有漏光边缘，轮廓靠光比自然收暗"
    )
    if extras:
        text += "；" + extras
    return text + "。"


def compose_block(comp: dict, aspect: str) -> str:
    pos = str(comp.get("subject_position", "画面右侧三分之一线")).strip()
    framing = str(comp.get("framing", "胸部以上半身")).strip()
    gap = str(comp.get("negative_space", "画面左侧保留大块纯黑负空间承接拖影")).strip()
    return f"{aspect} 画幅；人物位于{pos}；{framing}；{gap}。"


def build(spec: dict) -> tuple[str, dict, list[str], list[str]]:
    warns: list[str] = []
    errors: list[str] = []

    name = str(spec.get("name") or "portrait").strip()
    aspect = str(spec.get("aspect") or "3:2").strip()
    if aspect not in SIZE_MAP:
        warns.append(f"W106 画幅「{aspect}」不在预设内，按 3:2 处理（可选：{', '.join(SIZE_MAP)}）")
        aspect = "3:2"
    elif aspect != "3:2":
        warns.append(f"W106 画幅为 {aspect}，原始需求是 3:2 横画幅，确认是有意改的")
    size, size_fallback, out_8k = SIZE_MAP[aspect]

    char = spec.get("character") or {}
    ref = str(char.get("ref", "")).strip()
    if not ref:
        errors.append("E101 character.ref 为空：没有参考照片就无法锁长相与发色")
    elif not ref.startswith(("http://", "https://")) and not Path(ref).exists():
        warns.append(f"参考照片不存在：{ref}（相对路径请确认工作目录）")

    if not str(char.get("beard", "")).strip():
        warns.append("W107 未写胡须状态：干净少年感通常要显式写「脸部干净无胡须」")

    identity, hair, w = identity_block(char)
    warns += w

    wardrobe = str(spec.get("wardrobe", "")).strip()
    if not wardrobe:
        warns.append("W108 未写衣物：拖影颜色要取衣物色，衣物不写死，拖影就没有基准色")
        wardrobe = "深炭灰色修身外套，面料有细腻冷调反光"

    light, w = light_block(spec.get("lighting") or {})
    warns += w

    trail, w, e = trail_block(spec.get("trail") or {}, wardrobe)
    warns += w
    errors += e

    mood = str(spec.get("mood", "硬朗、冷酷、帅气")).strip()
    poster = str(spec.get("poster", "高级电影海报质感")).strip()
    grade = str(spec.get("grade", "")).strip() or GRADE_DEFAULT.format(mood=mood, poster=poster)
    quality = str(spec.get("quality", "")).strip() or QUALITY_DEFAULT

    blocks = [
        "【任务】" + str(spec.get("task", TASK_DEFAULT)).strip(),
        "【人物锁定】" + identity,
        "【发色锁定】" + _end(hair),
        "【人物状态】" + state_block(char),
        "【服装】" + _end(wardrobe) + "拖影颜色以此为准。",
        "【光线】" + light,
        "【背景】" + background_block(spec.get("background") or {}),
        "【构图】" + compose_block(spec.get("composition") or {}, aspect),
        "【动态拖影】" + trail,
        "【质感与调色】" + grade,
        "【画质】" + quality,
    ]

    forbidden = [str(f).strip() for f in (spec.get("forbidden") or []) if str(f).strip()]
    for must in ("水印", "文字", "logo"):
        if not any(must in f for f in forbidden):
            forbidden.append(must)
    tail = [t for t in TAIL_FORBIDDEN if not any(t in f for f in forbidden)]
    blocks.append("【禁止】" + "、".join(forbidden + tail) + "。")

    prompt = "\n\n".join(blocks)

    meta = {
        "name": name,
        "aspect": aspect,
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "imagegen": {
            "prompt_file": "prompt.txt",
            "image1": ref or None,
            "size": size,
            "size_fallback": size_fallback,
            "quality": "high",
            "input_fidelity": "high",
            "revise": False,
            "footnote": None,
        },
        "postcheck": {"target": "8k", "expected_size": list(out_8k), "check_background": True},
        "trail": {
            "length_ratio": spec.get("trail", {}).get("length_ratio", 0.4),
            "color_source": spec.get("trail", {}).get("color_source", "衣物"),
        },
        "tool_note": "revise 必须 false：提示词一被改写，发色锁与三角光斑条款就没了。footnote 留空即无水印。",
    }
    return prompt, meta, warns, errors


def main() -> int:
    ap = argparse.ArgumentParser(description="低调电影感人像 · 提示词装配器")
    ap.add_argument("--spec", required=True)
    ap.add_argument("--outdir")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    spec_path = Path(args.spec)
    if not spec_path.exists():
        raise SystemExit(f"[spec] 文件不存在：{spec_path}")
    spec = json.loads(spec_path.read_text(encoding="utf-8-sig"))

    prompt, meta, warns, errors = build(spec)

    if args.outdir and not args.check:
        outdir = Path(args.outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "prompt.txt").write_text(prompt, encoding="utf-8")
        (outdir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps({"prompt": prompt, "meta": meta, "warnings": warns, "errors": errors},
                         ensure_ascii=False, indent=2))
    else:
        print(prompt)
        print()
        for w in warns:
            print(f"[warn] {w}")
        for e in errors:
            print(f"[error] {e}")
        if not warns and not errors:
            print("[ok] 质量门全过")
        if args.outdir and not args.check:
            print(f"[out] {Path(args.outdir).as_posix()}/prompt.txt, meta.json")

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
