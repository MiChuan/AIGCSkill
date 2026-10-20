#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发型代码诊断器：测量值 -> 三位代码 -> 处方 -> 查表推荐（纯标准库）。

三位代码 = 脸型 + 最宽处 + 指宽：

    位 1  脸型     A 长脸 / B 方脸 / C 宽脸（圆脸、短宽脸）
    位 2  最宽处   A 额部 / B 颧骨 / C 下颌
    位 3  指宽     A 三指 / B 四指 / C 五指

9 行款式表按前两位索引（脸型 × 最宽处），第三位只调刘海露额度与顶部体积。

用法:

    # 1) 全自动：给测量数字，脚本自己判代码
    python scripts/haircode.py --forehead 7.2 --face-length 18.6 \\
        --temple 14.6 --cheek 15.6 --jaw 14.2 --hair fine

    # 2) 半自动：已经判好了，直接给结论
    python scripts/haircode.py --shape long --widest cheek --fingers 4 --hair hard

    # 3) 生成测量网格图（读出百分比坐标，再用 1) 或 2) 喂回来）
    python scripts/haircode.py --guide assets/face.jpg --out out/guide.png

    # 4) 把第 N 款推荐导出成出图 spec
    python scripts/haircode.py --shape long --widest cheek --fingers 4 --hair hard \\
        --emit-spec --style 1 --ref assets/face.jpg --out-spec specs/session-01.json

每个判定都打印判据，不是黑箱打分——判错了你能看出是哪一步判错的。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data" / "hair-matrix.json"

SHAPE_ALIAS = {
    "a": "A", "long": "A", "长脸": "A",
    "b": "B", "square": "B", "方脸": "B",
    "c": "C", "wide": "C", "round": "C", "宽脸": "C", "圆脸": "C",
}
WIDEST_ALIAS = {
    "a": "A", "forehead": "A", "temple": "A", "额": "A", "额头": "A",
    "b": "B", "cheek": "B", "zygomatic": "B", "颧骨": "B",
    "c": "C", "jaw": "C", "chin": "C", "下颌": "C", "下巴": "C",
}
HAIR_ALIAS = {
    "a": "A", "fine": "A", "细软": "A",
    "b": "B", "hard": "B", "straight": "B", "硬直": "B",
    "c": "C", "curl": "C", "curly": "C", "自然卷": "C",
}

FINGER_WIDTH_CM = 2.2      # 成年人一根手指横量的宽度
FACE_LENGTH_CM = 18.5      # 只用于把像素换回厘米（发际线到下巴）

STRUCT_LABEL = (
    ("part", "分缝"), ("fringe", "刘海"), ("side", "两侧"),
    ("nape", "后颈"), ("layer", "层次"), ("volume", "体积"),
)
STRUCT_KEYS = tuple(k for k, _ in STRUCT_LABEL)


def load_matrix() -> dict:
    return json.loads(DATA.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ 判定

def judge_fingers(forehead_cm: float, finger_w: float = FINGER_WIDTH_CM) -> tuple[str, str]:
    """额头高度 -> 三/四/五指。边界取相邻整数的中点。"""
    n = forehead_cm / finger_w
    if n < 3.5:
        return "A", f"{forehead_cm:.1f}cm ÷ {finger_w}cm = {n:.2f} 指，不到 3.5 指"
    if n < 4.5:
        return "B", f"{forehead_cm:.1f}cm ÷ {finger_w}cm = {n:.2f} 指，落在 3.5–4.5 指之间"
    return "C", f"{forehead_cm:.1f}cm ÷ {finger_w}cm = {n:.2f} 指，超过 4.5 指"


def judge_shape(face_len: float, widths: dict) -> tuple[str, str]:
    """脸型：长宽比为主，下颌/颧宽比为辅。widths 的键是 temple / cheek / jaw。"""
    w = max(widths.values())
    ratio = face_len / w
    jaw_cheek = widths["jaw"] / widths["cheek"]

    if ratio >= 1.55:
        return "A", f"脸长/脸宽 = {face_len:.1f}/{w:.1f} = {ratio:.2f} ≥ 1.55，纵向明显长于横向"
    if jaw_cheek >= 0.86:
        return "B", (
            f"脸长/脸宽 = {ratio:.2f}（不到 1.55），且下颌宽/颧宽 = {jaw_cheek:.2f} ≥ 0.86，"
            "下颌角方、下颌与颧骨差不多同宽"
        )
    return "C", (
        f"脸长/脸宽 = {ratio:.2f}（不到 1.55），下颌宽/颧宽 = {jaw_cheek:.2f} < 0.86，"
        "轮廓收得快、下颌角不突出"
    )


def judge_widest(widths: dict) -> tuple[str, str, str]:
    """最宽处取三个宽度的最大值；差距小于 1.5% 视为并列，给一条提示。"""
    order = [("A", "temple", "额部"), ("B", "cheek", "颧骨"), ("C", "jaw", "下颌")]
    top = max(order, key=lambda t: widths[t[1]])
    tol = widths[top[1]] * 0.015
    rivals = [t for t in order if t[0] != top[0] and widths[top[1]] - widths[t[1]] < tol]
    detail = "、".join(f"{n} {widths[k]:.1f}" for _, k, n in order)
    if rivals:
        warn = (
            f"{top[2]}与{rivals[0][2]}相差不到 1.5%（{detail}），属于并列最宽："
            f"先按「{top[2]}」处理；如果两侧轮廓很圆，换成「{rivals[0][2]}」那一列的款式会更稳"
        )
        return top[0], f"{detail}，最大的是{top[2]}", warn
    return top[0], f"{detail}，最大的是{top[2]}", ""


def pick(alias: dict, value: str, flag: str) -> str:
    key = value.strip().lower()
    if key in alias:
        return alias[key]
    if value.strip() in alias:
        return alias[value.strip()]
    raise SystemExit(f"{flag} 不认识：{value}；可选 {sorted(set(alias.values()))} 或中文名")


# ------------------------------------------------------------------ 按指宽改写刘海

def tweak_fringe(fingers: str, fringe: str, style_name: str) -> tuple[str, list[str]]:
    """指宽影响的是「露多少额」，不是款式本身。返回 (追加到刘海的句子, 提示)。"""
    rules = load_matrix()["rules"]["fingers"][fingers]
    notes: list[str] = []
    if fingers == "C":
        exposed = ("露出额头" in fringe) or ("开方" in style_name)
        if exposed:
            notes.append(
                f"H602 「{style_name}」在额头处是开/露设计，与五指额头（上庭偏长）打架："
                "已追加遮额改写；想彻底解决就换一款不露额的"
            )
        return rules["fringe_adjust"], notes
    if fingers == "A":
        if "压到" in fringe:
            notes.append("H603 三指额头（上庭偏短）配压到眉下的刘海会更显脸短，刘海至少留到眉上")
        return "", notes
    return "", notes


# ------------------------------------------------------------------ 主判定

def build(args) -> dict:
    m = load_matrix()
    notes: list[str] = []

    widths = {}
    for key, val in (("temple", args.temple), ("cheek", args.cheek), ("jaw", args.jaw)):
        if val is not None:
            widths[key] = float(val)

    if args.shape:
        shape = pick(SHAPE_ALIAS, args.shape, "--shape")
        shape_why = "手动指定"
    elif len(widths) == 3 and args.face_length:
        shape, shape_why = judge_shape(float(args.face_length), widths)
    else:
        raise SystemExit("脸型判不了：要么给 --shape，要么同时给 --face-length 与 --temple/--cheek/--jaw")

    if args.widest:
        widest = pick(WIDEST_ALIAS, args.widest, "--widest")
        widest_why = "手动指定"
    elif len(widths) == 3:
        widest, widest_why, widest_warn = judge_widest(widths)
        if widest_warn:
            notes.append("H601 " + widest_warn)
    else:
        raise SystemExit("最宽处判不了：要么给 --widest，要么给 --temple/--cheek/--jaw")

    if args.fingers:
        f = args.fingers.strip()
        if f not in ("3", "4", "5"):
            raise SystemExit("--fingers 只能填 3 / 4 / 5")
        fingers = {"3": "A", "4": "B", "5": "C"}[f]
        fingers_why = "手动指定"
    elif args.forehead:
        fh = float(args.forehead)
        if args.unit == "px":
            if not args.face_length:
                raise SystemExit("像素单位下判指宽需要 --face-length（用来换算成厘米）")
            scale = args.face_length_cm / float(args.face_length)
            fh = fh * scale
            notes.append(
                f"H604 像素换算假设：脸长（发际线到下巴）= {args.face_length_cm}cm，"
                f"比例 {scale:.4f} cm/px。脸明显偏大或偏小的照片请改用 --unit cm 或 --face-length-cm"
            )
        fingers, fingers_why = judge_fingers(fh, args.finger_width)
    else:
        raise SystemExit("指宽判不了：要么给 --fingers 3/4/5，要么给 --forehead（配合 --unit）")

    if not args.hair:
        raise SystemExit("缺 --hair（fine / hard / curl，或 细软 / 硬直 / 自然卷）")
    hair = pick(HAIR_ALIAS, args.hair, "--hair")

    code = shape + widest + fingers
    cell = m["cells"].get(shape + widest)
    if not cell:
        raise SystemExit(f"数据表里没有 cell {shape + widest}，hair-matrix.json 可能被改坏了")

    ht = m["hairtypes"][hair]

    prescription = [
        ("脸型", m["rules"]["shape"][shape]),
        ("最宽处", m["rules"]["widest"][widest]),
        ("指宽", m["rules"]["fingers"][fingers]),
        ("发质", {
            "label": ht["name"],
            "prescription": ht["cut_logic"],
            "avoid": ht["never"],
            "why": "发质决定这些款式能不能撑起来：剪得再好，发丝撑不住也是塌的。",
        }),
    ]

    recs = []
    for i, name in enumerate(cell["styles"][hair], 1):
        base = dict(cell["structure"])
        base.update(m.get("overrides", {}).get(shape + widest, {}).get(name, {}))
        extra, style_notes = tweak_fringe(fingers, base["fringe"], name)
        notes.extend(style_notes)
        if extra:
            base["fringe"] = base["fringe"] + "；" + extra
        clash = [w for w in ht["never"] if w.split("（")[0] in name]
        if clash:
            notes.append(f"H605 「{name}」在发质「{ht['name']}」的黑名单里（{clash[0]}），别选")
        recs.append({
            "index": i,
            "name": name,
            "structure": base,
            "why": cell["why"],
            "risk": cell["risk"],
            "clash": clash,
        })

    avoid: list[str] = []
    seen_core: set[str] = set()
    for _, rule in prescription:
        for a in rule.get("avoid", []):
            core = a.split("（")[0].strip()
            if core in seen_core:
                continue
            seen_core.add(core)
            avoid.append(core)

    blob = " ".join(recs[0]["structure"].values()) if recs else ""
    if hair == "A" and ("中长" in blob or "长发" in blob):
        notes.append(
            "H606 细软发配中长/长发，重量会压在头顶上：剪裁必须是高层次、发根必须有支撑，"
            "出图时要写死「发根立起」，否则生成结果会是贴头皮的一片"
        )
    if hair == "C" and ("齐" in blob and "刘海" in blob):
        notes.append("H607 自然卷配齐刘海会炸成一个三角，刘海必须碎剪")

    return {
        "code": code,
        "alias_graphic": cell["alias"],
        "face": cell["face"],
        "shape": {"code": shape, "label": m["rules"]["shape"][shape]["label"], "why": shape_why},
        "widest": {"code": widest, "label": m["rules"]["widest"][widest]["label"], "why": widest_why},
        "fingers": {"code": fingers, "label": m["rules"]["fingers"][fingers]["label"], "why": fingers_why},
        "hairtype": {"code": hair, "label": ht["name"], "how_to_tell": ht["how_to_tell"]},
        "prescription": [
            {"axis": k, "label": v["label"], "rule": v["prescription"], "why": v["why"]}
            for k, v in prescription
        ],
        "recommend": recs,
        "avoid": avoid,
        "issues": notes,
    }


def spec_from(report: dict, index: int, ref: str | None, out_path: Path) -> dict:
    rec = next((r for r in report["recommend"] if r["index"] == index), None)
    if not rec:
        raise SystemExit(f"没有第 {index} 款推荐（共 {len(report['recommend'])} 款）")
    spec = {
        "_note": "由 haircode.py --emit-spec 生成。structure 已按指宽改写过，不要再手改 fringe。",
        "code": report["code"],
        "alias_graphic": report["alias_graphic"],
        "hairtype": report["hairtype"]["code"],
        "style": {
            "name": rec["name"],
            "index": rec["index"],
            "why": rec["why"],
            "risk": rec["risk"],
            "structure": rec["structure"],
        },
        "ref": {
            "path": ref or "assets/face.jpg",
            "note": "参考图同时是身份、光线、背景、构图、衣物的唯一来源",
        },
        "frame": {"aspect": "3:4", "size": "1536x2048"},
        "keep": {
            "identity": "脸型骨相、五官比例、眼型、鼻、唇、肤色、年龄感、胡须与痣",
            "hairline": "发际线的高度与形状、额角、鬓角起点、发旋位置",
            "light": "参考图的光位、光比、色温",
            "background": "参考图的背景（同一面墙、同样的明暗与颜色）",
            "frame": "参考图的构图与镜头距离、头部在画面里的位置与大小",
            "expression": "参考图的表情",
            "wardrobe": "参考图里的衣服",
        },
        "negative_extra": [],
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    return spec


def draw_guide(src: Path, out: Path) -> None:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        raise SystemExit("--guide 需要 Pillow：pip install pillow")
    im = Image.open(src).convert("RGB")
    if im.width > 1400:
        im = im.resize((1400, round(im.height * 1400 / im.width)), Image.LANCZOS)
    d = ImageDraw.Draw(im)
    for i in range(1, 20):
        y = round(im.height * 0.05 * i)
        d.line([(0, y), (im.width, y)], fill=(0, 255, 200), width=1)
        d.text((4, max(0, y - 12)), f"y={i * 5}", fill=(0, 255, 200))
    for i in range(1, 19):
        x = round(im.width * 0.05 * i)
        col = (255, 120, 0) if i == 10 else (0, 255, 200)
        d.line([(x, 0), (x, im.height)], fill=col, width=1)
        if i % 2 == 0:
            d.text((x + 2, 4), f"x={i * 5}", fill=col)
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out)
    print(f"guide -> {out.as_posix()}  ({im.width}x{im.height})")
    print(
        "网格每 5% 一条，数字是百分比。读这 6 个数（只用单位一致就行，脚本只算比例）：\n"
        "  发际线中点 y     眉心 y（不是眉尾）      下巴最低点 y\n"
        "  额宽 x 跨度（两侧太阳穴最凹处）  颧宽 x 跨度（颧弓最外点）  下颌宽 x 跨度（下颌角最外点）\n"
        "喂回脚本：--forehead = 眉心y - 发际线y ；--face-length = 下巴y - 发际线y ；\n"
        "          --temple/--cheek/--jaw = 三个 x 跨度 ；--unit px"
    )


# ------------------------------------------------------------------ 输出

def print_report(r: dict) -> None:
    line = "=" * 66
    print(line)
    print(f"代码 {r['code']}    原图对应 {r['alias_graphic']}    {r['face']}")
    print(f"发质 {r['hairtype']['code']} {r['hairtype']['label']}")
    print(line)

    print("\n【怎么判出来的】")
    print(f"  脸型   {r['shape']['label']:<6}  {r['shape']['why']}")
    print(f"  最宽处 {r['widest']['label']:<8}  {r['widest']['why']}")
    print(f"  指宽   {r['fingers']['label']:<8}  {r['fingers']['why']}")

    print("\n【处方】")
    for p in r["prescription"]:
        print(f"\n  {p['axis']}｜{p['label']}")
        print(f"    {p['rule']}")
        print(f"    -- {p['why']}")

    print(f"\n【推荐：查表得 {len(r['recommend'])} 款】")
    for rec in r["recommend"]:
        print(f"\n  [{rec['index']}] {rec['name']}")
        for k, label in STRUCT_LABEL:
            print(f"      {label}：{rec['structure'][k]}")
        print(f"      为什么：{rec['why']}")
        print(f"      注意：{rec['risk']}")

    print("\n【避雷】")
    for a in r["avoid"]:
        print(f"  × {a}")

    if r["issues"]:
        print("\n【提示】")
        for i in r["issues"]:
            print(f"  ! {i}")

    print("\n【下一步】")
    print("  python scripts/haircode.py <同样的参数> --emit-spec --style 1 --ref <照片> --out-spec specs/s.json")
    print("  python scripts/build_hair_prompt.py --spec specs/s.json --outdir out/s1")


def main() -> int:
    ap = argparse.ArgumentParser(description="发型代码诊断 + 查表推荐")
    ap.add_argument("--shape", help="long / square / wide（长脸 / 方脸 / 宽脸）")
    ap.add_argument("--widest", help="forehead / cheek / jaw（额 / 颧 / 下颌）")
    ap.add_argument("--fingers", help="3 / 4 / 5（三指 / 四指 / 五指额头）")
    ap.add_argument("--hair", help="fine / hard / curl（细软 / 硬直 / 自然卷）")
    ap.add_argument("--forehead", help="发际线中点到眉心的高度")
    ap.add_argument("--face-length", dest="face_length", help="发际线中点到下巴最低点")
    ap.add_argument("--temple", help="额宽（两侧太阳穴）")
    ap.add_argument("--cheek", help="颧宽")
    ap.add_argument("--jaw", help="下颌宽")
    ap.add_argument("--unit", default="cm", choices=["cm", "px"], help="测量单位；比例判定与单位无关")
    ap.add_argument("--face-length-cm", dest="face_length_cm", type=float, default=FACE_LENGTH_CM,
                    help=f"仅像素单位下判指宽时用，默认 {FACE_LENGTH_CM}cm")
    ap.add_argument("--finger-width", dest="finger_width", type=float, default=FINGER_WIDTH_CM,
                    help=f"一根手指横量的宽度，默认 {FINGER_WIDTH_CM}cm")
    ap.add_argument("--json", dest="json_out", help="把完整报告写成 JSON")
    ap.add_argument("--emit-spec", action="store_true", help="导出第 N 款为出图 spec")
    ap.add_argument("--style", type=int, default=1, help="配合 --emit-spec，选第几款（从 1 开始）")
    ap.add_argument("--ref", help="参考图路径，写进 spec")
    ap.add_argument("--out-spec", dest="out_spec", help="spec 输出路径")
    ap.add_argument("--guide", help="生成测量网格图（需要 Pillow）")
    ap.add_argument("--out", help="配合 --guide 的输出路径")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if args.guide:
        draw_guide(Path(args.guide), Path(args.out or "out/measure-guide.png"))
        return 0

    report = build(args)
    print_report(report)

    if args.json_out:
        p = Path(args.json_out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\njson -> {p.as_posix()}")

    if args.emit_spec:
        out = Path(args.out_spec or f"specs/{report['code']}-{args.style}.json")
        spec_from(report, args.style, args.ref, out)
        print(f"spec -> {out.as_posix()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
