#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""经典纪实摄影调色风格（国家地理式）提示词装配器。

输入一份 JSON spec，输出可直接喂给 Hy Image3.5 preview 的中文结构化提示词。
纯标准库实现，无第三方依赖。

设计要点：
  1. 风格锚点只用品牌名是危险的——模型对「国家地理」的先验包含黄色矩形边框、
     白色无衬线字标、广角畸变。所以品牌名会被翻译成可执行的光色条款，
     并把边框/logo/字标自动写进禁止项。
  2. 色彩不给形容词，给 HSV 锚点。「通透的蓝天」在模型里可以是一百种蓝，
     但 hue 205–215 / S 0.55–0.80 / V 0.55–0.75 只有一种。
  3. 影调顺序写死：先保两端动态范围，再压中间调对比。反过来就是 HDR 假。

用法：
    python build_ng_prompt.py --spec specs/natgeo-ganzi.json --outdir out/demo
    python build_ng_prompt.py --spec specs/natgeo-ganzi.json --check   # 只跑质量门
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------- 预设档案

# 三种常见题材的默认色彩取向。spec 里的 palette 只需覆盖差异项。
PRESETS = {
    "nature": {
        "label": "自然风光",
        "sat_bias": "饱和度适中偏高，风光色块浓郁饱满，纯度扎实不透明，允许局部到达饱和上限但不溢出",
        "contrast": "中高对比：中间调斜率明显、明暗分界干净，但两端都有余地",
        "shadow_weight": "暗部占比中等，阴影里保留地形与材质的层次",
        "people_hint": "画面以自然为主体，人可以很小、只作为尺度参照，不要抢占视觉重心",
    },
    "street": {
        "label": "人文扫街",
        "sat_bias": "饱和度适中，比风光克制一档：环境色压住、人物与服饰色略提，靠明暗关系而不是靠艳色抓眼",
        "contrast": "高对比：光斑与阴影并置，局部强烈、整体平衡",
        "shadow_weight": "暗部占比高且深，但深而不死——阴影里要看得出墙面、衣褶、地砖的走向",
        "people_hint": "人是画面的锚点，姿态自然、不看镜头，与环境的互动比表情更重要",
    },
    "travel": {
        "label": "旅行纪实",
        "sat_bias": "饱和度适中偏高：天、草、土、水的本色还原准确，饱和度抬高但不改变色相，没有滤镜式的偏色",
        "contrast": "中高对比，光影层次立体分明，近中远三层各自成立",
        "shadow_weight": "暗部深邃干净，有细节；高光通透柔和，有细节",
        "people_hint": "人与环境同等重要：人在场景里做事（行走、劳作、眺望），不是摆拍",
    },
}

# 品牌名 → 可执行的风格条款。命中即替换，避免模型去画边框。
STYLE_ANCHORS = {
    "natgeo": (
        "经典纪实摄影的调子（非杂志版式）：色彩还原准确、饱和度适中偏高，"
        "原色通透扎实、真实耐看，没有任何滤镜式的偏色与虚假感；"
        "对比度硬朗而高级，光影层次立体分明；风光色彩浓郁饱满——蓝天通透、绿植鲜活、大地色调醇厚复古；"
        "人物纪实肤色自然真实、质感立体；暗部细节丰富、深邃干净，高光通透柔和、细节保留完整，"
        "全画面动态范围充足、容错度高；整体复古大气、纪实感浓厚、视觉冲击力强，"
        "兼具自然治愈与人文厚重"
    ),
    "kodachrome": (
        "早期彩色反转片的调子：红黄扎实、蓝偏青、绿色收敛不发荧光，"
        "暗部略沉但有层次，高光干净不发黄，整体有一点年代感但不做旧"
    ),
    "documentary": (
        "纯纪实摄影的调子：以还原现场为准，不做风格化调色，饱和度自然、色温忠于现场光，"
        "靠光线与构图而不是靠色彩取胜"
    ),
}

# 命中即视为「用户用了品牌名当风格锚」
BRAND_PATTERNS = {
    "national geographic": "natgeo",
    "nationalgeographic": "natgeo",
    "natgeo": "natgeo",
    "国家地理": "natgeo",
    "国家地理杂志": "natgeo",
    "柯达克罗姆": "kodachrome",
    "kodachrome": "kodachrome",
    "纪实摄影": "documentary",
}

# 会毁掉这个调子的词。出现在 scene/subject/palette/notes 里就拦。
HIGH_RISK_WORDS = {
    "hdr": "HDR 的默认效果是局部提亮 + 灰雾，正是「虚假感」的来源",
    "去雾": "去雾会把空气透视一起抹掉，远景失去纵深",
    "暗角": "暗角是短视频滤镜的标志，纪实摄影不做",
    "vignette": "暗角是短视频滤镜的标志，纪实摄影不做",
    "霓虹": "霓虹色会把这个调子拉成赛博朋克",
    "赛博": "赛博朋克与本风格的光色逻辑完全相反",
    "橙青": "橙青分离是电影海报的商业调色，会把真实肤色烧成橘色",
    "teal and orange": "橙青分离是电影海报的商业调色，会把真实肤色烧成橘色",
    "动漫": "动漫化会丢掉全部材质与光学真实感",
    "插画": "插画化会丢掉全部材质与光学真实感",
    "3d渲染": "3D 渲染感的塑料高光是本风格的头号反面",
    "油画": "油画笔触会毁掉照片的纹理",
    "磨皮": "磨皮会抹掉皮肤质感，纪实人像恰恰要保留毛孔与皱纹",
    "塑料": "塑料感是材质真实度的硬伤",
    "过度锐化": "过度锐化会产生白边光晕，暗部细节看起来像噪点",
}

# 分色锚点：元素 → (色相区间°, 一句描述)。色相用 0–360 表述，脚本转成提示词文字。
HUE_ANCHORS = {
    "天空": (200, 218, "天顶深青蓝、向地平线逐渐变浅并略降饱和，云的白是干净的暖白，云底有淡淡的蓝灰"),
    "植被": (85, 125, "鲜活的黄绿到深绿，受光面偏黄绿、背光面偏冷绿，深绿不发黑、不发荧光"),
    "大地": (20, 42, "醇厚的红棕到土黄，明度中等偏低，带一点复古的暖，不是橙也不是灰"),
    "水体": (185, 210, "深水偏青蓝且通透见底，浅水与浪花偏中性白，水面的反光是碎的高光而不是一片白"),
    "岩石": (25, 45, "中性偏暖的灰褐，有冷暖变化，受光面暖、背光面偏冷，不是单一灰色"),
    "雪线": (200, 220, "雪不是纯白：受光面暖白、阴影面淡蓝，有细微的蓝紫过渡"),
    "建筑": (18, 45, "风化的暖色墙面与冷色阴影并存，砖石有各自的固有色，不做统一染色"),
}

# 允许的画幅 → 推荐出图尺寸（Hy Image3.5 单边上限 4096）
ASPECT_SIZES = {
    "3:2": "2048x1365",
    "2:3": "1365x2048",
    "4:5": "1638x2048",
    "16:9": "2048x1152",
    "1:1": "2048x2048",
    "21:9": "2048x878",
}

PLACEHOLDER_HINTS = (
    "参考剧集", "参考", "待定", "某地", "xxx", "XXX", "待填", "自定", "示例",
    "...", "……", "?", "？", "[", "]", "【", "】",
)

# 正午硬光：纪实风光在这个时段几乎必翻车（顶光、短影、发灰）
HARSH_NOON = ("正午", "中午", "12:00", "12：00", "13:00", "顶光", "烈日当空")


# ---------------------------------------------------------------- 工具

def aspect_ratio(aspect: str) -> float | None:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*[:：/]\s*(\d+(?:\.\d+)?)\s*", aspect or "")
    if not m:
        return None
    w, h = float(m.group(1)), float(m.group(2))
    return w / h if h else None


def size_ratio(size: str) -> float | None:
    m = re.fullmatch(r"\s*(\d+)\s*[x×]\s*(\d+)\s*", size or "")
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    return w / h if h else None


def detect_style(raw: str) -> tuple[str | None, str]:
    """从用户写的风格锚里认出是哪一种，返回 (key, 命中的原文)。"""
    low = (raw or "").lower()
    for pat, key in BRAND_PATTERNS.items():
        if pat in low:
            return key, pat
    return None, ""


def scan_high_risk(*texts: str) -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    blob = " ".join(t or "" for t in texts).lower()
    for word, reason in HIGH_RISK_WORDS.items():
        if word in blob:
            hits.append((word, reason))
    return hits


def has_placeholder(text: str) -> str | None:
    for h in PLACEHOLDER_HINTS:
        if h in (text or ""):
            return h
    return None


def dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for i in items:
        i = str(i).strip()
        if i and i not in seen:
            seen.add(i)
            out.append(i)
    return out


# ---------------------------------------------------------------- 各段装配

def style_block(spec: dict) -> tuple[str, list[tuple[str, str]], bool]:
    issues: list[tuple[str, str]] = []
    anchor = spec.get("style_anchor") or {}
    raw = str(anchor.get("raw", "")).strip()
    mapped = str(anchor.get("mapped", "")).strip()
    key, hit = detect_style(raw)

    brand_used = False
    text = ""
    if key:
        brand_used = True
        text = STYLE_ANCHORS[key]
        if mapped:
            text += "；" + mapped
        issues.append((
            "info",
            f"风格锚点「{hit}」已翻译成光色条款：品牌名只写进提示词会让模型去画边框和字标，"
            "所以提示词里只说「经典纪实摄影的调子」，不说杂志名",
        ))
    elif mapped:
        text = mapped
        issues.append(("warn", "W301 style_anchor.mapped 是自定义描述：确认里面没有暗角/HDR/橙青这类反向词"))
    else:
        text = STYLE_ANCHORS["natgeo"]
        issues.append(("warn", "W302 没写 style_anchor，按经典纪实摄影默认调子处理"))

    if brand_used:
        # 让「没有边框」进到提示词正文，而不只是禁止项
        text += "。画面是纯粹的摄影作品，没有杂志版式：不加边框、不加白边、不加黄色矩形、不加色块、不加任何字标"
    return text, issues, brand_used


def scene_block(spec: dict) -> tuple[str, list[tuple[str, str]]]:
    issues: list[tuple[str, str]] = []
    region = str(spec.get("region", "")).strip()
    scene = str(spec.get("scene", "")).strip()
    subject = str(spec.get("subject", "")).strip()
    season = str(spec.get("season", "")).strip()
    weather = str(spec.get("weather", "")).strip()

    if not scene:
        raise ValueError("E301 scene 为空：必须写清画面里发生了什么（地点、天气、主体在哪、在做什么）")

    for field, val in (("region", region), ("scene", scene), ("subject", subject)):
        ph = has_placeholder(val)
        if ph:
            raise ValueError(
                f"E302 {field} 里还有占位符「{ph}」：{val[:40]}… "
                "纪实摄影依赖真实的地点与现场细节，占位符会让模型编一座「名山大川」出来。"
                "请换成具体地名与可拍到的场景"
            )

    parts = []
    if region:
        parts.append(f"地点：{region.rstrip('。')}。")
    parts.append(f"场景：{scene.rstrip('。')}。")
    if season:
        parts.append(f"季节：{season.rstrip('。')}。")
    if weather:
        parts.append(f"天气：{weather.rstrip('。')}。")
    if subject:
        parts.append(f"画面主体：{subject.rstrip('。')}。")

    text = "".join(parts) + (
        "以上是现场记录，不是概念设计：地形、植被、建筑、器物都必须是真实可存在的样子，"
        "细节服从地理与季节常识（海拔对应植被带、纬度对应光照角度、湿度对应空气通透度）。"
    )
    return text, issues


def people_block(spec: dict, preset: dict) -> tuple[str, list[tuple[str, str]]]:
    issues: list[tuple[str, str]] = []
    people = spec.get("people") or {}
    count = people.get("count")
    desc = str(people.get("desc", "")).strip()
    skin = str(people.get("skin", "")).strip()

    n = 0
    if isinstance(count, int):
        n = count
    elif isinstance(count, str) and count.isdigit():
        n = int(count)

    if not desc and not n:
        text = (
            "画面中没有人；若场景逻辑上应有人（集市、村道、码头），可以出现极小的、"
            "侧背向的、被环境淹没的人影，不出现清晰正脸"
        )
        return text, issues

    text = f"主体人物：{desc or '现场居民或旅人'}"
    if n:
        text += f"；画面里共 {n} 人"
    text += (
        "。人物的存在方式是「在生活里」而不是「在拍照」：不看镜头、不摆姿势、"
        "动作被环境打断（正在弯腰、正在卸货、正在回头），身体有一部分被环境遮挡，"
        "人在画面里占的面积不超过三分之一。"
    )
    text += (
        "肤色必须自然真实：受光面是暖调的健康肤色（色相 20°–32°、饱和度 0.28–0.50、明度 0.45–0.72），"
        "暗面是同一个肤色的加深而不是变红或变绿，高光是皮肤的反光而不是白斑；"
        "皮肤保留毛孔、汗光、风吹的糙感、额头的纹路，不做磨皮、不做统一的「奶油肌」。"
    )
    if skin:
        text += f"个体特征：{skin}。"
    if n > 5:
        issues.append((
            "warn",
            f"W303 写了 {n} 个人：人物多时模型容易把每张脸都画糊，也容易排成整齐的一排。"
            "建议压到 1–3 个清晰主体，其余写成「人群中模糊的背影」",
        ))
    return text, issues


def camera_block(spec: dict) -> tuple[str, list[tuple[str, str]]]:
    issues: list[tuple[str, str]] = []
    cam = spec.get("camera") or {}
    lens = str(cam.get("lens", "35mm")).strip()
    height = str(cam.get("height", "视平线，约与主体胸口平齐")).strip()
    angle = str(cam.get("angle", "平视")).strip()
    place = str(cam.get("placement", "")).strip()
    layers = cam.get("layers") or []

    text = (
        f"镜头：等效 {lens} 全画幅定焦；视角：{angle}；机位高度：{height}"
        + (f"；机位：{place}" if place else "")
        + "。纪实摄影的镜头习惯是「靠近了拍」而不是「拉长了拍」："
        "透视自然、边缘不畸变、四角不拉伸，不用鱼眼也不用超长焦压缩空间。"
    )

    if layers:
        layers = [str(x).strip() for x in layers if str(x).strip()]
        text += "画面分三层，每层都要有独立的内容与清晰度："
        text += "；".join(f"第{i + 1}层——{v}" for i, v in enumerate(layers)) + "。"
        text += (
            "层次靠空气透视分开（远景对比度降低、略偏冷偏灰），不靠虚化硬切；"
            "纪实风光用小光圈让远景也在焦内，纪实人像才允许主体与环境适度分离。"
        )
    else:
        issues.append((
            "warn",
            "W304 camera.layers 为空：没有前后景的电影感会变成「一张平片」。"
            "建议补上 前景/中景/远景 各一句",
        ))

    return text, issues


def light_block(spec: dict) -> tuple[str, list[tuple[str, str]]]:
    issues: list[tuple[str, str]] = []
    light = spec.get("light") or {}
    time_ = str(light.get("time", "日出后 1–2 小时")).strip()
    quality = str(light.get("quality", "定向的硬光，带一点柔和的环境反射")).strip()
    direction = str(light.get("direction", "侧逆光，光源在画面右后方画外")).strip()
    ratio = str(light.get("ratio", "受光面与背光面约 1:3，暗部由环境反光托起而不是靠补光")).strip()

    text = (
        f"光线：{time_}的自然光，{quality}；{direction}；光比{ratio}。"
        "只用现场光——不出现闪光灯的生硬投影、不出现反光板打出的平光、"
        "不出现影棚式的三点布光痕迹。"
        "光线要能在画面里读出方向：同一物体上，受光面、过渡带、阴影面、反射光四个区域的边界看得清，"
        "这是「立体」的来源。"
    )
    if any(k in (time_ + quality) for k in HARSH_NOON):
        issues.append((
            "warn",
            f"W305 光线时段是「{time_}」：正午顶光会把地形压平、阴影缩到脚下、天空发白。"
            "纪实风光建议改到日出后 1–2 小时或日落前 2 小时，或阴天的散射光",
        ))
    return text, issues


def palette_block(spec: dict, preset_key: str, has_people: bool = False) -> tuple[str, list[tuple[str, str]]]:
    issues: list[tuple[str, str]] = []
    preset = PRESETS[preset_key]
    pal = spec.get("palette") or {}

    text = (
        preset["sat_bias"] + "。"
        "色彩还原优先于风格化：不做整体色偏、不做双色调分离、不做 LUT 式的统一染色，"
        "每一种颜色都保持它本来的色相，只在饱和度和明度上做文章。"
        "全画面饱和度「适中偏高」的定义是：色块内部有层次、相邻色能分开、"
        "但任何一块颜色都不会亮到失去纹理（不出现纯色溢出）。"
    )

    elements = pal.get("elements") or []
    names = [str(el.get("name", el)).strip() if isinstance(el, dict) else str(el).strip() for el in elements]
    if elements:
        lines = []
        for el in elements:
            if isinstance(el, str):
                lines.append(f"·{el}")
                continue
            name = str(el.get("name", "")).strip()
            note = str(el.get("note", "")).strip()
            anchor = HUE_ANCHORS.get(name)
            if anchor:
                h0, h1, desc = anchor
                lines.append(f"·{name}（色相 {h0}°–{h1}°）：{desc}" + (f"；本场景特殊处理：{note}" if note else ""))
            elif name:
                lines.append(f"·{name}：{note or '保持固有色，饱和度与明度随环境光自然变化'}")
        text += "分色要求如下（色相以 HSV 计）：" + "；".join(x.lstrip("·") for x in lines) + "。"
    else:
        issues.append((
            "warn",
            "W306 palette.elements 为空：只写「色彩浓郁」模型会给你通篇艳色。"
            "至少指定 天空/植被/大地 三个元素的分色要求",
        ))

    # 肤色的色相（20°–32°）与大地色（20°–42°）几乎重叠，不单独声明就一定会一起被推饱和
    if has_people and not any("肤色" in n or "人物" in n or "皮肤" in n for n in names):
        text += (
            "肤色必须与大地色分离：皮肤与土墙、岩石同处暖色区间，但必须能一眼分开——"
            "受光皮肤的饱和度和明度都低于同一画面里的土色，阴影侧的皮肤偏冷褐而不是偏绿，"
            "不允许把脸推成砖红或橘黄。"
        )
        issues.append((
            "warn",
            "W310 有主要人物，但 palette.elements 里没有肤色条款：已自动补上「肤色与大地色分离」一段。"
            "土色都在 20°–42° 色相区，不单独约束的话模型会顺手把脸一起推饱和，出来是橘色的脸",
        ))

    if pal.get("accent"):
        text += f"点缀色：{pal['accent']}——面积小、饱和度高，用来打破大色块的沉闷，但不允许成为第二个主体。"

    text += (
        "暗部不参与「浓郁」：阴影里的颜色只降明度不降纯度到发灰，也不许整体染蓝或染绿。"
        "高光不参与「浓郁」：高光处饱和度自然衰减，避免高光边缘出现彩色镶边。"
    )
    return text, issues


def tone_block(spec: dict, preset_key: str) -> tuple[str, list[tuple[str, str]]]:
    issues: list[tuple[str, str]] = []
    preset = PRESETS[preset_key]
    tone = spec.get("tone") or {}

    text = (
        f"{preset['contrast']}；{preset['shadow_weight']}。"
        "调色顺序必须是「先立动态范围、再给中间调对比」，反过来就是 HDR 的假亮。"
        "黑位：最深的暗部落在 5%–10% 的亮度区间而不是 0，纯黑区域里仍然能读出纹理走向；"
        "白位：最亮的高光落在 96%–98% 而不是被裁到 255，云、雪、水面高光内部有层层过渡；"
        "中间调：明暗交界过渡紧凑，斜率略陡，形成「硬朗」的观感；"
        "局部对比：靠光线的自然衰减产生体积感，不靠锐化与清晰度滑块硬拉。"
    )
    if tone.get("grain"):
        strength = float(tone.get("grain", 0.15))
        text += (
            f"颗粒：极细的胶片颗粒（强度约 {strength:.2f}），在全画面均匀分布、在暗部略明显，"
            "是「底片感」而不是「噪点感」——颗粒不能聚成块、不能出现彩噪。"
        )
        if strength > 0.35:
            issues.append((
                "warn",
                f"W307 颗粒强度 {strength:.2f} 偏高：颗粒会在暗部糊成噪点，"
                "与本风格「暗部细节丰富」的核心诉求直接冲突，建议 ≤ 0.25",
            ))
    if tone.get("klass"):
        text += f"{tone['klass']}"
    return text, issues


def quality_block(spec: dict) -> str:
    detail = str(spec.get("detail", "")).strip()
    base = (
        "【质感与细节】材质必须可辨：布料的经纬与褶皱、皮肤毛孔与汗光、"
        "岩石的颗粒与风化壳、木头的纹理与开裂、金属的氧化与划痕、水面的波纹尺度、"
        "草叶的分叉与尘土。同一画面里不同材质的高光形状不同（皮肤是柔的散射高光、"
        "金属是锐的镜面高光、水面是碎的点状高光），这是真实感最有效的判据。"
    )
    if detail:
        base += "本场景重点：" + detail
    return base


def format_block(spec: dict) -> tuple[str, list[tuple[str, str]]]:
    issues: list[tuple[str, str]] = []
    aspect = str(spec.get("aspect", "3:2")).strip()
    size = str(spec.get("size", "")).strip()

    ar, sr = aspect_ratio(aspect), size_ratio(size)
    if ar is None:
        raise ValueError(f"E303 aspect「{aspect}」不是合法画幅：写成 3:2 / 4:5 / 16:9 这样")
    if size and sr is None:
        raise ValueError(f"E303 size「{size}」不是合法尺寸：写成 2048x1365 这样")
    if size and abs(ar - sr) / ar > 0.015:
        raise ValueError(
            f"E304 aspect={aspect}（{ar:.3f}）与 size={size}（{sr:.3f}）不一致："
            "出图后会被拉伸或裁切，两种写法的比例必须一致"
        )
    if size:
        w, h = (int(x) for x in re.split(r"[x×]", size))
        if max(w, h) > 4096:
            raise ValueError(f"E305 size={size} 超过模型单边上限 4096：8K 是后期重采样交付尺寸，不要直接请求")
    if aspect not in ASPECT_SIZES:
        issues.append(("warn", f"W308 aspect={aspect} 不在推荐集 {list(ASPECT_SIZES)}：确认这是你要的版面"))

    rec = ASPECT_SIZES.get(aspect, "2048x1365")
    text = (
        f"【输出】{aspect} 画幅，单帧完整照片，构图在出图时就成立（不靠后期裁切二次构图）。"
        f"请求尺寸 {size or rec}，长边不超过 4096。"
        "画面是满幅的摄影作品：没有留白边框、没有多格拼接、没有样张排版。"
    )
    return text, issues


# ---------------------------------------------------------------- 主装配

def build(spec: dict) -> tuple[str, list[tuple[str, str]]]:
    issues: list[tuple[str, str]] = []

    preset_key = str(spec.get("mode", "travel")).strip().lower()
    if preset_key not in PRESETS:
        issues.append(("warn", f"W309 mode「{preset_key}」不认识，按 travel（旅行纪实）处理"))
        preset_key = "travel"
    preset = PRESETS[preset_key]

    style_text, w, brand_used = style_block(spec)
    issues += w

    scene_text, w = scene_block(spec)
    issues += w

    people_text, w = people_block(spec, preset)
    issues += w
    has_people = bool((spec.get("people") or {}).get("desc")) or bool((spec.get("people") or {}).get("count"))

    cam_text, w = camera_block(spec)
    issues += w

    light_text, w = light_block(spec)
    issues += w

    pal_text, w = palette_block(spec, preset_key, has_people)
    issues += w

    tone_text, w = tone_block(spec, preset_key)
    issues += w

    fmt_text, w = format_block(spec)
    issues += w

    # 高危词：只扫用户自由文本，不扫脚本自己生成的话
    raw_texts = [
        str(spec.get("scene", "")), str(spec.get("subject", "")),
        str((spec.get("style_anchor") or {}).get("mapped", "")),
        str(spec.get("detail", "")), str(spec.get("notes", "")),
        " ".join(str(x) for x in (spec.get("palette") or {}).get("elements") or []),
    ]
    hits = scan_high_risk(*raw_texts)
    if hits:
        detail = "；".join(f"「{w}」({r})" for w, r in hits)
        issues.append((
            "error",
            f"E306 输入里有会破坏该调子的词：{detail}。请从 spec 里删掉，或确认你要的是别的风格",
        ))

    # 风格相关冲突
    if brand_used and not any("边框" in str(f) for f in (spec.get("forbidden") or [])):
        issues.append((
            "info",
            "已自动把「没有黄色矩形边框 / 没有杂志字标 / 没有白边」写进提示词与禁止项："
            "这是「国家地理」这个词最容易触发的错误先验",
        ))
    if has_people and preset_key == "nature":
        issues.append(("warn", "W311 mode=nature 但写了主要人物：风光预设的饱和度会把脸也推上去，考虑改 travel"))

    forbidden = [
        "水印", "logo", "签名", "文字", "字幕", "画框", "白边", "黄色矩形边框", "杂志排版", "杂志色块",
        "过度 HDR", "橙青分离", "暗角", "滤镜感", "塑料皮肤", "磨皮", "油画笔触", "插画感",
        "3D 渲染", "CG 感", "过饱和溢出", "彩色镶边", "锐化光晕", "噪点块", "模糊失焦", "多格拼接",
        "畸形手指", "面部变形", "重复人物",
    ]
    forbidden += [str(f) for f in (spec.get("forbidden") or [])]
    forbidden = dedupe(forbidden)

    blocks = [
        "【任务】生成一张可直接出版的纪实摄影作品（单帧、满幅、无版式）。"
        "它看起来必须像「有人真的站在那个位置按下快门」，而不是像「一个被生成出来的画面」。",
        "【风格定位】" + style_text + "。",
        "【场景】" + scene_text,
        "【人物】" + people_text,
        "【构图与镜头】" + cam_text,
        "【光线】" + light_text,
        "【色彩】" + pal_text,
        "【影调与动态范围】" + tone_text,
        quality_block(spec),
        "【真实感红线】三条底线：一、光线只有一个来源方向，所有物体的受光面与投影都服从它；"
        "二、材质的反射性质不串（布不反光、石头不透明、水面必反光）；"
        "三、没有任何「为了好看」而存在的元素——画面里每样东西都要能回答「它为什么在这里」。"
        "不做超现实、不做概念合成、不做双曝光。",
        "【禁止】" + "、".join(forbidden) + "。",
        fmt_text,
    ]

    prompt = "\n".join(blocks)
    return prompt, issues


def main() -> int:
    ap = argparse.ArgumentParser(description="经典纪实摄影调色风格 提示词装配器")
    ap.add_argument("--spec", required=True, help="spec JSON 路径")
    ap.add_argument("--outdir", help="输出目录（写 prompt.txt / meta.json）")
    ap.add_argument("--check", action="store_true", help="只跑质量门并打印提示词，不写文件")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

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
            "mode": spec.get("mode", "travel"),
            "region": spec.get("region"),
            "aspect": spec.get("aspect", "3:2"),
            "size": spec.get("size") or ASPECT_SIZES.get(str(spec.get("aspect", "3:2")), "2048x1365"),
            "chars": len(prompt),
            "gate": {
                "errors": len(errors),
                "issues": [f"[{lv}] {m}" for lv, m in issues],
            },
        }
        (outdir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n[write] {outdir / 'prompt.txt'}（{len(prompt)} 字）")
        print(f"[write] {outdir / 'meta.json'}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
