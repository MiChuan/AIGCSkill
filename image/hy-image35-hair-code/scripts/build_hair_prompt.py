#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只换头发：结构化提示词装配器（纯标准库）。

输入一份 spec（由 haircode.py --emit-spec 生成，或手写），输出：

    prompt.txt    正面提示词（8 段，中文结构化）
    negative.txt  负面提示词（一行）
    meta.json     出图参数、质量门结论、可交付说明

核心约束是「只换头发」：参考图同时是身份、光线、背景、构图、衣物的唯一来源，
除了发际线以上那一块，其它像素应该几乎不动。所以提示词里必须有一段
「保持不变」，而且必须锁发际线——发际线一改，脸的比例就变了，等于换脸。

用法:
    python scripts/build_hair_prompt.py --spec specs/example.json --outdir out/1
    python scripts/build_hair_prompt.py --spec specs/example.json --check
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data" / "hair-matrix.json"

ASPECTS = {
    "3:4": "1536x2048",
    "4:5": "1600x2000",
    "1:1": "2048x2048",
    "2:3": "1360x2040",
}

STRUCT_KEYS = ("part", "fringe", "side", "nape", "layer", "volume")
STRUCT_LABEL = {
    "part": "分缝", "fringe": "刘海", "side": "两侧",
    "nape": "后颈", "layer": "层次", "volume": "体积",
}

# 只写「适中」「稍长」这类词，模型每次都会给你不同的长度——发型必须落在具体解剖位置上
VAGUE = ["适中", "稍长", "略长", "稍短", "略短", "正常长度", "随便", "差不多", "大概", "一点点", "自然一点"]

# 保脸模式下不能出现的改形表述
RESHAPE = ["瘦脸", "小脸", "V 脸", "瓜子脸", "尖下巴", "削骨", "改脸型", "改变脸型", "脸型更瘦", "骨相更立体"]

DEFAULT_NEGATIVES = [
    "换脸", "另一个人", "五官改变", "脸型改变", "发际线改变", "发际线上移", "发际线后移",
    "改变光线方向", "改变背景", "改变构图", "换衣服", "换表情", "加帽子", "加发饰", "加耳环",
    "头发糊成一片", "头发像帽子", "假发感", "塑料发丝", "过度磨皮", "油画质感", "卡通", "插画",
    "多余人物", "多余手臂", "文字", "水印", "logo", "签名", "画面边框",
    "低画质", "模糊", "噪点", "过度锐化",
]


# ------------------------------------------------------------------ 质量门

def load_matrix() -> dict:
    return json.loads(DATA.read_text(encoding="utf-8"))


def gate(spec: dict, m: dict) -> list[tuple[str, str]]:
    issues: list[tuple[str, str]] = []
    code = str(spec.get("code", "")).strip().upper()
    hair = str(spec.get("hairtype", "")).strip().upper()
    style = spec.get("style") or {}
    struct = style.get("structure") or {}
    keep = spec.get("keep") or {}
    frame = spec.get("frame") or {}

    if not re.fullmatch(r"[ABC]{3}", code):
        issues.append(("error", f"E601 code={code!r} 不是三位 A/B/C，形如 ABB"))
        return issues
    cell = m["cells"].get(code[:2])
    if not cell:
        issues.append(("error", f"E602 code 前两位 {code[:2]} 在表里找不到，只能是 AA/AB/AC/BA/BB/BC/CA/CB/CC"))
    if hair not in m["hairtypes"]:
        issues.append(("error", f"E603 hairtype={hair!r} 不合法，只能是 A（细软）/ B（硬直）/ C（自然卷）"))

    # 结构完整性
    missing = [k for k in STRUCT_KEYS if not str(struct.get(k, "")).strip()]
    if missing:
        issues.append(("error", f"E604 结构缺项：{missing}；六项缺一项，模型就会自己补一个你没要的"))

    # 模糊长度
    for k in ("fringe", "side", "nape"):
        v = str(struct.get(k, ""))
        for w in VAGUE:
            if w in v:
                issues.append((
                    "error",
                    f"E605 结构.{k} 里出现「{w}」：长度必须落在解剖位置（眉上/耳中/颌线/颈中），"
                    "写模糊词等于把决定权交给模型",
                ))
                break

    # 保「不变」的底线
    if not keep.get("hairline"):
        issues.append(("error", "E606 缺 keep.hairline：不发际线锁，换出来的就是另一个人"))
    for k, label in (("light", "光线"), ("background", "背景"), ("frame", "构图")):
        if not keep.get(k):
            issues.append((
                "error",
                f"E607 缺 keep.{k}（{label}）：三张方案要在同一组光与背景里对比，"
                "模型一旦自己改光，方案之间就没法比了",
            ))
    if not (spec.get("ref") or {}).get("path"):
        issues.append(("error", "E608 缺 ref.path：这个流程必须有参考图，没有参考图就退化成「画一个人」"))
    if frame.get("aspect") and frame["aspect"] not in ASPECTS:
        issues.append(("warn", f"W601 画幅 {frame['aspect']} 不在推荐值 {list(ASPECTS)} 里"))
    size = str(frame.get("size", ""))
    if size and frame.get("aspect") in ASPECTS and size != ASPECTS[frame["aspect"]]:
        issues.append(("warn", f"W602 size={size} 与 {frame['aspect']} 的推荐值 {ASPECTS[frame['aspect']]} 不一致"))

    # 款式是否在表里
    name = str(style.get("name", ""))
    if not name:
        issues.append(("error", "E609 缺 style.name"))
    elif cell and hair in m["hairtypes"]:
        if name not in cell["styles"].get(hair, []):
            issues.append((
                "warn",
                f"W603 「{name}」不在代码 {code} + 发质 {hair} 的推荐列表里（自选款）："
                "结构参数照你自己写的走，但先确认它不撞下面的发质黑名单",
            ))

    # 发质与结构打架
    ht = m["hairtypes"].get(hair, {})
    blob = " ".join(str(v) for v in struct.values())
    for w in ht.get("forbidden_words", []):
        if w in blob:
            issues.append(("warn", f"W604 结构里出现「{w}」，但发质「{ht.get('name')}」撑不住这种效果"))
    if hair == "A" and ("推光" in blob or "大背头" in name):
        issues.append(("warn", "W605 细软发推光或做大背头，发根支撑不住，出来一定贴头皮"))
    if hair == "B" and re.search(r"推光|推到耳|寸头", blob + name):
        issues.append(("warn", "W606 硬直发推光/寸头，发尾会全部立起来，头型会显大"))
    if hair == "C" and re.search(r"齐刘海|一刀切", blob + name):
        issues.append(("warn", "W607 自然卷剪齐刘海或一刀切，会炸成一个三角"))

    # 改形词
    text = json.dumps(spec, ensure_ascii=False)
    for w in RESHAPE:
        if w in text:
            issues.append(("warn", f"W608 spec 里出现改形表述「{w}」：只换头发的流程不允许改脸"))

    # 一次一个发型
    if re.search(r"[、/]|以及|和", name) and len(name) > 6:
        issues.append(("warn", f"W609 style.name「{name}」像是一次要了好几个发型：一次只换一个，方案数量靠多次生成"))

    return issues


# ------------------------------------------------------------------ 装配

def sec_task(spec: dict) -> str:
    return (
        "【任务】同一张照片，只改头发。\n"
        "参考图是这次生成的唯一来源：它是同一个人、同一束光、同一面背景、同一件衣服、同一个构图。"
        "要做的事情只有一件——把头发换成下面写明的发型，其它一切保持原样。\n"
        "判定标准：把结果和参考图并排放在一起，如果除了头发之外还有任何一处变了，这张就是废片。"
    )


def sec_identity(spec: dict) -> str:
    keep = spec.get("keep") or {}
    return (
        "【身份锁定｜最高优先级】\n"
        "画面里的人是参考图里那一个人，不是「照描述画一个像的人」：\n"
        "· 脸的比例：脸的长宽比、三庭比例、颧骨到下颌的宽度变化，与参考图一致；\n"
        "· 五官位置：眉眼间距、眼裂长度与走向、内外眼角高度差、鼻根高度、鼻翼宽度、"
        "人中长度、唇峰形状与唇厚比例，逐项对齐；\n"
        "· 细节特征：单双眼皮形态、眼袋与卧蚕的有无、痣与印记的位置，保留；\n"
        "· 肤色与肤质：同一个人、同一块皮肤，毛孔与细小的绒毛都在，不磨皮、不美白。\n"
        f"必须保持：{keep.get('identity', '脸型骨相、五官比例、肤色、年龄感')}。"
    )


def sec_hairline(spec: dict) -> str:
    keep = spec.get("keep") or {}
    return (
        "【发际线锁定｜换发型的第二道锁】\n"
        "发际线的高度与形状一个像素都不动：正中的最高点、两侧额角的圆弧、鬓角的起点、发旋的位置，"
        "全部与参考图一致。不允许把发际线整体下压来「显得发量多」，也不允许上移来「显得额头干净」——"
        "发际线一改，三庭比例就变了，观众看到的是另一个人。\n"
        "新发型是从这条既有的发际线上长出来的：能改的只有发际线以上的长度、方向、层次与体积，"
        f"以及鬓角当下的形状。必须保持：{keep.get('hairline', '发际线高度与形状、额角、鬓角起点、发旋')}。"
    )


def sec_style(spec: dict) -> str:
    style = spec.get("style") or {}
    struct = style.get("structure") or {}
    lines = [f"· {STRUCT_LABEL[k]}：{str(struct.get(k, '')).strip().rstrip('。')}" for k in STRUCT_KEYS]
    why = str(style.get("why", "")).strip()
    out = (
        f"【目标发型：{style.get('name', '')}】\n"
        + "\n".join(lines) + "\n"
        "这六项是硬指标，不能互相让步：分缝决定脸被切成几块，刘海决定额头露多少，"
        "两侧决定脸的宽度有没有被放大，后颈决定视觉终点停在哪，层次决定重量压在头顶还是发尾，"
        "体积决定轮廓是往上还是往两边走。"
    )
    if why:
        out += f"\n这张脸为什么要这么剪：{why}"
    return out


def sec_hairtype(spec: dict, m: dict) -> str:
    ht = m["hairtypes"][str(spec.get("hairtype", "A")).upper()]
    return (
        f"【发质呈现：{ht['name']}】\n"
        f"{ht['render']}\n"
        "发质写错，发型就塌：三种发丝的粗细、支撑力和卷度不一样，画成同一种质感，"
        "出来的就是「假发套」而不是「这个人的头发」。"
    )


def sec_keep(spec: dict) -> str:
    keep = spec.get("keep") or {}
    pairs = [
        ("光线", keep.get("light")),
        ("背景", keep.get("background")),
        ("构图", keep.get("frame")),
        ("表情", keep.get("expression")),
        ("衣物", keep.get("wardrobe")),
    ]
    lines = [f"· {k}：{v}" for k, v in pairs if v]
    return (
        "【保持不变｜这些一律不动】\n" + "\n".join(lines) + "\n"
        "光线尤其重要：头发的体积是靠光读出来的，光一变，同一个发型看起来就是两个发型，"
        "三张方案也就没法并排比较了。"
    )


def sec_texture() -> str:
    return (
        "【发丝质感】\n"
        "真实的头发是一束一束的，不是一整块：发根到发梢有粗细与方向的渐变，"
        "发束之间有明暗的间隙，外轮廓有少量飞散的碎发，发际线边缘能看到细小的绒毛，"
        "不是刀切一样的一条硬边。头发上的高光是一条断断续续的带子，不是一片亮面。"
    )


def sec_negative(negs: list[str]) -> str:
    return "【禁止项】" + "、".join(negs) + "。\n以上任何一项出现即视为废片。"


def assemble(spec: dict, m: dict) -> tuple[str, str, dict]:
    negs: list[str] = []
    for n in list(spec.get("negative_extra") or []) + DEFAULT_NEGATIVES:
        n = str(n).strip()
        if n and n not in negs:
            negs.append(n)

    parts = [
        sec_task(spec), sec_identity(spec), sec_hairline(spec), sec_style(spec),
        sec_hairtype(spec, m), sec_keep(spec), sec_texture(), sec_negative(negs),
    ]
    prompt = "\n\n".join(parts)

    issues = gate(spec, m)
    frame = spec.get("frame") or {}
    aspect = frame.get("aspect", "3:4")
    meta = {
        "code": spec.get("code"),
        "hairtype": spec.get("hairtype"),
        "style": (spec.get("style") or {}).get("name"),
        "aspect": aspect,
        "size": frame.get("size") or ASPECTS.get(aspect, "1536x2048"),
        "negative_count": len(negs),
        "imagegen": {
            "image1": (spec.get("ref") or {}).get("path", "<参考图绝对路径>"),
            "input_fidelity": "high",
            "revise": False,
            "footnote": "<不传：这个字段本身就是水印文本>",
            "quality": "high",
            "size": frame.get("size") or ASPECTS.get(aspect, "1536x2048"),
            "output_dir": "out/<session>/raw",
        },
        "issues": [
            {"level": lv, "code": msg.split(" ")[0], "message": msg} for lv, msg in issues
        ],
        "next_steps": [],
    }
    if any(i["level"] == "error" for i in meta["issues"]):
        meta["next_steps"].append("先修掉 error，再出图")
    else:
        meta["next_steps"].append("出一张正面；要判断层次，再加一张 45 度侧脸（同一个 spec，只改构图说明）")
        meta["next_steps"].append("多方案出完后跑 scripts/contact_sheet.py 拼对比图")
    return prompt, "\n".join(negs), meta


def main() -> int:
    ap = argparse.ArgumentParser(description="只换头发的提示词装配器")
    ap.add_argument("--spec", required=True, help="spec JSON 路径")
    ap.add_argument("--outdir", default="out", help="产物目录")
    ap.add_argument("--check", action="store_true", help="只校验，不写文件")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    spec_path = Path(args.spec)
    if not spec_path.exists():
        raise SystemExit(f"spec 不存在：{spec_path}")
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    m = load_matrix()

    prompt, negative, meta = assemble(spec, m)

    if not args.check:
        outdir = Path(args.outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "prompt.txt").write_text(prompt, encoding="utf-8")
        (outdir / "negative.txt").write_text(negative, encoding="utf-8")
        (outdir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    errs = [i for i in meta["issues"] if i["level"] == "error"]
    warns = [i for i in meta["issues"] if i["level"] == "warn"]
    print(f"spec: {spec_path.as_posix()}")
    print(f"代码 {meta['code']}  发质 {meta['hairtype']}  款式 {meta['style']}  画幅 {meta['aspect']} {meta['size']}")
    print(f"prompt: {len(prompt)} chars, {prompt.count('【')} 段；negative: {meta['negative_count']} 词")
    for i in errs:
        print(f"  [ERROR] {i['message']}")
    for i in warns:
        print(f"  [WARN]  {i['message']}")
    if not args.check:
        print(f"written: {(Path(args.outdir)).as_posix()}/prompt.txt, negative.txt, meta.json")
    return 1 if errs else 0


if __name__ == "__main__":
    raise SystemExit(main())
