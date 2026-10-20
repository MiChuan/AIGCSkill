#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""水墨平涂印刷品（米白特种纸 / 极简几何重构 / 底部英文排版）提示词装配器。

输入一份 JSON spec，输出可直接喂给 Hy Image3.5 preview 的中文结构化提示词。
纯标准库实现，无第三方依赖。

设计要点（每一条都对应一个稳定的翻车模式）：
  1. 「水墨」这个词直接进提示词，模型给的是**传统国画**：宣纸毛边、卷轴、
     题款、朱印、皴法。本 skill 要的是**淡墨作为笔触层**出现在几何色块之上，
     所以「水墨」必须被翻译成浓度、笔势、渗化半径三个可执行参数，
     同时把国画的伴生物写进禁止项。
  2. 「边缘自然柔和」被理解成整张图高斯模糊。所以提示词里把「清晰的边界」
     和「柔和的墨色过渡」分成两条并列写，并且明说色块与色块之间的边缘是
     干净的——那是平面的清晰，不是照片的锐利。
  3. 留白是版面结构，不是背景。必须给比例、给基线、给边距，
     否则模型会填满整个画布，然后「留白」退化成一条白边。
  4. 色彩只给形容词没用。色板来自 extract_palette.py 的实测 hex，
     并且原色必须被改写（降饱和、抬明度）才能印在米白纸上。
  5. 文字是本风格最大的风险点：模型排英文长句会糊字、会多出一行、
     会把标题写成 logo。所以字数上限写死，超过就拦，并给出叠字保底路线。

用法：
    python build_print_prompt.py --spec specs/ink-print-lake.json --outdir out/demo
    python build_print_prompt.py --spec specs/ink-print-lake.json --check   # 只跑质量门
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------- 常量

PAPER_HEX = "#F4EFE4"

# 画幅 → 出图尺寸（Hy Image3.5 单边上限 4096，2048 是原生档）
ASPECT_SIZES = {
    "4:5": "1638x2048",
    "2:3": "1365x2048",
    "3:4": "1536x2048",
    "1:1": "2048x2048",
    "3:2": "2048x1365",
    "16:9": "2048x1152",
}

# 默认版面（都是占画幅高度的比例，左上角为原点）
LAYOUT_DEFAULT = {
    "whitespace": 0.52,     # 纯纸白区域占全画面的比例
    "block_top": 0.10,      # 插画块上缘
    "block_bottom": 0.62,   # 插画块下缘
    "block_width": 0.68,    # 插画块宽度（相对画幅宽度）
    "title_y": 0.80,        # 标题行中心
    "desc_y": 0.875,        # 描述行中心
    "gap": 0.07,            # 插画下缘到标题顶部的空档
}

# 「水墨/国画/扁平插画/海报」这类风格词 → 可执行条款。
# 命中即替换，因为模型对这些词的先验都带着一整套伴生物。
STYLE_ANCHORS = {
    "ink": (
        "以淡墨晕染作为唯一的笔触层：墨分五色，只用最淡的两三级，"
        "墨色始终是半透明的，能看见底下的色块；墨的边界靠水分自然渗开，"
        "中心浓、边缘淡，一笔之内有浓淡变化"
    ),
    "guohua": (
        "淡墨晕染的笔触层：墨分五色，只用最淡的两三级，半透明，边缘靠水分渗开"
    ),
    "flat": (
        "分层平涂的几何色块：每一层的颜色是均匀的，没有渐变、没有笔触纹理、没有噪点，"
        "层与层之间靠明度差分开而不是靠描边"
    ),
    "poster": (
        "编辑设计（editorial design）的版面逻辑：大面积纸白留白、窄的视觉重心、"
        "底部居中的两行小字，像一本艺术出版社的图录扉页"
    ),
}

BRAND_PATTERNS = {
    "水墨": "ink",
    "国画": "guohua",
    "写意": "ink",
    "淡墨": "ink",
    "扁平插画": "flat",
    "扁平": "flat",
    "海报": "poster",
    "平面设计": "poster",
    "editorial": "poster",
}

# 会毁掉这张图的高危词。命中即 error，因为它们的失败模式是 100% 复现的。
HIGH_RISK_WORDS = {
    "水彩": "水彩的默认表现是透明叠加 + 花朵状扩散边，会把平涂色块泡烂。要湿边就写「淡墨渗化」",
    "渐变": "渐变得出屏幕味；本风格的色块必须是均匀平涂的一整块",
    "gradient": "渐变得出屏幕味；本风格的色块必须是均匀平涂的一整块",
    "纸纹": "纸纹/纤维纹理会被画成噪点，米白纸的质感是均匀哑光，不是颗粒",
    "纤维": "纸纤维会被画成噪点或毛边，纸面要干净",
    "褶皱": "折痕/卷边会让画面变成「一张纸的照片」，本 skill 要的是纸上的画，不是拍纸",
    "阴影": "印刷品没有投影。任何「阴影」都会被画成 3D 浮雕或环境光遮蔽",
    "投影": "印刷品没有投影。任何「投影」都会被画成 3D 浮雕或环境光遮蔽",
    "立体": "立体感 = 光影 = 不是平面的了",
    "3d": "3D 渲染的塑料高光是本风格的头号反面",
    "写实": "写实会把几何重构拉回照片贴图",
    "照片": "参考图只提供结构，不要让它以照片形式出现在成图里",
    "细节丰富": "细节丰富 = 填满几何面 = 满幅杂物。本风格要的是结构的少，不是细节的多",
    "精细": "精细的纹理与装饰会让画面失去克制",
    "泼墨": "泼墨的随机飞溅与留白的克制是反义词",
    "飞溅": "飞溅/溅点会被画成满画面的墨点，本风格最多允许一处淡墨的边缘渗化",
    "splash": "飞溅/溅点会被画成满画面的墨点",
    "题款": "题款、落款、朱印会把画面拉成传统国画",
    "印章": "朱印是国画的伴生物，本风格没有任何印章",
    "卷轴": "卷轴、宣纸毛边会让画面变成「一张古董画的照片」",
    "宣纸": "本 skill 的纸是米白哑光特种纸，不是宣纸；宣纸会带出毛边与洇墨的先验",
    "边框": "加边框会让留白退化成一条白边",
    "发光": "发光/光晕在印刷品上不存在",
    "光效": "光效（镜头光晕、体积光）与无光照的印刷品冲突",
    "霓虹": "霓虹色会把这个调子拉成赛博朋克",
    "赛博": "赛博朋克与米白纸的克制完全相反",
    "装饰": "多余的装饰元素是「高级感」的头号敌人",
    "复杂": "复杂 = 细节 = 满幅。本风格的力量来自减法",
}

PLACEHOLDER_HINTS = ("待定", "某地", "xxx", "XXX", "待填", "自定", "示例", "……", "？", "[", "]", "【", "】")
CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


# ---------------------------------------------------------------- 工具

def pct(v: float) -> str:
    return f"{round(float(v) * 100):d}%"


def aspect_ratio(s: str) -> float | None:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*[:：/]\s*(\d+(?:\.\d+)?)\s*", s or "")
    if not m:
        return None
    w, h = float(m.group(1)), float(m.group(2))
    return w / h if h else None


def size_ratio(s: str) -> float | None:
    m = re.fullmatch(r"\s*(\d+)\s*[x×]\s*(\d+)\s*", s or "")
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    return w / h if h else None


def words(text: str) -> list[str]:
    return [w for w in re.split(r"\s+", (text or "").strip()) if w]


def _hex_dist(a: str, b: str) -> float:
    """两个 hex 的加权 RGB 距离（0–~500）。只用来判断「改写幅度大不大」。"""
    try:
        pa = [int(a.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
        pb = [int(b.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    except (ValueError, IndexError):
        return 0.0
    dr, dg, db = pa[0] - pb[0], pa[1] - pb[1], pa[2] - pb[2]
    return (2 * dr * dr + 4 * dg * dg + 3 * db * db) ** 0.5


def has_placeholder(text: str) -> str | None:
    for h in PLACEHOLDER_HINTS:
        if h in (text or ""):
            return h
    return None


def scan_high_risk(*texts: str) -> list[tuple[str, str]]:
    blob = " ".join(t or "" for t in texts).lower()
    return [(w, r) for w, r in HIGH_RISK_WORDS.items() if w in blob]


def dedupe(items: list[str]) -> list[str]:
    seen, out = set(), []
    for i in items:
        i = str(i).strip()
        if i and i not in seen:
            seen.add(i)
            out.append(i)
    return out


def load_palette(spec: dict) -> tuple[list[dict], dict | None, list[tuple[str, str]]]:
    """从 palette.json 或 spec.palette.manual 取色板，返回 (簇列表, 纸, issues)。"""
    issues: list[tuple[str, str]] = []
    pal_conf = spec.get("palette") or {}
    src = str(pal_conf.get("from", "")).strip()

    if src:
        p = Path(src)
        if not p.is_absolute():
            # 相对 spec 所在目录解析，再退回 cwd
            for base in (Path(spec.get("_spec_dir", ".")), Path.cwd()):
                cand = base / src
                if cand.exists():
                    p = cand
                    break
        if not p.exists():
            return [], None, [("error", f"E120 palette.from 指向的色板文件不存在：{src}")]
        data = json.loads(p.read_text(encoding="utf-8"))
        clusters = [c for c in data.get("clusters", []) if c.get("role") != "drop"]
        paper = data.get("paper")
        ink = data.get("ink")
        issues.append((
            "info",
            f"色板取自 {src}：{len(clusters)} 个保留色簇，纸白 {paper.get('hex') if paper else PAPER_HEX}，"
            f"墨色 {ink.get('hex') if ink else '未提取'}",
        ))
        return clusters, paper, issues

    manual = pal_conf.get("manual") or []
    clusters = []
    for el in manual:
        if isinstance(el, str):
            clusters.append({"hex": el, "role": "manual", "share": None, "flat": {"note": ""}})
        elif isinstance(el, dict):
            clusters.append({
                "hex": str(el.get("hex", "")).strip(),
                "role": str(el.get("role", "manual")),
                "share": el.get("share"),
                "flat": {"note": str(el.get("note", ""))},
            })
    bad = [c["hex"] for c in clusters if not re.fullmatch(r"#[0-9A-Fa-f]{6}", c["hex"] or "")]
    if bad:
        return [], None, [("error", f"E121 manual 色板里有不合法的 hex：{bad}（写成 #RRGGBB）")]
    if clusters:
        issues.append(("warn", "W120 用的是手写色板：色相是否真的来自原图无从校验，记得对着原图核一遍"))
    return clusters, None, issues


# ---------------------------------------------------------------- 各段装配

def style_block(spec: dict) -> tuple[str, list[tuple[str, str]]]:
    issues: list[tuple[str, str]] = []
    anchor = spec.get("style_anchor") or {}
    raw = str(anchor.get("raw", "")).strip()
    mapped = str(anchor.get("mapped", "")).strip()

    keys, hits = [], []
    low = raw.lower()
    for pat, key in BRAND_PATTERNS.items():
        if pat in low and key not in keys:
            keys.append(key)
            hits.append(pat)
    if keys:
        head = "；".join(STYLE_ANCHORS[k] for k in keys)
        if mapped:
            head += "；" + mapped
        issues.append((
            "info",
            f"风格锚点「{'、'.join(hits)}」已翻译成可执行条款：这些词在模型里各自带一整套伴生物"
            "（水墨→卷轴题款朱印、扁平→色块糊成一片、海报→加边框加装饰），所以只保留笔触与版面属性",
        ))
        text = head
    elif mapped:
        text = mapped
        issues.append(("warn", "W101 style_anchor.mapped 是自定义描述：确认里面没有渐变/阴影/纸纹这类反向词"))
    else:
        text = "；".join(STYLE_ANCHORS[k] for k in ("flat", "ink", "poster"))
        issues.append(("warn", "W102 没写 style_anchor，按「平涂 + 淡墨 + 编辑版面」默认处理"))

    text += (
        "。风格定位是一张**平面设计作品 / 艺术印刷品**，不是照片、不是插画稿、也不是国画："
        "它有印刷品的物理属性（纸、油墨、无光照），有版面的属性（留白、基线、边距），"
        "但没有场景的属性（没有光源、没有景深、没有环境）。"
    )
    return text, issues


def source_block(spec: dict) -> tuple[str, list[tuple[str, str]]]:
    issues: list[tuple[str, str]] = []
    src = spec.get("source") or {}
    crop = str(src.get("crop", "画面上半部分")).strip()
    keep = str(src.get("keep", "")).strip()
    drop = str(src.get("drop", "")).strip()
    path = str(src.get("path", "")).strip()

    if not path and not keep:
        issues.append((
            "error",
            "E101 source 为空：本 skill 是「基于参考原图重构」，必须写 path（参考图路径）"
            "或至少写 keep（要保留下来的景物）",
        ))

    text = f"参考原图只取{crop}，其余部分全部丢弃。"
    if keep:
        text += f"保留的景物只有这些：{keep}。"
    if drop:
        text += f"原图里明确丢弃的：{drop}——它们不进画面，一个都不进。"
    text += (
        "参考图的作用是提供「结构」与「色彩」，不是提供「内容」："
        "不描摹原图的像素、不保留原图的透视与明暗、不出现照片质感的局部。"
        "重构后仍要能认出原图是同一处景物（靠轮廓与坡度），但已经没有任何一个像素来自照片。"
    )
    return text, issues


def structure_block(spec: dict) -> tuple[str, list[tuple[str, str]]]:
    issues: list[tuple[str, str]] = []
    st = spec.get("structure") or {}
    skeleton = str(st.get("skeleton", "")).strip()
    seam = str(st.get("seam", "")).strip()
    planes = st.get("planes") or []

    if not skeleton:
        issues.append((
            "error",
            "E102 structure.skeleton 为空：没有骨架线，模型会把景物画成一堆并排的色块，"
            "形不成结构神韵。写一句「画面靠哪一条线立起来」",
        ))
    if not planes:
        issues.append(("error", "E103 structure.planes 为空：重构的几何面至少要 4 个"))

    count_note = ""
    n = len(planes)
    if n:
        if n < 4:
            issues.append(("warn", f"W103 只有 {n} 个几何面：会退化成「一个色块 + 留白」，缺少层次"))
        elif n > 9:
            issues.append((
                "warn",
                f"W104 写了 {n} 个几何面：超过 9 个，人眼就不再读作「极简」。"
                "按面积排序，砍掉后 1/3，把它们并进上一层",
            ))
        count_note = f"整个插画由 {n} 个平面构成，不多不少。"

    lines = []
    for i, el in enumerate(planes):
        if isinstance(el, str):
            lines.append(f"第{i + 1}面——{el}")
            continue
        name = str(el.get("name", "")).strip()
        form = str(el.get("form", "")).strip()
        role = str(el.get("role", "")).strip()
        seg = f"第{i + 1}面·{name}：{form}"
        if role:
            seg += f"（{role}）"
        lines.append(seg)

    text = ""
    if skeleton:
        text += f"几何骨架：{skeleton}。这条骨架在画面里必须能一眼读出来，低饱和也不许消失。"
    if lines:
        text += "解构后的平面如下（按视觉权重从大到小）：" + "；".join(lines) + "。"
    if count_note:
        text += count_note
    text += (
        "每一个面都是一整块闭合的色形：形状由直线、缓弧、斜切构成，不要描边、不要双线、"
        "不要在面内再切碎。面与面的关系是「叠压」而不是「拼接」——上层压住下层，"
        "交界处是一条干净的线，没有过渡灰带。"
    )
    if seam:
        text += f"接缝处理：{seam}。"
    return text, issues


def palette_block(spec: dict, clusters: list[dict], paper: dict | None) -> tuple[str, list[tuple[str, str]]]:
    issues: list[tuple[str, str]] = []
    paper_hex = (paper or {}).get("hex", PAPER_HEX)

    if not clusters:
        return "", issues

    text = (
        f"纸白：{paper_hex}（米白、微暖、哑光，接近象牙白但不是米黄）。"
        "全画面只有这一种纸色，纸白同时承担「高光」的角色——"
        "最亮的地方是纸本身，不要再加比纸更白的颜色。"
    )

    # 色板：给 hex、给面积占比、给层级。主展示「平涂改写色」——
    # 原图的深色直接写在提示词里，模型会照抄屏幕色，出来发脏。
    items = []
    for c in clusters[:8]:
        hexv = c.get("hex", "")
        share = c.get("share")
        role = str(c.get("role", ""))
        flat_hex = (c.get("flat") or {}).get("hex") if isinstance(c.get("flat"), dict) else None
        seg = flat_hex or hexv
        if flat_hex and hexv and _hex_dist(hexv, flat_hex) > 10:
            seg += f"（原图实测 {hexv}，已压到纸上）"
        if isinstance(share, (int, float)):
            seg += f" 占插画面积约 {round(share * 100):d}%"
        if role:
            seg += f"，{_role_cn(role)}"
        items.append(seg)
    text += "配色只用下面这几个色相，一个不多：" + "；".join(items) + "。"
    text += (
        "面积配比是主次分明的：最大的一个色相占插画面积的一半以上，"
        "第二层占三成，其余加起来不超过两成。"
        "所有颜色都做过「印刷化改写」——色相忠于原图，饱和度压到淡彩（不超过 0.42，约为原值的 55%），"
        "明度整体抬到能落在纸上的区间（0.34–0.96）；"
        "原图里那些深绿、深蓝、纯黑不许原样出现，它们会被纸白衬得发脏。"
    )

    accent = str((spec.get("palette") or {}).get("accent", "")).strip()
    if accent:
        text += f"点缀色：{accent}——面积很小，是画面里唯一的「响」的地方，但不许成为第二个主体。"
    else:
        text += "画面里没有高饱和的点缀色：整张图是同一族低饱和色的深浅关系。"

    ink = str((spec.get("palette") or {}).get("ink", "")).strip()
    if ink:
        text += f"最深的一层墨是 {ink}，它只出现在最重的那个面和文字上。"
    else:
        text += "最深的一层是墨色（约 #2E2C27 的暖黑），不是纯黑；纯黑在米白纸上像屏幕像素。"

    text += (
        "全画面不允许出现原色板之外的颜色，尤其不允许紫蓝渐变、彩虹色、荧光色、"
        "以及任何「为了平衡画面」而添加的第三色。"
    )

    # 区分度：色板里两个色挨得太近，插画就会糊成一团（这是「扁平」最容易翻车的地方）
    hexes = [str(c.get("hex", "")) for c in clusters[:6] if re.fullmatch(r"#[0-9A-Fa-f]{6}", str(c.get("hex", "")))]
    if len(hexes) >= 2:
        pairs = [(_hex_dist(hexes[i], hexes[j]), hexes[i], hexes[j])
                 for i in range(len(hexes)) for j in range(i + 1, len(hexes))]
        dmin, a, b = min(pairs)
        if dmin < 25:
            issues.append((
                "warn",
                f"W121 色板区分度不足：{a} 与 {b} 的加权距离只有 {dmin:.0f}（< 25）。"
                "两个色块挨得这么近，平涂之后会糊成一片，构图的层次就没了。"
                "重跑 extract_palette.py 把 --k 调小（合并近似色），或者手动指定一个更深的层",
            ))
    return text, issues


def _role_cn(role: str) -> str:
    return {
        "primary": "主色块",
        "secondary": "第二层",
        "support": "支持层",
        "accent": "点缀",
        "ground": "底色/大面积浅色",
        "manual": "手写指定",
        "drop": "已剔除",
    }.get(role, role)


def wash_block(spec: dict) -> tuple[str, list[tuple[str, str]]]:
    issues: list[tuple[str, str]] = []
    w = spec.get("wash") or {}
    places = [str(x).strip() for x in (w.get("places") or []) if str(x).strip()]
    density = str(w.get("density", "极淡，浓度 8%–15%（墨与纸的混合比例）")).strip()
    stroke = str(w.get("stroke", "一笔横拖，起笔略重、收笔渐轻，笔锋不回收")).strip()

    if not places:
        issues.append((
            "warn",
            "W105 wash.places 为空：不给位置，模型会把淡墨晕染铺满整张图，"
            "结果是一层灰雾而不是笔触。至少指定 1–2 处，且必须贴着结构（山脚、水线、云脚）",
        ))
        places = ["山体与水面（或地面）交接的一道横向带"]

    text = (
        f"淡墨晕染只出现在这几处：{'、'.join(places)}。它是一层**叠加**在平涂色块之上的笔触，"
        "不是色块本身，也不是背景。"
        f"浓度：{density}——淡到能看见下面的色块，浓淡变化靠水分而不是靠加黑。"
        f"笔势：{stroke}；一笔之内有起收，笔与笔之间留缝，不来回涂抹。"
    )
    text += (
        "墨的边界是「渗」出来的：中心实、边缘虚，虚的宽度只有 2–4 毫米（相对画面），"
        "边缘有极轻微的毛边与水痕，但绝不出现花朵状的扩散、盐粒效果、喷溅点、以及水彩式的透明叠色。"
    )
    text += (
        "关键的区分：**色块的边缘是干净的，墨的边缘才是柔的**。"
        "几何面之间的交界是一条利落的线（这是平面的清晰），"
        "只有淡墨接触纸白的那一圈是渐隐的。"
        "两者不能混——混了就是整张图糊掉，或者整张图变成硬边矢量图。"
    )
    return text, issues


def layout_block(spec: dict) -> tuple[str, list[tuple[str, str]]]:
    issues: list[tuple[str, str]] = []
    lo = dict(LAYOUT_DEFAULT)
    lo.update({k: v for k, v in (spec.get("layout") or {}).items() if v is not None})

    ws = float(lo["whitespace"])
    bt, bb = float(lo["block_top"]), float(lo["block_bottom"])
    bw = float(lo["block_width"])
    ty, dy = float(lo["title_y"]), float(lo["desc_y"])
    gap = float(lo["gap"])

    if not 0 <= bt < bb <= 1:
        issues.append(("error", f"E104 版面冲突：block_top={bt} 必须小于 block_bottom={bb}"))
    if not 0 < ws <= 0.9:
        issues.append(("error", f"E105 whitespace={ws} 不是合法比例（0–0.9）"))
    if ws < 0.40:
        issues.append((
            "warn",
            f"W106 留白只有 {pct(ws)}：本风格的克制感主要来自留白。"
            "低于 40% 时画面会读作「一张满幅插画」，而不是「一张有版面的印刷品」。建议 45%–58%",
        ))
    if bt < 0.06:
        issues.append(("warn", f"W107 block_top={pct(bt)}：插画顶到纸边了，上边距至少留 8%"))
    if bb + gap > ty - 0.03:
        issues.append((
            "error",
            f"E106 版面冲突：插画下缘 {pct(bb)} + 空档 {pct(gap)} = {pct(bb + gap)}，"
            f"已经压到标题（{pct(ty)}）。文字必须完全落在纸白上，不能被色块托着",
        ))
    if ty >= dy:
        issues.append(("error", "E107 版面冲突：title_y 必须在 desc_y 之上"))
    if dy > 0.94:
        issues.append(("warn", f"W108 desc_y={pct(dy)}：描述行贴到纸边了，下边距至少留 6%"))

    text = (
        f"【版面】纯纸白（不含文字）占全画面的 {pct(ws)} 以上，且这些留白是**连续的整块**，"
        "不是被色块切碎的白缝。"
        f"插画块水平居中，宽度占画幅 {pct(bw)}，上缘在 {pct(bt)} 处，下缘在 {pct(bb)} 处；"
        f"插画下缘到文字之间空出 {pct(gap)} 的纯纸白。"
        "画面左右两侧的插画块不顶边，两侧各留出不小于 12% 的纸白。"
        "整个构图的重心在上半部，下半部只有两行小字——上重下轻，这是版面的呼吸。"
    )
    return text, issues


def type_block(spec: dict) -> tuple[str, list[tuple[str, str]]]:
    issues: list[tuple[str, str]] = []
    t = spec.get("type") or {}
    title = str(t.get("title", "")).strip()
    desc = str(t.get("desc", "")).strip()
    ink = str(t.get("ink", "#2E2C27")).strip()
    lo = dict(LAYOUT_DEFAULT)
    lo.update({k: v for k, v in (spec.get("layout") or {}).items() if v is not None})

    for field, val in (("title", title), ("desc", desc)):
        ph = has_placeholder(val)
        if ph:
            issues.append(("error", f"E110 type.{field} 里还有占位符「{ph}」：模型会把占位符原样排进画面"))

    if not title:
        issues.append(("error", "E111 type.title 为空：底部第一行是这张图的标题，不能空着"))
    if not desc:
        issues.append(("warn", "W110 type.desc 为空：第二行的描述句是版面的呼吸，建议给 8–12 个词"))

    # 中文字符：图像模型排汉字是画形状，不是排字
    for field, val in (("title", title), ("desc", desc)):
        m = CJK.search(val)
        if m:
            issues.append((
                "error",
                f"E112 type.{field} 含中文字符「{m.group(0)}」：本 skill 的排版行只排英文。"
                "图像模型渲染汉字是画形状不是排字，4 个字以上几乎必有错字。"
                "中文标题请改用 scripts/settype.py 后期叠字",
            ))

    wt, wd = words(title), words(desc)
    if len(wt) > 4:
        issues.append((
            "warn",
            f"W111 标题 {len(wt)} 个词：「{' '.join(wt)}」偏长。标题 2–4 个词最稳，"
            "超过 4 个词时模型容易缩字号、加装饰线，或者干脆漏字",
        ))
    if len(wd) > 14:
        issues.append((
            "warn",
            f"W112 描述 {len(wd)} 个词：小字号长句是糊字的高发区。压到 14 词以内，"
            "或者拆成「描述 + 叠字补」的路线（见 settype.py）",
        ))
    if len(wd) < 3:
        issues.append(("warn", "W113 描述少于 3 个词：第二行会显得比标题更像标签，缺少句子感"))

    title_style = str(t.get("title_style", "")).strip() or (
        "优雅的衬线体，带轻微的手写笔势（calligraphic serif）：笔画有粗细变化、"
        "字腔不闭合过紧、收笔有一点点上挑；字重 regular，全小写，字距正常"
    )
    desc_style = str(t.get("desc_style", "")).strip() or (
        "无衬线体，字重 light，全小写，字距放大到 +15% 左右，行内不换行"
    )

    text = (
        f"文字只有两行，居中排布，除此之外画面里没有任何其他文字。"
        f"第一行是标题「{title}」，{title_style}；"
        f"第二行是「{desc}」，{desc_style}。"
        f"字号关系：标题的字高约为描述的两点二到两点五倍——这个比例是「标题 + 注脚」的关系，"
        "不是「大标题 + 小标题」。"
        f"两行之间空出约一个描述行高的空档，两行的正中心分别在 {pct(lo['title_y'])} 与 {pct(lo['desc_y'])} 处。"
        f"文字颜色 {ink}（墨黑，不是纯黑），与纸白的对比清晰但不刺眼。"
        "排版纪律：不加引号、不加连字符断行、不加装饰线、不加圆点、不加序号、不加副标题、"
        "不加第二段说明；字距左右对称，整块文字的水平中线与画幅中线重合（误差极小）；"
        "文字必须是**排出来的**，不是画出来的——字形的字重、字距、基线要一致。"
    )
    return text, issues


def medium_block(spec: dict, paper: dict | None) -> str:
    paper_hex = (paper or {}).get("hex", PAPER_HEX)
    return (
        "【承载与质感】画面是印在米白哑光特种纸上的作品："
        f"纸色 {paper_hex}，微暖、无光泽、无反光、无网点、无纹理颗粒、无折痕、无卷边。"
        "纸面是均匀的——每一处纸白都完全一样，不出现纸张的明暗变化、不出现暗角、"
        "不出现边角的加深。整张图没有光源方向、没有环境光、没有投影、没有景深、没有高光反射；"
        "它是扫描/翻拍平面印刷品得到的效果，而不是「一张纸躺在桌上被拍下来」。"
        "色块是均匀的平涂油墨：边缘干净、内部没有颗粒、没有笔刷纹理、没有厚涂的立体感。"
        "唯一的「手感」来自淡墨晕染那一层（见笔触段），以及油墨落在纸上极轻微的边缘柔化（不超过 1 像素）。"
    )


def format_block(spec: dict) -> tuple[str, list[tuple[str, str]]]:
    issues: list[tuple[str, str]] = []
    aspect = str(spec.get("aspect", "4:5")).strip()
    size = str(spec.get("size", "")).strip()

    ar, sr = aspect_ratio(aspect), size_ratio(size)
    if ar is None:
        issues.append(("error", f"E120 aspect「{aspect}」不是合法画幅：写成 4:5 / 2:3 / 1:1 这样"))
    if size and sr is None:
        issues.append(("error", f"E121 size「{size}」不是合法尺寸：写成 1638x2048 这样"))
    if ar and sr and abs(ar - sr) / ar > 0.015:
        issues.append((
            "error",
            f"E122 aspect={aspect}（{ar:.3f}）与 size={size}（{sr:.3f}）不一致：出图后会被拉伸或裁切",
        ))
    if size:
        w, h = (int(x) for x in re.split(r"[x×]", size))
        if max(w, h) > 4096:
            issues.append(("error", f"E123 size={size} 超过模型单边上限 4096"))

    rec = ASPECT_SIZES.get(aspect, "1638x2048")
    if aspect and aspect not in ASPECT_SIZES:
        issues.append(("warn", f"W114 aspect={aspect} 不在推荐集 {list(ASPECT_SIZES)}：确认这是你要的版面"))

    text = (
        f"【输出】{aspect} 竖幅（竖版是这类印刷品的默认版面），单张完整的平面设计作品，"
        f"请求尺寸 {size or rec}。"
        "画面是一张完整的作品：没有画框、没有白边、没有样机、没有多格拼接、"
        "没有「一张纸放在桌面上」的场景、没有手指与阴影。"
    )
    return text, issues


# ---------------------------------------------------------------- 主装配

def build(spec: dict) -> tuple[str, list[tuple[str, str]]]:
    issues: list[tuple[str, str]] = []

    clusters, paper, w = load_palette(spec)
    issues += w
    if not clusters:
        issues.append((
            "error",
            "E124 没有可用色板：先跑 scripts/extract_palette.py 从原图提取，"
            "再在 spec 里写 palette.from，或者手写 palette.manual。"
            "只写「色彩参考原图」是无效指令——模型拿不到原图的色值，会自己编一套",
        ))

    style_text, w = style_block(spec)
    issues += w

    src_text, w = source_block(spec)
    issues += w

    st_text, w = structure_block(spec)
    issues += w

    pal_text, w = palette_block(spec, clusters, paper)
    issues += w

    wash_text, w = wash_block(spec)
    issues += w

    layout_text, w = layout_block(spec)
    issues += w

    type_text, w = type_block(spec)
    issues += w

    fmt_text, w = format_block(spec)
    issues += w

    # 高危词只扫用户自由文本
    raw_texts = [
        str(spec.get("scene", "")),
        str((spec.get("style_anchor") or {}).get("raw", "")),
        str((spec.get("style_anchor") or {}).get("mapped", "")),
        str((spec.get("source") or {}).get("keep", "")),
        str((spec.get("structure") or {}).get("skeleton", "")),
        str((spec.get("wash") or {}).get("density", "")),
        str((spec.get("wash") or {}).get("stroke", "")),
        str(spec.get("notes", "")),
        " ".join(str(p.get("form", "")) if isinstance(p, dict) else str(p)
                 for p in (spec.get("structure") or {}).get("planes") or []),
    ]
    hits = scan_high_risk(*raw_texts)
    if hits:
        detail = "；".join(f"「{x}」({r})" for x, r in hits)
        issues.append(("error", f"E130 输入里有会破坏该风格的词：{detail}。请换措辞或删掉"))

    forbidden = [
        "照片贴图", "写实细节", "摄影光影", "光源方向", "投影", "环境光遮蔽",
        "渐变", "色彩溢出", "荧光色", "彩虹色", "紫蓝渐变",
        "纸纹", "纤维", "颗粒", "暗角", "折痕", "卷边", "纸张反光",
        "3D 渲染", "浮雕", "塑料高光", "描边", "双线", "发光",
        "水彩花朵", "泼墨", "溅点", "盐粒效果",
        "国画题款", "朱印", "印章", "卷轴", "宣纸毛边",
        "画框", "白边", "样机", "桌面", "手指",
        "装饰线", "圆点", "序号", "副标题", "第二段文字", "中文字",
        "多格拼接", "九宫格", "元素堆叠", "满幅构图",
    ]
    forbidden += [str(f) for f in (spec.get("forbidden") or [])]
    forbidden = dedupe(forbidden)

    blocks = [
        "【任务】把一张参考照片重构成一张平面设计作品："
        "一张印在米白哑光特种纸上的、以淡墨与平涂色块构成的极简几何插画，"
        "底部居中两行英文。它不是照片的滤镜处理，也不是照片的描摹，"
        "而是**把照片的信息量砍掉一个数量级之后剩下的结构**。",
        "【风格定位】" + style_text,
        "【取景与重构】" + src_text,
        "【几何骨架与平面】" + st_text,
        "【色彩】" + pal_text,
        "【笔触与晕染】" + wash_text,
        medium_block(spec, paper),
        layout_text,
        "【文字】" + type_text,
        "【克制红线】四条底线：一、画面里每一块颜色的面积都能解释——没有「填空用」的色块；"
        "二、几何面总数不超过九个，且面内不再切碎；三、淡墨只在指定的几处，"
        "不许为了「丰富」再加墨团；四、整张图只有一种纸色、一组同族色、两行字。",
        "【禁止】" + "、".join(forbidden) + "。",
        fmt_text,
    ]

    prompt = "\n".join(blocks)
    return prompt, issues


def main() -> int:
    ap = argparse.ArgumentParser(description="水墨平涂印刷品 提示词装配器")
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

    spec["_spec_dir"] = str(spec_path.parent.resolve())

    prompt, issues = build(spec)

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
        lo = dict(LAYOUT_DEFAULT)
        lo.update({k: v for k, v in (spec.get("layout") or {}).items() if v is not None})
        meta = {
            "spec": str(spec_path),
            "name": spec.get("name"),
            "aspect": spec.get("aspect", "4:5"),
            "size": spec.get("size") or ASPECT_SIZES.get(str(spec.get("aspect", "4:5")), "1638x2048"),
            "chars": len(prompt),
            "layout": lo,
            "type": spec.get("type"),
            "gate": {
                "errors": len(errors),
                "issues": [f"[{lv}] {m}" for lv, m in issues],
            },
        }
        (outdir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[write] {outdir / 'prompt.txt'}（{len(prompt)} 字）")
        print(f"[write] {outdir / 'meta.json'}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
