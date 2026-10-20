#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""韩系学院风证件写真：结构化提示词装配器（纯标准库）。

把一份 JSON spec 装配成两段可以直接喂给模型的东西：
  prompt.txt   正面提示词（13 节，中文结构化）
  negative.txt 负面提示词（去重、去冲突后的一行）
  meta.json    出图参数、文字路线、质量门结论、被自动改写的词

用法:
    python build_id_prompt.py --spec specs/male-glasses.json --outdir out/male
    python build_id_prompt.py --spec specs/male-glasses.json --check     # 只校验

设计取舍见 references/prompt-formula.md。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------- 常量

# 画幅 → 原生出图尺寸 / 8K 交付尺寸
# 原生尺寸都取 16 的倍数，避免部分接入方对 size 的整除要求。
ASPECTS = {
    "3:4": {"size": "1536x2048", "8k": (5760, 7680), "4k": (2880, 3840)},
    "4:5": {"size": "1600x2000", "8k": (6144, 7680), "4k": (3072, 3840)},
    "2:3": {"size": "1360x2040", "8k": (5120, 7680), "4k": (2560, 3840)},
    "1:1": {"size": "2048x2048", "8k": (7680, 7680), "4k": (3840, 3840)},
}

# 改形词 → 光学等价表述。
# 「保留原图五官样貌」和「精瘦脸型 / 下颌线清晰 / 体脂率 15%」在字面上是冲突的：
# 前一个要求几何不变，后一个要求几何改变。模型同时看到这两句时，会优先执行改形，
# 于是脸被拉成另一种骨相，保脸直接失败。正确做法不是删掉需求，而是把「改形」
# 翻译成「用光把既有的形勾出来」——观众看到的是清晰的下颌，而骨相没动。
FACE_REWRITES = [
    (r"精瘦脸型|瘦脸|小脸|V\s*脸|瓜子脸|尖下巴|削骨|瘦下巴|改脸型|改变脸型",
     "脸型与参考图一致（不改骨相），用光让轮廓显出来"),
    (r"体脂率?\s*\d+\s*%|体脂低|低体脂",
     "皮肤紧致、面部干净无赘肉感（不改变原有脸型）"),
    (r"下颌线清晰|下颌线分明|轮廓分明",
     "下颌线要靠光勾出来（形不变，只是被照亮）"),
    (r"骨相立体|五官立体|立体五官",
     "五官立体感由柔和的明暗过渡建立（不重塑造型）"),
]

# 与「柔和柔光 / 干净高级」直接打架的表述：出现在正面里就一定出事。
# 注意「低饱和」是限制而不是推动，所以用 lookbehind 把它排除掉——
# 否则「低饱和的豆沙色唇」会被报成「推饱和」。
TONE_CONFLICTS = [
    (r"硬光|强反差|高对比|强烈阴影|硬朗阴影|戏剧光", "柔光棚拍里出现硬光要求，阴影会脏"),
    (r"暗调|低调|low-?key|压暗|暗黑", "韩系学院风是高调片，暗调会让肤色发灰"),
    (r"(?<!低)饱和|鲜艳|浓郁", "证件写真的色彩是「干净」不是「浓郁」，推饱和会让人工感变重"),
    (r"磨皮|美颜|无瑕肌|零毛孔|塑料", "磨皮会消掉皮肤纹理，保脸和真实感一起崩"),
]

# 负面词默认表（用户给的 + 这类写真必须补的）
DEFAULT_NEGATIVES = [
    "畸形五官", "脸部不对称", "眼睛大小不一", "牙齿外露", "表情僵硬",
    "模糊", "低画质", "噪点过多", "压缩伪影",
    "水印", "logo", "签名", "二维码", "边框内阴影", "画中画",
    "多余杂物", "背景杂物", "第三人", "重复人物",
    "丑陋", "变形", "拉长脸", "比例失调",
    "手畸形", "多手指", "少手指", "断手",
    "灰暗", "发灰", "脏色", "偏黄", "偏绿",
    "卡通", "动漫", "插画", "3D 渲染", "油画", "夸张滤镜", "塑料皮肤", "过度锐化",
]

# 文字路线阈值：模型能写对的边界
TEXT_LIMITS = {"headline": 24, "sub": 46, "third": 46}

PLACEHOLDER_PAT = re.compile(r"【[^】]*】|\[[^\]]*\]|\bXX+\b|待填|TODO|占位")


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# ---------------------------------------------------------------- 装配辅助

def join(items: list[str], sep: str = "；") -> str:
    return sep.join(x.strip().rstrip("。；") for x in items if x and x.strip())


def sanitize_face(text: str) -> tuple[str, list[str]]:
    """把改形词换成光学表述，返回 (新文本, 被替换的原词)。"""
    hits: list[str] = []
    out = text
    for pat, repl in FACE_REWRITES:
        for m in re.finditer(pat, out):
            hits.append(m.group(0))
        out = re.sub(pat, repl, out)
    return out, hits


def walk_strings(obj, path=""):
    """递归遍历 spec 里所有字符串，产出 (路径, 值)。下划线开头的键是备注，不参与质量门。"""
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).startswith("_"):
                continue
            yield from walk_strings(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk_strings(v, f"{path}[{i}]")


# 否定词：中文里的否定常以「不 / 无 / 没 / 禁」出现，且都在同一个分句内。
# 「不允许出现任何 logo」「不磨皮」这类写法命中了词表，但其实是在排除它，
# 不识别否定就会把最正确的那几句报成矛盾。
NEG_WORDS = ("不", "无", "没", "别", "禁", "避免", "零", "绝", "非")
SPLIT_CHARS = "，。；：\n！？"


def clause_of(text: str, pos: int) -> str:
    """取 pos 所在的那个分句（按中文标点切，不按顿号——顿号常把关键词单独切出来）。"""
    lo = 0
    for i in range(pos - 1, -1, -1):
        if text[i] in SPLIT_CHARS:
            lo = i + 1
            break
    hi = len(text)
    for i in range(pos, len(text)):
        if text[i] in SPLIT_CHARS:
            hi = i
            break
    return text[lo:hi]


def affirmed_matches(text: str, pat: str) -> list[str]:
    """返回 pat 在 text 里「确实被肯定」的匹配（落在否定分句里的不算）。"""
    out = []
    for m in re.finditer(pat, text):
        if not any(w in clause_of(text, m.start()) for w in NEG_WORDS):
            out.append(m.group(0))
    return out


# ---------------------------------------------------------------- 各段

def sec_identity(spec: dict) -> tuple[str, list]:
    issues = []
    sp = spec.get("subject") or {}
    lock = sp.get("face_lock", "strong")
    text = "【身份锁定｜最高优先级】\n"

    if lock == "strong":
        text += (
            "这是一次参考图条件生成，不是「照着描述画一个像的人」。画面里的人必须是参考图里那一个人：\n"
            "· 脸的比例：脸的长宽比、三庭比例、颧骨到下颌的宽度变化，与参考图一致；\n"
            "· 五官位置：眉眼间距、眼裂长度与走向、内外眼角高度差、鼻根高度、鼻翼宽度、"
            "人中长度、唇峰形状与唇厚比例，逐项对齐；\n"
            "· 细节特征：单双眼皮形态、眼袋与卧蚕的有无、痣与印记的位置，保留；\n"
            "· 发际线与发旋位置不改；头发长度可以按造型调整，但从发根长出来的方向要自然。\n"
            "判据：把这张图和参考图并排放在一起，能一眼认出是同一个人，而不是「同类型的两个人」。"
        )
    elif lock == "medium":
        text += (
            "以参考图人物为原型：五官大形与脸的长宽比保持一致，允许发型与妆容做造型化处理，"
            "但不得换成另一种骨相。若这次没有参考图，则按下面的描述建立一个人物，"
            "并在整张图里把这张脸贯彻到底——不要画成「平均脸」。"
        )
    else:
        issues.append(("warn", "W510 face_lock=lax：不锁脸时这张图只是「同风格的另一张写真」，别当成本人交付"))

    extra = sp.get("identity_extra")
    if extra:
        text += "\n" + str(extra).strip()
    return text, issues


def sec_face(spec: dict) -> tuple[str, list, list[str]]:
    """脸与妆。返回 (文本, issues, 被改写的词)。"""
    issues: list[str] = []
    sp = spec.get("subject") or {}
    fx = sp.get("face") or {}

    parts = []
    skin = fx.get("skin")
    if skin:
        parts.append(f"皮肤：{skin}")
    makeup = fx.get("makeup")
    if makeup:
        parts.append(f"妆容：{makeup}")
    eyes = fx.get("eyes")
    if eyes:
        parts.append(f"眼神：{eyes}")
    mouth = fx.get("expression")
    if mouth:
        parts.append(f"表情：{mouth}")

    text = "【脸与妆】" + join(parts) + "。\n"

    shape = fx.get("shape")
    rewritten: list[str] = []
    if shape:
        cleaned, rewritten = sanitize_face(str(shape))
        text += (
            "轮廓要求：" + cleaned.strip().rstrip("。") + "。\n"
            "这条只能通过光来实现：下颌与颈部的交界用一个柔和但方向明确的明暗过渡分开，"
            "受光侧的下颌边缘保留一道细亮线，阴影侧不补光。**不允许拉伸、收窄或重塑脸型**——"
            "脸型一旦被改，这张图就不再是这个人。"
        )
    if rewritten:
        issues.append((
            "info",
            f"I502 已把改形词 {rewritten} 改写为光学表述：保脸模式下不能同时要求改骨相，"
            "否则模型优先执行改形、脸直接变人",
        ))

    glasses = fx.get("glasses")
    if glasses:
        text += (
            f"\n眼镜：{glasses}。镜框不能挡住眼睛的关键信息：瞳孔、双眼皮、眼裂两端都要透过镜片看清；"
            "镜片不反白、不反射屏幕、不出现第二重影子。"
        )
        if sp.get("face_lock", "strong") == "strong":
            issues.append((
                "warn",
                "W506 参考图若不戴眼镜，加眼镜等于在眼睛前面压一层遮挡，保脸精度会下降；"
                "想让眼镜成为固定人设，最好直接用戴眼镜的照片当参考图",
            ))
    return text.rstrip(), issues, rewritten


def sec_hair(spec: dict) -> tuple[str, list]:
    issues = []
    hr = (spec.get("subject") or {}).get("hair") or {}
    if not hr:
        issues.append(("warn", "W511 没写发型：模型会自己选一个，容易和参考图差太远"))
        return "", issues

    bits = []
    if hr.get("cut"):
        bits.append(f"剪裁：{hr['cut']}")
    if hr.get("length"):
        bits.append(f"长度：{hr['length']}")
    if hr.get("texture"):
        bits.append(f"质感：{hr['texture']}")
    if hr.get("detail"):
        bits.append(f"细节：{hr['detail']}")
    if hr.get("keep"):
        bits.append(f"必须保留：{hr['keep']}")

    text = (
        "【发型】" + join(bits) + "。\n"
        "头发要有真实的「发束感」：发根到发梢有粗细与方向的渐变，边缘有少量飞散的碎发，"
        "不要一整片光滑的色块。发际线处能看到细小的绒毛，不要画出刀切一样的硬边。"
    )
    return text, issues


def sec_wardrobe(spec: dict) -> tuple[str, list]:
    issues = []
    wd = spec.get("wardrobe") or {}
    if not wd:
        issues.append(("error", "E501 wardrobe 为空：证件写真一半的信息在制服上，逐件写清楚"))
        return "", issues

    order = ["jacket", "vest", "shirt", "tie", "pin", "accessory"]
    label = {
        "jacket": "外套", "vest": "背心", "shirt": "衬衫",
        "tie": "领带", "pin": "徽章", "accessory": "配饰",
    }
    lines = []
    for key in order + [k for k in wd if k not in order]:
        val = wd.get(key)
        if not val:
            continue
        name = label.get(key, key)
        if isinstance(val, dict):
            desc = join([f"{k}：{v}" for k, v in val.items()], "，")
        else:
            desc = str(val)
        lines.append(f"· {name}：{desc}")

    if not any(k in wd for k in ("jacket", "vest", "shirt")):
        issues.append(("warn", "W512 缺「外套 / 背心 / 衬衫」里的至少一件：学院风的三件套是识别符号，缺了就散"))

    text = "【服装｜逐件固定，不许自由发挥】\n" + "\n".join(lines) + "\n"
    text += (
        "穿法要有真实的层次与厚度：领子下面压出衬衫领的厚度，背心的 V 口边缘有一点点被撑开的形变，"
        "外套袖口若在画面内，要露出一点衬衫袖口的边，"
        "外套的肩线落在肩骨外侧一点。"
        "所有布料的织纹（西装斜纹、针织罗纹、衬衫平纹）在近景里要看得见，不要糊成一块颜色。"
    )
    return text, issues


def sec_pose(spec: dict) -> tuple[str, list]:
    issues = []
    ps = spec.get("pose") or {}
    framing = ps.get("framing", "胸像")
    center = ps.get("center", True)
    shoulder = ps.get("shoulder", "正面平肩，肩膀放松下沉")

    text = (
        f"【姿态与构图】{framing}构图，人物{'水平居中' if center else '按 spec 偏置'}，"
        "身体轴线垂直，不歪头、不侧身卖萌。"
        f"肩线：{shoulder}。"
        "头顶留白约为画面高度的 8–12%，下巴到画面底边留出放文字的空间（≥ 12%）；"
        "两侧肩膀完整在画面内，不切肩。"
        "证件写真的构图逻辑是「正、稳、满」：脸在画面上三分之一，视线落在镜头中心偏上的位置，"
        "直视镜头但不瞪眼。"
    )
    if ps.get("hands"):
        hands = str(ps["hands"])
        text += f"手部：{hands}。"
        if "不入画" not in hands and "不露" not in hands:
            text += "手的姿态要简单——证件写真里手是风险源，能不露就不露。"
    return text, issues


def sec_backdrop(spec: dict) -> tuple[str, list]:
    issues = []
    bg = spec.get("backdrop") or {}
    color = bg.get("color", "浅灰蓝")
    hexv = bg.get("hex")
    tex = bg.get("texture", "磨砂")

    # tex 可能已经是完整短语（「无缝纸的磨砂面，有极细的哑光颗粒」），
    # 也可能是单个名词（「磨砂」）。只有后者才需要补「质感」两个字。
    noun_only = not re.search(r"[，、；]|面|感|纹理|颗粒", tex)
    text = f"【背景】{color}"
    if hexv:
        text += f"（约 {hexv}）"
    text += f"，{tex}" + ("质感" if noun_only else "") + "。\n"

    grad = bg.get("gradient")
    if grad:
        text += f"明暗：{grad}。"
    else:
        text += (
            "明暗：中心略亮、四周极缓地变暗，跨度只占背景亮度的 8–12%（几乎看不出来，但确实有）。"
        )
        issues.append((
            "warn",
            "W502 背景没写明暗渐变：纯平色背景在观众眼里等于「抠图换底」，"
            "高级感的来源恰恰是那层几乎看不出来的渐变",
        ))

    text += (
        "背景必须是有距离感的：人物轮廓边缘有一点点因为景深产生的柔化，"
        "背景上有极细的颗粒（强度相当于 ISO 200 的胶片），不是数码的纯色噪点。"
        "背景上不允许出现任何图案、纹理、logo、光影道具、窗帘褶皱或墙面接缝。"
    )
    if bg.get("vignette") is False:
        text += "不加暗角。"
    return text, issues


def sec_light(spec: dict) -> tuple[str, list]:
    issues = []
    lt = spec.get("light") or {}
    key = lt.get("key", "大面积柔光源，来自人物左前方 45°、略高于眼平")
    ratio = lt.get("ratio", "主辅光比约 1:1.5，暗部不黑，只比亮部低一档")

    text = (
        f"【光线】{key}。光源面积很大（相当于 120cm 柔光箱贴得很近），"
        "所以阴影的边缘是宽的、软的，没有一道硬边。\n"
        f"明暗比：{ratio}。\n"
        "高光落在额头、鼻梁、颧骨的最高点，形状是一个柔和的椭圆而不是一条亮线；"
        "鼻底的影子很短、边缘很虚，说明光是从偏正面过来的；"
        "下颌在颈上投下淡淡一团影子，让脸和脖子分开（这是证件写真显立体的关键，不能没有）。"
        "眼睛里有明确的眼神光：一块方形或圆形的柔光斑，位置一致、大小相同——"
        "眼神光不对，脸再准也像假人。"
    )
    return text, issues


def sec_tone(spec: dict) -> tuple[str, list]:
    issues = []
    tn = spec.get("tone") or {}
    temp = tn.get("temp", 5400)
    text = (
        f"【影调与色彩】高调（high-key）影调，白位接近纯白但不过曝，"
        f"黑位抬到 8–18 之间——暗部是「深灰」不是「死黑」。"
        f"整体色温 {temp}K 偏冷，白衬衫在这个色温下是中性白，不发黄。\n"
        "肤色是这张图最不能出错的地方：皮肤保持中性偏粉的暖调，"
        "明度高于背景一档，饱和度低于背景里任何一块颜色（肤色一艳，人就假）。"
        "不允许把皮肤推成橘黄、不允许整体加暖色滤镜、不允许 Duotone 或青橙分离。\n"
        "胶片感来自两件事：高光肩部是软的（最亮处缓慢滚落，不是一刀切），"
        "以及极轻的全色颗粒（可见但绝不影响皮肤质感）。"
    )
    return text, issues


def sec_text(spec: dict, route: str) -> tuple[str, list]:
    """画面底部文字排版。route=model 让模型写；route=overlay 只留空白带。"""
    issues = []
    tx = spec.get("typeset") or {}
    head = str(tx.get("headline", "")).strip()
    sub = str(tx.get("sub", "")).strip()
    third = str(tx.get("third", "")).strip()

    if route == "overlay":
        text = (
            "【画面底部留白】画面最下方 6–18% 是一条干净的留白带（背景延续，没有人物、没有衣领、"
            "没有阴影）。**这一段不要写任何文字、数字、符号、线条或装饰**——"
            "所有排版由后期叠加，模型只需要把这块地方空出来。"
        )
        return text, issues

    blocks = []
    if head:
        blocks.append(f"第一行（主标题）：全部大写的「{head}」，字号约为画面高度的 3%，字距加宽（tracking 约 12%），细衬线体，白色")
    if sub:
        blocks.append(f"第二行（副标题）：小写或全大写的「{sub}」，字号约为画面高度的 1.1%，字距更宽（tracking 约 22%），与主标题同一套字体")
    if third:
        blocks.append(f"第三行（页脚小字）：「{third}」，字号约 0.9%，同色，位置在最下方")

    if not blocks:
        issues.append(("error", "E505 typeset 为空但 route=model：要么给文字，要么走 overlay"))
        return "", issues

    text = (
        "【画面底部文字排版】\n" + "\n".join("· " + b for b in blocks) + "\n"
        "排版规则（比文字内容更容易画错，逐条遵守）：\n"
        "· 所有文字左对齐或居中对齐，只能选一种，每一行共用同一条对齐基准线；\n"
        "· 字号只有三档，同档字号必须完全一致，不允许逐行随意缩放；\n"
        "· 字距必须是均匀加宽的宽字距排法，不是把字母拉开填空隙；\n"
        "· 字体只有一套，全部用细衬线体（类似 Didot / Bodoni 的现代衬线），"
        "笔画有粗细对比，不要用无衬线、不要用圆体、不要用手写体；\n"
        "· 文字是纯白或接近纯白，不描边、不发光、不加投影、不加色块底；\n"
        "· 文字整体位于画面底部留白带内，横向居中，与底边距离约为画面高度的 4%；\n"
        "· 拼写必须逐字正确，字母数量与给定字符串完全一致，绝不允许漏字母、多字母、"
        "把单词连在一起、或自行添加任何未指定的词。\n"
        "如果无法准确排出一行文字，宁可不写，也不要用似是而非的字母凑数。"
    )
    return text, issues


def sec_border(spec: dict) -> tuple[str, list]:
    issues = []
    fr = spec.get("frame") or {}
    border = fr.get("border", True)
    if not border:
        return "【画幅】满幅画面，不留边、不加框。", issues

    issues.append((
        "info",
        "I501 白边框不由模型绘制：模型画的框宽度随机、四边不等、还会多出一圈灰相纸。"
        "提示词里只要求四周留出安全边，边框由 finalize.py 按精确像素加上",
    ))
    text = (
        "【画面边缘】**不要在画面里画任何边框、白边、相纸、内阴影、投影或拼贴效果**，"
        "把整幅画面画满。但构图时四周留出约 3% 的安全边（人物与重要元素不贴到边缘），"
        "因为后期会在外面加一圈精确等宽的白边——你现在画的任何框都会和后期那圈打架。"
    )
    return text, issues


def sec_quality(spec: dict) -> tuple[str, list]:
    """画质。注意：交付口径（8K 是重采样）是给人看的，不写进提示词——
    模型看到「重采样」「不承诺原生」这类词，会把画面往「糊一点也没关系」的方向带。
    交付口径放在 meta.json 的 deliver_8k 里。
    """
    sp = spec.get("subject") or {}
    has_glasses = bool((sp.get("face") or {}).get("glasses"))
    lens = "、镜片边缘有干净的折射" if has_glasses else ""
    return (
        "【画质】超写实人像摄影，中画幅数码后背的解析力：睫毛一根一根分开、"
        f"眼白上的微小血丝可见{lens}、布料织纹在近景里清晰。"
        "皮肤保留真实纹理（毛孔、细小的绒毛、极淡的肤色不均），但没有任何痘印、油光、瑕疵斑块。"
        "全画面没有锐化过度的白边、没有 AI 感的塑料平滑、没有局部重复的纹理。",
        [],
    )


def sec_negative(spec: dict, negs: list[str]) -> tuple[str, list]:
    return (
        "【禁止项】" + "、".join(negs) + "。\n"
        "以上任何一项出现即视为废片，不是「可以接受的小瑕疵」。",
        [],
    )


# ---------------------------------------------------------------- 质量门

def build_issues(spec: dict, prompt: str, negs: list[str], route: str) -> list[tuple[str, str]]:
    issues: list[tuple[str, str]] = []
    sp = spec.get("subject") or {}
    fr = spec.get("frame") or {}
    tx = spec.get("typeset") or {}

    # 占位符
    for path, val in walk_strings(spec):
        m = PLACEHOLDER_PAT.search(val)
        if m:
            issues.append(("error", f"E502 {path} 里残留占位符 {m.group(0)}：要么替换成真值，要么删掉这一项"))

    # 比例
    aspect = fr.get("aspect", "3:4")
    if aspect not in ASPECTS:
        issues.append(("error", f"E503 不支持的比例 {aspect}，可选 {list(ASPECTS)}"))
    size = (fr.get("size") or "").strip()
    if size and aspect in ASPECTS and size != ASPECTS[aspect]["size"]:
        issues.append((
            "warn",
            f"W503 size={size} 与比例 {aspect} 的推荐值 {ASPECTS[aspect]['size']} 不一致，"
            "出图前改回推荐值，否则后期加边框会歪",
        ))

    # 改形词残留在任何地方都算硬错
    for path, val in walk_strings(spec):
        for pat, _ in FACE_REWRITES:
            if re.search(pat, val) and sp.get("face_lock", "strong") == "strong":
                if not path.startswith("subject.face.shape"):
                    issues.append((
                        "error",
                        f"E504 {path} 里有改形表述：保脸模式下它和「保留五官样貌」互斥，"
                        "模型会优先改形。请只在 subject.face.shape 里写，脚本会自动改写",
                    ))
                break

    # 影调冲突（否定分句里的不算：「不磨皮」「不加暖色滤镜」是对的）
    for path, val in walk_strings(spec):
        if path.startswith("negative"):
            continue
        for pat, why in TONE_CONFLICTS:
            hits = affirmed_matches(val, pat)
            if hits:
                issues.append(("warn", f"W504 {path} 命中「{hits[0]}」：{why}"))

    # 文字
    head = str(tx.get("headline", "")).strip()
    sub = str(tx.get("sub", "")).strip()
    third = str(tx.get("third", "")).strip()
    if route == "model":
        if len(head) > TEXT_LIMITS["headline"]:
            issues.append(("error", f"E506 主标题 {len(head)} 字符超过 {TEXT_LIMITS['headline']}，模型一定排乱，改用 overlay"))
        if len(sub) > TEXT_LIMITS["sub"]:
            issues.append(("error", f"E507 副标题 {len(sub)} 字符超过 {TEXT_LIMITS['sub']}，模型一定排乱，改用 overlay"))
        if len(third) > TEXT_LIMITS["third"]:
            issues.append(("error", f"E508 第三行 {len(third)} 字符超过 {TEXT_LIMITS['third']}，模型一定排乱，改用 overlay"))
        for label, val in (("headline", head), ("sub", sub), ("third", third)):
            if val and re.search(r"[\u4e00-\u9fff]", val):
                issues.append((
                    "warn",
                    f"W507 {label} 里有中文字：模型写中文的成功率远低于英文，"
                    "且小字号下必然糊成墨块，建议改成英文或走 overlay",
                ))

    # 负面词自相矛盾：负面词本身出现在正面里。
    # 注意要先切掉整个【禁止项】段——否则每个负面词都在「自己那一段」里被找到，
    # 变成 100% 误报。
    low_prompt = prompt.split("【禁止项】")[0]
    for n in negs:
        core = n.strip()
        if len(core) >= 2 and affirmed_matches(low_prompt, re.escape(core)):
            issues.append(("warn", f"W505 负面词「{core}」同时出现在正面描述里，模型会信号打架"))

    # 有参考图却没写肤色条款
    if sp.get("ref_image") and "肤色" not in prompt:
        issues.append(("error", "E509 有参考图但提示词里没有肤色条款：肤色漂移是保脸失败最常见的表现"))

    return issues


# ---------------------------------------------------------------- 主流程

def assemble(spec: dict) -> tuple[str, str, dict]:
    sp = spec.get("subject") or {}
    fr = spec.get("frame") or {}
    tx = spec.get("typeset") or {}
    aspect = fr.get("aspect", "3:4")

    # 文字路线：字数在模型能力内 → model；否则 → overlay
    head = str(tx.get("headline", "")).strip()
    sub = str(tx.get("sub", "")).strip()
    third = str(tx.get("third", "")).strip()
    want_text = bool(head or sub or third)
    route = str(tx.get("route", "auto"))
    if route == "auto":
        over = (
            len(head) > TEXT_LIMITS["headline"]
            or len(sub) > TEXT_LIMITS["sub"]
            or len(third) > TEXT_LIMITS["third"]
            or bool(re.search(r"[\u4e00-\u9fff]", head + sub + third))
        )
        route = "overlay" if (want_text and over) else ("model" if want_text else "none")

    # 负面词：默认表 + spec 追加，去重保序
    negs: list[str] = []
    for n in list(spec.get("negative_extra") or []):
        n = str(n).strip()
        if n and n not in negs:
            negs.append(n)
    for n in DEFAULT_NEGATIVES:
        if n not in negs:
            negs.append(n)

    sections: list[str] = []
    issues: list[tuple[str, str]] = []

    task = (
        "【任务】生成一张韩系学院风证件写真（Korean preppy ID portrait）："
        + ("参考图里的这个人，" if sp.get("ref_image") else "下面描述的这个年轻人，")
        + "穿学院制服，在影棚里拍一张正、稳、干净的胸像。"
        "不是时尚大片，不是生活照，是那种会印在入学册、练习生档案、毕业纪念册上的照片——"
        "克制、通透、经得起放大看。"
    )
    sections.append(task)

    for fn in (sec_identity, sec_face, sec_hair, sec_wardrobe,
               sec_pose, sec_backdrop, sec_light, sec_tone):
        out = fn(spec)
        text, iss = out[0], list(out[1])
        if text:
            sections.append(text)
        issues.extend((lv, m) for lv, m in iss)

    # 文字 / 画幅 / 画质 / 禁止项
    for text, iss in (
        sec_text(spec, route),
        sec_border(spec),
        sec_quality(spec),
        sec_negative(spec, negs),
    ):
        if text:
            sections.append(text)
        issues.extend(iss)

    prompt = "\n\n".join(sections)
    gate = build_issues(spec, prompt, negs, route)

    # 去重 issues（同一句只留一次）
    seen = set()
    merged: list[tuple[str, str]] = []
    for lv, msg in issues + gate:
        if msg in seen:
            continue
        seen.add(msg)
        merged.append((lv, msg))

    order = {"error": 0, "warn": 1, "info": 2}
    merged.sort(key=lambda t: order.get(t[0], 3))

    aspect_info = ASPECTS.get(aspect, ASPECTS["3:4"])
    border_frac = float(fr.get("border_frac", 0.015))
    meta = {
        "route_text": route,
        "aspect": aspect,
        "size": fr.get("size") or aspect_info["size"],
        "deliver_8k": list(aspect_info["8k"]),
        "deliver_4k": list(aspect_info["4k"]),
        "border": bool(fr.get("border", True)),
        "border_frac": border_frac,
        "border_px_8k": round(min(aspect_info["8k"]) * border_frac) if fr.get("border", True) else 0,
        "negative_count": len(negs),
        "imagegen": {
            "image1": "<参考图绝对路径｜身份来源，必须有>",
            "input_fidelity": "high",
            "revise": False,
            "footnote": "<不传：该字段就是水印文本>",
            "quality": "high",
            "size": fr.get("size") or aspect_info["size"],
            "output_dir": "out/<name>/raw",
        },
        "next_steps": [],
        "issues": [{"level": lv, "code": m.split(" ")[0], "message": m} for lv, m in merged],
    }
    if route == "overlay":
        meta["next_steps"].append(
            "出图后跑 scripts/settype.py 叠文字：主标题 %r" % head
        )
    if meta["border"]:
        meta["next_steps"].append(
            "finalize.py --target 8k --border %s  （白边框由后期按精确像素加，不靠模型画）"
            % border_frac
        )
    if route == "overlay" and meta["border"]:
        meta["next_steps"].append(
            "顺序必须是 finalize → settype：先加边框再叠字，字才会落在框内"
        )
    return prompt, "\n".join(negs), meta


def main() -> int:
    ap = argparse.ArgumentParser(description="韩系学院风证件写真提示词装配器")
    ap.add_argument("--spec", required=True, help="spec JSON 路径")
    ap.add_argument("--outdir", default="out", help="产物目录")
    ap.add_argument("--check", action="store_true", help="只校验，不写文件")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    spec_path = Path(args.spec)
    if not spec_path.exists():
        raise SystemExit(f"[in] spec 不存在：{spec_path}")
    spec = json.loads(spec_path.read_text(encoding="utf-8"))

    prompt, negative, meta = assemble(spec)

    errors = [i for i in meta["issues"] if i["level"] == "error"]
    warns = [i for i in meta["issues"] if i["level"] == "warn"]
    infos = [i for i in meta["issues"] if i["level"] == "info"]

    if not args.check:
        outdir = Path(args.outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "prompt.txt").write_text(prompt, encoding="utf-8")
        (outdir / "negative.txt").write_text(negative, encoding="utf-8")
        (outdir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    print(f"spec: {spec_path.as_posix()}")
    print(f"route_text: {meta['route_text']}  aspect: {meta['aspect']}  size: {meta['size']}")
    print(f"deliver: {meta['deliver_8k'][0]}x{meta['deliver_8k'][1]} (8K)  border: {meta['border']}")
    print(f"prompt: {len(prompt)} chars, {prompt.count('【')} sections; negative: {meta['negative_count']} terms")
    for grp, tag in ((errors, "ERROR"), (warns, "WARN"), (infos, "INFO")):
        for i in grp:
            print(f"  [{tag}] {i['message']}")
    if not args.check:
        print(f"written: {Path(args.outdir).as_posix()}/prompt.txt, negative.txt, meta.json")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
