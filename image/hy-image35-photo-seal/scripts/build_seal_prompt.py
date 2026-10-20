#!/usr/bin/env python3
"""照片 → 专属印章（篆刻钤印）提示词装配器。

输入一份 JSON spec，输出可直接喂给 Hy Image3.5 preview 的中文结构化提示词。
纯标准库实现，无第三方依赖。

用法：
    python build_seal_prompt.py --spec specs/photo-seal.json --outdir out/seal
    python build_seal_prompt.py --spec specs/photo-seal.json --check    # 只跑质量门
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# ---------------------------------------------------------------- 词表

SHAPE_LABEL = {"round": "圆形", "square": "方形"}

SHAPE_DESC = {
    "round": (
        "印面为正圆形，圆周连续闭合、圆度准确（不出现手绘式的抖动变形；"
        "局部的磕碰缺口另有规定，不算变形），圆心与画布中心重合"
    ),
    "square": (
        "印面为正方形，四边等长、四角略带极小的圆度（不是数学直角，也不是圆角卡片），"
        "边长约为画布宽度的五分之四，印面居中"
    ),
}

# 朱文 / 白文：篆刻最基础的一对概念，写错整个印面就反了
SEAL_TYPE = {
    "zhuwen": (
        "朱文（阳刻）",
        "文字与图案在石面上凸起，钤印后线条与图案呈印泥色、底为纸色。"
        "整体红少留白多，线条是画面主体，笔画之间留出通透的空白，忌讳笔画挤成一团。",
    ),
    "baiwen": (
        "白文（阴刻）",
        "文字与图案在石面上凹陷，钤印后底为大面积印泥色、笔画与图案呈纸色。"
        "整体红多留白少，白线由两侧的红底围出，线条边缘必须干净，忌讳红底里出现灰阶过渡。",
    ),
}

BORDER_DESC = {
    "thin": "细边朱文框：边框宽度约为笔画宽度的二分之一，四边粗细略有变化，不是机械等宽的圆环或方框。",
    "thick": "粗边宽框：边框宽度约为笔画宽度的一点五倍，边线与中间的图案之间留出明显的红色或纸白间隙。",
    "broken": "残边：边框不闭合，有一到两处断裂，或边框与图案直接相接形成借边，断口处呈自然崩缺状。",
    "none": "无边：不画任何外框，印面形状完全由图案与文字的自身轮廓围合，靠印泥外溢的气息收边。",
}

KNIFE = (
    "刀法取冲刀与切刀并用：长线用冲刀，线条一侧光洁、另一侧带细微毛边，有行刀的爽利速度感；"
    "短笔与转折用切刀，笔画呈一节一节的方折顿挫，末端可见米粒大小的崩口；"
    "线条粗细有呼吸变化，起笔略重、收笔略提，不是等宽的矢量描边；"
    "所有笔画内部保留细碎的漏红与印泥颗粒，像石面凹凸吃墨不匀，呈现手工刻石的层次。"
)

TEXT_POS = {
    "right_column": "印面右侧的一列竖向留白",
    "left_column": "印面左侧的一列竖向留白",
    "top": "印面上方的横向留白",
    "bottom": "印面下方的横向留白",
    "around": "图案四周的回文式留白",
}

PLACEHOLDER_HINTS = (
    "照片主题", "待定", "主题", "xxx", "XXX", "文字", "刻字", "自定", "...", "……", "？", "?",
)

# 篆刻用字的高频简化字 → 繁体（只收常见字，用于提示「你填的是简体」）
SIMPLIFIED_HINTS = {
    "风": "風", "云": "雲", "听": "聽", "龙": "龍", "华": "華", "国": "國", "门": "門",
    "见": "見", "东": "東", "长": "長", "万": "萬", "与": "與", "无": "無", "处": "處",
    "归": "歸", "来": "來", "时": "時", "书": "書", "画": "畫", "兰": "蘭", "岁": "歲",
    "梦": "夢", "惊": "驚", "鸟": "鳥", "马": "馬", "鱼": "魚", "车": "車", "爱": "愛",
    "欢": "歡", "旧": "舊", "远": "遠", "边": "邊", "声": "聲", "气": "氣", "独": "獨",
    "静": "靜", "间": "間", "问": "問", "闻": "聞", "阴": "陰", "阳": "陽", "岭": "嶺",
    "泉": "泉", "岩": "巖", "树": "樹", "闲": "閒", "游": "遊", "踪": "蹤", "客": "客",
}

# 印泥色彩档案：名字 → (hex, 一句色彩描述)
INK_PROFILES = {
    "朱砂红": ("#B23A2E", "偏暖的正红，明度中等偏暗，像陈年朱砂，不是鲜亮的电商红"),
    "朱膘": ("#C4553A", "偏橘的红，暖调更重，像旧年画上的红"),
    "仿古": ("#8C3B2B", "偏褐的暗红，带陈旧的土味，明度低"),
    "西洋红": ("#A02B3C", "偏洋红的深红，冷调，近现代的印泥色"),
    "墨": ("#2B2622", "近乎黑的墨色，带极弱的暖褐，像老墨锭磨出的墨"),
}

PAPER_PROFILES = {
    "宣纸": ("#F2E8D5", "米黄宣纸，可见细密纤维与极浅的褶皱起伏"),
    "净白": ("#FBFBF9", "净白的书画纸，接近纸白，几乎无黄调"),
    "古纸": ("#E7D9BC", "泛黄的老纸，颜色更沉，有轻微的年代感"),
}

FORBIDDEN_BASE = [
    "照片滤镜感", "照片直接套圆框", "写实照片质感", "连续调灰阶",
    "彩色", "多色印泥", "渐变", "发光", "半调网点",
    "立体浮雕", "投影", "阴影", "桌面", "背景杂物", "纸角卷曲",
    "水印", "签名", "logo", "英文", "现代印刷字体", "黑体字形", "等宽矢量线条",
    "镜像反转的字", "错字", "缺字", "无法辨认的纹样",
]


# ---------------------------------------------------------------- 色彩工具

def hex_to_rgb(value: str) -> tuple[int, int, int]:
    v = value.strip().lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    if len(v) != 6:
        raise ValueError(f"色值格式不对：{value}")
    return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    def lin(c: float) -> float:
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def hsv_of(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    r, g, b = (c / 255.0 for c in rgb)
    mx, mn = max(r, g, b), min(r, g, b)
    v = mx
    s = 0.0 if mx == 0 else (mx - mn) / mx
    return s, v, v


# ---------------------------------------------------------------- 各段装配

def resolve_ink(ink: dict | None) -> tuple[str, tuple[int, int, int], list[str]]:
    warns: list[str] = []
    ink = ink or {}
    name = str(ink.get("color", "朱砂红")).strip() or "朱砂红"
    hexv = str(ink.get("hex", "")).strip()

    base_hex, base_desc = INK_PROFILES.get(name, (None, ""))
    if not hexv:
        if base_hex is None:
            warns.append(f"W301 印泥色「{name}」不在内置档案里，也未给 hex，按朱砂红处理")
            hexv = INK_PROFILES["朱砂红"][0]
        else:
            hexv = base_hex

    rgb = hex_to_rgb(hexv)
    s, v, _ = hsv_of(rgb)
    if s > 0.85 and v > 0.75:
        warns.append(
            f"W303 印泥色 {hexv} 过亮过艳（S={s:.2f} V={v:.2f}），会是电商红而不是印泥红；"
            "朱砂类印泥的明度一般落在 V≈0.55–0.72"
        )

    desc = base_desc or "沉稳的印泥色"
    text = (
        f"印泥色为{name}（{hexv}）：{desc}。"
        "印泥浓淡不匀，笔画中心色厚、边缘色薄，局部有极细的颗粒与堆叠感；"
        "全图是单色印刷，除印泥色与纸色之外不出现第三种色相，"
        "不出现紫、橙、绿等邻近色偏移。"
    )
    return text, rgb, warns


def resolve_paper(paper: dict | None) -> tuple[str, tuple[int, int, int], list[str]]:
    paper = paper or {}
    name = str(paper.get("color", "宣纸")).strip() or "宣纸"
    hexv = str(paper.get("hex", "")).strip()
    base_hex, base_desc = PAPER_PROFILES.get(name, (None, ""))
    if not hexv:
        hexv = base_hex or PAPER_PROFILES["宣纸"][0]
    desc = base_desc or "洁净的纸面"
    text = (
        f"纸面为{name}（{hexv}）：{desc}；"
        "纸面干净，无污渍、无水痕、无阴影、无桌面、无钉孔；"
        "整张纸平铺正对镜头，没有透视与倾斜，纸表就是画面的全部背景。"
    )
    return text, hex_to_rgb(hexv), []


def subject_block(subject: dict | None) -> tuple[str, list[str]]:
    warns: list[str] = []
    subject = subject or {}
    keep = [str(x).strip() for x in (subject.get("keep") or []) if str(x).strip()]
    drop = [str(x).strip() for x in (subject.get("drop") or []) if str(x).strip()]
    style = str(subject.get("style", "")).strip()

    if not keep:
        raise ValueError("E101 subject.keep 为空：必须写明从照片里提取哪些核心元素（人物/建筑/植物/器物）")

    text = (
        "从参考照片中提取并重构以下核心元素，作为印面图案："
        + "、".join(keep)
        + "。提取是翻译而不是描边：把这些元素压缩成篆刻语言——"
        "用粗重的块面与短促的方折线概括形体，去掉光影、材质、透视与灰阶层次，"
        "只保留能让人一眼认出该元素的最少笔画；元素之间的主次关系要保留（主体大、次体小）。"
    )
    if style:
        text += f"具体处理：{style}。"

    if drop:
        text += "照片中其余内容全部舍弃（" + "；".join(drop) + "），印面上不出现照片原貌。"
    else:
        text += "照片中未被列出的内容一律舍弃，印面上不出现照片原貌。"
        warns.append("W302 没写 subject.drop：建议列出要丢掉的细节（天空、地面、衣纹、远景等），否则模型容易照抄整张照片")

    if len(keep) > 4:
        warns.append(f"W305 提取了 {len(keep)} 个元素，印面方寸容易碎；建议压到 1–3 个主体")
    if any(k in ("人", "人物", "人脸", "人像") for k in keep):
        warns.append("W306 人物入印需要高度概括（侧影/剪影/半身轮廓），若照片有正脸细节要求，说明会与印章语言冲突")
    return text, warns


def text_block(theme: str, theme_seal: str, layout: dict) -> tuple[str, list[str]]:
    warns: list[str] = []
    text = theme_seal or theme
    n = len(text)

    pos = str(layout.get("text_position", "right_column")).strip()
    if pos not in TEXT_POS:
        warns.append(f"W307 text_position「{pos}」不认识，按右侧竖列处理")
        pos = "right_column"

    cols = layout.get("text_columns")
    if cols:
        cols = int(cols)
    else:
        cols = 1 if n <= 4 else 2 if n <= 8 else 3

    if n > 6:
        warns.append(
            f"E-W401 刻字「{text}」共 {n} 字：图像模型渲染 6 字以上的汉字几乎必出错字/缺笔。"
            "建议压到 2–4 字，或改用 sealcut.py 的无字底 + stamp_text.py 叠字路线"
        )
    elif n > 4:
        warns.append(f"W402 刻字「{text}」共 {n} 字，出图后必须逐字核对，错一字就要重出")

    if not theme_seal:
        simp = [(c, SIMPLIFIED_HINTS[c]) for c in theme if c in SIMPLIFIED_HINTS]
        if simp:
            pairs = "、".join(f"「{a}」→「{b}」" for a, b in simp)
            warns.append(
                f"W403 篆刻惯例用繁体/本字：请把 spec 的 theme_seal 填成 {pairs}，"
                "或确认要用简体入印"
            )

    labeled = "、".join(f"第{i + 1}字「{c}」" for i, c in enumerate(text))
    text_block_str = (
        f"印面留白处刻「{text}」共 {n} 个字：{labeled}。"
        f"文字位于{TEXT_POS[pos]}，竖排、自上而下、字组自右向左、共 {cols} 列，字与字上下间距均匀。"
        "字形取汉印缪篆意：横平竖直、笔画粗细均匀、转折方硬、结构方整饱满，字内留出通透的空隙；"
        f"这 {n} 个字必须完整可辨、笔画正确，不得缺笔、不得多笔、不得造字、不得镜像反转、"
        "不得简化成无法辨认的装饰纹样。"
        "字口边缘有轻微崩口与印泥堆积，不追求印刷级光洁。"
        "文字只出现在留白区，不得压在图案上，也不得越出印面边界。"
    )
    return text_block_str, warns


def wear_block(wear: dict | None) -> tuple[str, list[str]]:
    wear = wear or {}
    parts: list[str] = []

    if wear.get("chip", True):
        parts.append("印面边缘有一到两处不规则的磕碰缺角，缺口位置不对称，缺口内可见印泥堆积")
    if wear.get("bleed", True):
        parts.append("印泥在印面边缘外侧渗出极淡的一圈晕边，像水分沿纸纤维洇开，宽约一到两毫米")
    pressure = str(wear.get("pressure", "uneven")).strip()
    if pressure == "uneven":
        parts.append("盖印时压力不均：一侧线条清晰锐利，对侧略淡且局部断续，形成自然的轻重过渡")
    elif pressure == "heavy":
        parts.append("盖印压力偏重：整体印泥厚实饱满，笔画边缘有轻微压溢")
    elif pressure == "light":
        parts.append("盖印压力偏轻：整体印泥略薄，部分笔画可见纸纹透出的白点")
    if wear.get("abrasion", False):
        parts.append("印面有一次被轻擦过的痕迹：少量笔画被擦淡、出现断续，但字与图案主体仍然完整可辨")

    if not parts:
        parts.append("印面边缘干净，仅保留极轻微的印泥不均")

    text = "磨损处理：" + "；".join(parts) + "。磨损是不对称、不均匀的局部现象，不是沿整圈等距铺开的噪点。"
    return text, []


def layout_block(layout: dict | None, shape: str, aspect: str,
                 seal_type: str = "zhuwen") -> tuple[str, list[str]]:
    warns: list[str] = []
    layout = layout or {}
    ratio = float(layout.get("pattern_ratio", 0.62))
    ratio = min(0.85, max(0.35, ratio))
    text_ratio = round(1 - ratio, 2)

    pos = str(layout.get("text_position", "right_column")).strip()
    pos_cn = TEXT_POS.get(pos, TEXT_POS["right_column"])
    pattern_side = {
        "right_column": "印面左侧",
        "left_column": "印面右侧",
        "top": "印面中下部",
        "bottom": "印面中上部",
        "around": "印面中央",
    }.get(pos, "印面左侧")

    # 刻字区的"空"对朱文和白文是两种东西，写混了模型会画错
    if seal_type == "baiwen":
        blank = "刻字区是一整块干净的印泥色，字以纸色从中空出，字周围必须留足红底"
    else:
        blank = "刻字区是干净的留白纸面，字以印泥色刻在上面，字与字之间透气"

    text = (
        f"正方形画布，{SHAPE_DESC[shape]}。"
        "纸面四周留出干净的纸边，约占画面宽度的 8% 到 12%，"
        "印面完整居中，四条边都不触碰画布边缘，不出现出血裁切。"
        f"印面内部分成两个区域：图案区占印面约 {round(ratio * 100)}%，置于{pattern_side}；"
        f"刻字区占约 {round(text_ratio * 100)}%，为{pos_cn}。"
        f"{blank}；图案不得侵入刻字区，两个区域之间有一道清晰的间隙把呼吸感分开。"
    )
    if ratio > 0.78:
        warns.append(f"W304 pattern_ratio={ratio} 偏高：留白不足，刻字会挤在边缘，建议 0.55–0.72")
    if aspect not in ("1:1", "1:1 "):
        warns.append(f"W308 画幅填的是 {aspect}：印章必须在正方形画布上出图，否则圆形会被压成椭圆")
    return text, warns


# ---------------------------------------------------------------- 主装配

def build(spec: dict) -> tuple[str, list[tuple[str, str]]]:
    """返回 (提示词全文, [(级别, 消息), ...])"""
    issues: list[tuple[str, str]] = []

    theme = str(spec.get("theme", "")).strip()
    theme_seal = str(spec.get("theme_seal", "")).strip()

    if not theme and not theme_seal:
        raise ValueError("E102 theme 为空：必须给出刻字内容（照片主题），如「昆仑问道」")
    raw_probe = (theme + theme_seal).strip("《》\"'「」 ")
    if any(h in raw_probe for h in PLACEHOLDER_HINTS) or len(raw_probe) <= 1:
        raise ValueError(
            f"E103 刻字内容「{raw_probe}」是占位符或过短：请把「[照片主题]」替换成真实主题，"
            "如「昆仑问道」「江南春」「听松」"
        )
    if any(ch in raw_probe for ch in "[]【】{}<>"):
        raise ValueError(f"E103 刻字内容「{raw_probe}」仍含占位符括号，请填真实文字")

    shape = str(spec.get("shape", "round")).strip().lower()
    if shape not in SHAPE_LABEL:
        raise ValueError(f"E104 shape「{shape}」不支持：只接受 round（圆形）或 square（方形）")

    seal_type = str(spec.get("seal_type", "zhuwen")).strip().lower()
    if seal_type not in SEAL_TYPE:
        raise ValueError(f"E105 seal_type「{seal_type}」不支持：只接受 zhuwen（朱文/阳刻）或 baiwen（白文/阴刻）")
    type_name, type_desc = SEAL_TYPE[seal_type]

    border = str(spec.get("border", "thin")).strip().lower()
    if border not in BORDER_DESC:
        issues.append(("warn", f"W309 border「{border}」不认识，按细边框处理"))
        border = "thin"

    ink_text, ink_rgb, w = resolve_ink(spec.get("ink"))
    issues += [("warn", m) for m in w]
    paper_text, paper_rgb, _ = resolve_paper(spec.get("paper"))

    cr = contrast_ratio(ink_rgb, paper_rgb)
    if cr < 3.2:
        issues.append((
            "warn",
            f"W310 印泥与纸面的对比度只有 {cr:.1f}:1，钤印会发虚；"
            "换更深的印泥色或更亮的纸（建议对比度 ≥ 4:1）",
        ))

    subj_text, w = subject_block(spec.get("subject"))
    issues += [("warn", m) for m in w]

    aspect = str(spec.get("aspect", "1:1"))
    layout_text, w = layout_block(spec.get("layout"), shape, aspect, seal_type)
    issues += [("warn", m) for m in w]

    txt_text, w = text_block(theme, theme_seal, spec.get("layout") or {})
    for m in w:
        issues.append(("error", m.replace("E-W", "E")) if m.startswith("E-W") else ("warn", m))

    wear_text, _ = wear_block(spec.get("wear"))

    if seal_type == "baiwen" and float((spec.get("layout") or {}).get("pattern_ratio", 0.62)) > 0.75:
        issues.append(("warn", "W311 白文 + 图案占比过高：红底会连成一片，白线被挤没；白文建议 pattern_ratio ≤ 0.70"))
    if border == "broken" and seal_type == "zhuwen":
        issues.append(("warn", "W312 残边 + 朱文细线：断边容易让细线也跟着断，出图后重点看边框是否还连成一周"))

    quality = (
        "高分辨率精细印面：每一根线条、每一处崩口、每一颗印泥颗粒、每一丝纸纤维都清晰可辨；"
        "全画面锐利对焦，没有景深虚化、没有动态模糊、没有镜头暗角；"
        "正方形构图，比例严格为一比一。"
    )

    forbidden = FORBIDDEN_BASE + [str(f).strip() for f in (spec.get("forbidden") or []) if str(f).strip()]
    seen: set[str] = set()
    forbidden = [f for f in forbidden if not (f in seen or seen.add(f))]

    blocks = [
        "【任务】把参考照片转化成一枚专属印章（篆刻钤印的印迹效果）。"
        "不是给照片套一个圆形或方形边框，也不是把照片整体染红，"
        "而是用篆刻的语言把照片的核心元素重新画一遍。"
        "最终画面是一枚盖在纸上的印：只有印泥色与纸色两种颜色，没有第三种颜色、没有灰阶过渡。",
        "【元素提取】" + subj_text,
        "【双色化规则】照片是连续调，印章只有两色：所有中间灰阶必须被消掉，"
        "要么成为印泥色、要么留成纸白；不出现渐变、不出现半调网点、不出现描边式的细线稿，"
        "也不出现照片阴影投在纸上的效果。图案的疏密靠留白调节，不靠明暗。",
        "【印章类型】" + type_name + "：" + type_desc,
        "【边框】" + BORDER_DESC[border],
        "【刀法】" + KNIFE,
        "【刻字】" + txt_text,
        "【边缘磨损】" + wear_text,
        "【印泥】" + ink_text,
        "【纸面】" + paper_text,
        "【构图】" + layout_text,
        "【画质】" + quality,
        "【禁止】" + "、".join(forbidden) + "。",
    ]

    return "\n".join(blocks), issues


def main() -> int:
    ap = argparse.ArgumentParser(description="照片 → 专属印章 提示词装配器")
    ap.add_argument("--spec", required=True, help="spec JSON 路径")
    ap.add_argument("--outdir", help="输出目录（写 prompt.txt / meta.json）")
    ap.add_argument("--check", action="store_true", help="只跑质量门并打印提示词，不写文件")
    args = ap.parse_args()

    spec_path = Path(args.spec)
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"[error] spec 不存在：{spec_path}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"[error] spec 不是合法 JSON：{exc}", file=sys.stderr)
        return 2

    try:
        prompt, issues = build(spec)
    except ValueError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1

    for level, msg in issues:
        print(f"[{level}] {msg}")

    errors = [m for lv, m in issues if lv == "error"]
    print(prompt)

    if args.check:
        if errors:
            print(f"\n[gate] 未通过：{len(errors)} 处硬错误", file=sys.stderr)
        return 1 if errors else 0

    if errors:
        print(f"\n[gate] 有 {len(errors)} 处硬错误，仍写出文件供参考，但不要直接拿去出图", file=sys.stderr)

    if args.outdir:
        outdir = Path(args.outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "prompt.txt").write_text(prompt, encoding="utf-8")
        meta = {
            "spec": str(spec_path),
            "theme": spec.get("theme"),
            "theme_seal": spec.get("theme_seal"),
            "shape": spec.get("shape", "round"),
            "seal_type": spec.get("seal_type", "zhuwen"),
            "aspect": spec.get("aspect", "1:1"),
            "size": spec.get("size", "2048x2048"),
            "ref": spec.get("ref"),
            "gate": {"errors": len(errors), "issues": [f"[{lv}] {m}" for lv, m in issues]},
        }
        (outdir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n[write] {outdir / 'prompt.txt'}（{len(prompt)} 字）")
        print(f"[write] {outdir / 'meta.json'}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
