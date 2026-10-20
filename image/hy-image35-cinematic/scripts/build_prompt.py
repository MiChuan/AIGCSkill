#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hy Image3.5 剧集级提示词装配器。

把一份 spec（JSON）装配成可直接投喂给模型的「结构化中文提示词」，
同时做质量门检查、推导出图参数、写盘 prompt.txt / meta.json。

纯标准库，Python 3.9+。

用法:
    python build_prompt.py --spec specs/yujian-kunlun.json --outdir out/yujian-kunlun
    python build_prompt.py --spec specs/yujian-kunlun.json --check      # 只校验
    python build_prompt.py --spec specs/yujian-kunlun.json --json       # 输出 prompt 全文(JSON)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

# ---------- 词库 ----------

LENS_PRESETS = {
    "8": "鱼眼，极端畸变，画面边缘强烈拉扯",
    "14": "超广角，透视夸张，近大远小强烈，边缘有轻微桶形畸变",
    "16": "超广角，空间被撑开，适合大场景与窄室内",
    "24": "广角，环境信息量大，透视偏强但可读",
    "28": "广角，临场感强，畸变克制",
    "35": "人文广角，接近叙事视角，人物与环境比例自然",
    "50": "标准镜头，视角接近人眼，不夸张不压缩",
    "85": "中长焦人像，背景压缩，焦外柔化",
    "100": "中长焦，主体被拎出环境，压缩感明显",
    "135": "长焦，空间被压平，背景化为色块",
    "200": "超长焦，极度压缩，适合把主体从环境中孤立出来",
}

ANGLE_PRESETS = {
    "俯拍": "高位俯拍，镜头朝下压，地面/云海成为背景板",
    "平视": "平视机位，与人物视线齐高",
    "仰拍": "低角度仰拍，主体向上压向观众，气势外放",
    "鸟瞰": "近乎垂直的鸟瞰，人物成为画面中的坐标点",
    "过肩": "过肩视角，前景留出肩膀与后脑虚化块",
    "低角贴地": "贴地低机位，地面细节进入前景",
}

SIZE_MAP = {
    "16:9": ("2048x1152", (7680, 4320)),
    "9:16": ("1152x2048", (4320, 7680)),
    "4:3": ("2048x1536", (7680, 5760)),
    "3:4": ("1536x2048", (5760, 7680)),
    "3:2": ("2048x1365", (7680, 5120)),
    "2:3": ("1365x2048", (5120, 7680)),
    "1:1": ("2048x2048", (7680, 7680)),
    "21:9": ("2048x878", (8192, 3508)),
}

# 风格锚点：真实片名 → 可执行的画面语言（不是让模型"画得像那部剧"，
# 而是把剧集质感翻译成光比、色调、颗粒、镜头习惯）
STYLE_ANCHORS = {
    "长安十二时辰": "唐风写实，暖金与赭红主调，高光略溢，镜头贴近人物，颗粒明显，市井烟火气重",
    "琅琊榜": "宋式清雅，低饱和青灰，柔光大面积留白，构图端正规整，画面呼吸感强",
    "庆余年": "轻喜剧剧集质感，色彩明快偏暖，光线干净，人物面部平光讨喜",
    "流浪地球": "重工业科幻，冷蓝灰主调，硬光与机械高光，尘雾体积感强，颗粒克制",
    "繁花": "港风霓虹，强色偏洋红与青，浅景深，皮肤油润，暗部噪点颗粒突出",
    "权力的游戏": "史诗剧集质感，冷灰蓝与土褐，自然光为主，云层厚重，广角大场景",
    "卧虎藏龙": "东方武侠，青绿山水的色阶，柔雾，衣袂飘动强调，动静对比",
    "影": "水墨黑白灰，宣纸质感，极简构图，雨幕与墨色晕染",
    "沙丘": "巨物压迫感，土黄与石灰，硬边逆光，极简几何构图，尘埃颗粒",
    "银翼杀手": "赛博朋克，青橙对撞，雨夜霓虹反射，烟雾与光束，湿滑质感",
}

DEFAULT_LOCKS = [
    "脸型骨相与颧骨-下颌线条",
    "五官轮廓与比例",
    "眉眼间距、眼型与双眼皮形态",
    "鼻梁高度与鼻头形状",
    "唇形与唇厚",
    "发型、发际线与发量",
    "肤色与年龄感",
]

PLACEHOLDER_HINTS = ("参考剧集", "参考", "示例", "剧集", "xx", "XX", "某剧", "同款")


# ---------- 工具 ----------

def read_spec(path: Path) -> dict:
    text = path.read_text(encoding="utf-8-sig")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"[spec] JSON 解析失败：{exc}")


def pick_lens(lens: str) -> tuple[str, str]:
    digits = "".join(ch for ch in str(lens) if ch.isdigit())
    if not digits:
        return str(lens), ""
    key = digits
    if key not in LENS_PRESETS:
        nearest = min(LENS_PRESETS, key=lambda k: abs(int(k) - int(key)))
        return f"{key}mm", LENS_PRESETS[nearest] + "（近似焦段）"
    return f"{key}mm", LENS_PRESETS[key]


DEFAULT_STYLE = "电影剧集写实质感：自然光比、克制的调色、轻微胶片颗粒"


def resolve_style(anchor: dict | str | None) -> tuple[str, list[str]]:
    """返回 (风格描述, 警告列表)"""
    warns: list[str] = []
    if not anchor:
        return DEFAULT_STYLE, warns

    if isinstance(anchor, str):
        raw, mapped = anchor.strip(), ""
    else:
        raw = str(anchor.get("raw", "")).strip()
        mapped = str(anchor.get("mapped", "")).strip()

    if not raw and not mapped:
        return DEFAULT_STYLE, warns

    name = raw.strip("《》 ")
    if not mapped and name in STYLE_ANCHORS:
        mapped = STYLE_ANCHORS[name]

    is_placeholder = not name or any(h in name for h in PLACEHOLDER_HINTS)

    if not mapped:
        if is_placeholder:
            warns.append(
                f"风格锚点「{raw}」像占位符/未被识别：请在 spec 的 style_anchor.mapped 里写成可执行的画面语言"
                "（光比、主色调、颗粒、镜头习惯），或换真实片名"
            )
            return DEFAULT_STYLE, warns
        mapped = f"《{name}》式剧集质感：自然光比、电影调色、克制的胶片颗粒"

    if is_placeholder:
        return mapped, warns
    return f"参考《{name}》的剧集质感——{mapped}", warns


def face_clause(char: dict) -> tuple[str, str, list[str]]:
    """返回 (保脸条款, 人物表现条款, 警告)"""
    warns: list[str] = []
    locks = char.get("identity_locks") or DEFAULT_LOCKS
    visibility = str(char.get("visibility", "full_face"))
    expression = str(char.get("expression", "")).strip()

    lock = (
        "以参考图中的人物为唯一身份来源："
        + "、".join(locks)
        + "必须与参考图完全一致；不得换脸、不得美颜磨皮、不得改造脸型胖瘦比例、"
        "不得改变眼型与双眼皮形态、不得增减年龄感。"
    )

    perf = ""
    if visibility == "full_face":
        perf = "人物正脸（可微侧 15° 以内）朝向镜头附近，脸部受光清晰、不被遮挡，五官可逐一辨认。"
    elif visibility == "profile":
        warns.append("侧脸/大角度侧身会显著削弱保脸效果，只有剧情必须时才用；建议同时出一张正脸版做对照")
        perf = "人物以侧脸入画，仍需保持鼻梁、下颌线、眉骨、耳位的结构一致，禁止把侧脸画成另一个人。"
    elif visibility == "partly_occluded":
        warns.append("脸部存在遮挡（道具/手/头发/逆光），保脸上限会下降，务必检查眉眼与鼻口是否完整")
        perf = "即使局部被道具遮挡，露出的眉眼、鼻、唇也必须与参考图可对应，遮挡不得压住整张脸。"
    else:
        warns.append(f"未知 visibility「{visibility}」，按 full_face 处理")

    if char.get("with_face_fill", True):
        perf += "脸部在画面中的高度占比不低于 8%，避免远景小人脸。"

    if expression:
        perf += f"表情与动作：{expression}；表情变化只改变肌肉与视线，不改变五官形状。"

    return lock, perf, warns


def camera_block(cam: dict) -> tuple[str, list[str]]:
    warns: list[str] = []
    if not cam:
        warns.append("spec 缺少 camera 段：机位/焦段缺失等于放弃控制画面，建议补上")
        return "", warns

    parts: list[str] = []

    angle = str(cam.get("angle", "")).strip()
    if angle:
        parts.append(ANGLE_PRESETS.get(angle, angle))
    else:
        warns.append("camera.angle 缺失（俯拍/平视/仰拍/鸟瞰），画面视角将不受控")

    azimuth = str(cam.get("azimuth", "")).strip()
    if azimuth:
        parts.append(f"机位方位：{azimuth}")

    lens = str(cam.get("lens", "")).strip()
    if lens:
        lens_name, lens_desc = pick_lens(lens)
        parts.append(f"{lens_name} 镜头，{lens_desc}" if lens_desc else f"{lens_name} 镜头")
    else:
        warns.append("camera.lens 缺失，透视关系将随机")

    shot = str(cam.get("shot", "")).strip()
    if shot:
        parts.append(f"景别：{shot}")

    depth = str(cam.get("depth", "")).strip()
    if depth:
        parts.append(f"纵深层次：{depth}")

    extra = str(cam.get("extra", "")).strip()
    if extra:
        parts.append(extra)

    if not parts:
        return "", warns
    return "；".join(parts) + "。", warns


def build(spec: dict) -> tuple[str, dict, list[str], list[str]]:
    """返回 (prompt, meta, warnings, errors)"""
    warns: list[str] = []
    errors: list[str] = []

    name = str(spec.get("name") or "image").strip()
    aspect = str(spec.get("aspect") or "16:9").strip()
    if aspect not in SIZE_MAP:
        errors.append(f"aspect「{aspect}」不支持，可选：{', '.join(SIZE_MAP)}")
        aspect = "16:9"
    size, out_8k = SIZE_MAP[aspect]

    char = spec.get("character") or {}
    ref = str(char.get("ref", "")).strip()
    if not ref:
        errors.append("character.ref 为空：没有参考图就无法锁脸，请给出本地路径或 URL")
    elif not (ref.startswith("http://") or ref.startswith("https://")):
        if not Path(ref).exists():
            warns.append(f"参考图不存在：{ref}（相对路径请确认工作目录）")

    style_text, w = resolve_style(spec.get("style_anchor"))
    warns += w

    face_lock, face_perf, w = face_clause(char)
    warns += w

    cam_text, w = camera_block(spec.get("camera") or {})
    warns += w

    scene = str(spec.get("scene", "")).strip()
    if len(scene) < 20:
        warns.append("scene 太短（<20 字）：场景描述不足会让模型自由发挥，建议补到 60 字以上，含空间关系")

    corners = [str(c).strip() for c in (spec.get("corners") or []) if str(c).strip()]
    if not corners:
        warns.append("corners 为空：四角构图是最有效的构图控制手段，建议至少写 2 个方位")
    elif len(corners) > 4:
        warns.append("corners 超过 4 个方位：互相打架，删到 2–3 个")

    blocks: list[str] = []
    blocks.append("【任务】" + str(spec.get("task", "电影剧集单帧剧照，写实电影质感，宽银幕构图，非插画、非3D渲染、非塑料感CG。")).strip())
    blocks.append("【风格锚点】" + style_text)
    blocks.append("【人物锁定】" + face_lock)
    if face_perf:
        blocks.append("【人物表现】" + face_perf)

    costume = str(spec.get("costume", "")).strip()
    if costume:
        blocks.append("【服装道具】" + costume)

    if scene:
        blocks.append("【情节场景】" + scene)

    if corners:
        blocks.append("【构图·画面四角】" + "；".join(corners) + "。")

    if cam_text:
        blocks.append("【镜头】" + cam_text)

    for key, title in (("light", "【光影与大气】"), ("grade", "【质感与调色】"), ("detail", "【画质】")):
        val = str(spec.get(key, "")).strip()
        if val:
            blocks.append(title + val)

    # 禁止项：强制包含水印相关
    forbidden = [str(f).strip() for f in (spec.get("forbidden") or []) if str(f).strip()]
    for must in ("水印", "logo", "签名", "文字"):
        if not any(must in f for f in forbidden):
            forbidden.append(must)
    blocks.append("【禁止】任何" + "、".join(forbidden) + "；画面为完整单格，无画框、无跨页拼贴。")

    prompt = "\n\n".join(blocks)

    if len(prompt) > 1600:
        warns.append(f"提示词 {len(prompt)} 字偏长，约束会互相稀释，建议删掉与画面无关的修辞")

    meta = {
        "name": name,
        "aspect": aspect,
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "imagegen": {
            "prompt_file": "prompt.txt",
            "image1": ref or None,
            "image2": str(char.get("ref2", "")).strip() or None,
            "size": size,
            "quality": "high",
            "input_fidelity": "high",
            "revise": False,
            "footnote": None,
        },
        "finalize": {
            "target": "8k",
            "expected_size": list(out_8k),
            "watermark": "auto",
        },
        "camera": spec.get("camera") or {},
        "tool_note": "footnote 留空即不请求水印；revise 必须 false，否则保脸与机位条款会被改写掉。",
    }
    return prompt, meta, warns, errors


# ---------- CLI ----------

def main() -> int:
    ap = argparse.ArgumentParser(description="Hy Image3.5 提示词装配器")
    ap.add_argument("--spec", required=True, help="spec JSON 路径")
    ap.add_argument("--outdir", help="输出目录（写 prompt.txt 与 meta.json）")
    ap.add_argument("--check", action="store_true", help="只校验，不写文件")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出 prompt 与检查结果")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    spec_path = Path(args.spec)
    if not spec_path.exists():
        raise SystemExit(f"[spec] 文件不存在：{spec_path}")

    spec = read_spec(spec_path)
    prompt, meta, warns, errors = build(spec)

    if args.outdir and not args.check:
        outdir = Path(args.outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "prompt.txt").write_text(prompt, encoding="utf-8")
        (outdir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    if args.json:
        print(json.dumps(
            {"prompt": prompt, "meta": meta, "warnings": warns, "errors": errors},
            ensure_ascii=False, indent=2,
        ))
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
