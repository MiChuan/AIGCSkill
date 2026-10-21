#!/usr/bin/env python3
"""把「观察」编译成「提示词」——这个 skill 里唯一有创作成分的一步，也是最容易滑掉的一步。

滑掉的方式只有一种：把形容词当结论。所以这个脚本对 spec 做的是**苛刻校验**，
不是宽松渲染：每条风格规格都必须带原文证据 + 可检验判据，出现「文笔细腻」
「富有感染力」这类没有落点的词直接报错退出，不许生成。

三档产物用途不同：
    short     140 字以内，塞进 system prompt 或对话开头；
    standard  七段式，规格 / 岔路 / 负向 / 自检齐全，不含原文句子；
    anchored  standard + 1–3 句语感锚句，最像但也最容易被抄句子。

纯标准库，零依赖。

用法：
    python scripts/build_prompt.py --spec specs/demo.json --outdir out/demo
    python scripts/build_prompt.py --spec specs/demo.json --check      # 只校验
    python scripts/build_prompt.py --spec specs/demo.json --stdout     # 打印标准版
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

VERSION = "1.0.0"

AXES = {
    "structure": "结构",
    "syntax": "句法",
    "lexicon": "词汇",
    "imagery": "意象",
    "pragmatics": "语用",
    "typography": "排版",
    "tone": "语气",
}

CONFIDENCE = {"high", "medium", "low"}

# 这些词不是风格，是读后感。出现它们就必须有数字或对照结构兜底。
EMPTY_WORDS = [
    "优美", "细腻", "生动", "感染力", "文采", "流畅", "大气", "高级感", "有深度",
    "耐读", "雅致", "灵气", "张力", "功底", "行云流水", "娓娓道来", "引人入胜",
    "回味", "共鸣", "独特", "丰富", "精准", "恰到好处", "别具一格", "匠心", "鲜活",
    "自然", "朴实", "深刻", "有力", "克制", "高级",
]

# 出现这些，说明这条规格给出了可执行的方向，不算空话
CONTRAST_WORDS = ["不", "避免", "而非", "代替", "别", "少用", "禁用", "拒绝", "改用", "换成"]

NUM_RE = re.compile(r"\d")
SENT_END = "。！？；：…"


class SpecError(Exception):
    pass


# ---------------------------------------------------------------- 校验

def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise SpecError(msg)


def _text(val, field: str, min_len: int = 1, max_len: int = 400) -> str:
    _require(isinstance(val, str), f"{field} 必须是字符串")
    s = val.strip()
    _require(len(s) >= min_len, f"{field} 太短（至少 {min_len} 字）：{s!r}")
    _require(len(s) <= max_len, f"{field} 太长（最多 {max_len} 字）：{s[:40]}…")
    return s


def is_empty_claim(rule: str) -> bool:
    """只有形容词、没有落点，且没有给出负向或替代方向 -> 空话。"""
    hits = [w for w in EMPTY_WORDS if w in rule]
    if not hits:
        return False
    if NUM_RE.search(rule) or any(w in rule for w in CONTRAST_WORDS):
        return False
    stripped = rule
    for w in hits:
        stripped = stripped.replace(w, "")
    return len(stripped.strip(SENT_END + " 　")) < 8


def validate(spec) -> list[str]:
    """通过则返回 warnings；不通过抛 SpecError。"""
    warnings: list[str] = []
    _require(isinstance(spec, dict), "spec 顶层必须是对象")

    meta = spec.get("meta")
    _require(isinstance(meta, dict), "缺少 meta")
    _text(meta.get("name"), "meta.name", 2, 40)
    conf = meta.get("confidence", "medium")
    _require(conf in CONFIDENCE, f"meta.confidence 只能是 {sorted(CONFIDENCE)}，拿到 {conf!r}")

    ident = spec.get("identity")
    _require(isinstance(ident, dict), "缺少 identity")
    _text(ident.get("task"), "identity.task", 4, 300)
    if ident.get("role"):
        _text(ident["role"], "identity.role", 2, 200)

    rows = spec.get("specs")
    _require(isinstance(rows, list), "specs 必须是数组")
    _require(len(rows) >= 3, f"风格规格至少要 3 条（现在 {len(rows)} 条），少于 3 条不足以构成风格")
    _require(len(rows) <= 12, f"风格规格最多 12 条（现在 {len(rows)} 条），再多就没人执行了")
    for i, row in enumerate(rows, 1):
        tag = f"specs[{i}]"
        _require(isinstance(row, dict), f"{tag} 必须是对象")
        axis = row.get("axis")
        _require(axis in AXES, f"{tag}.axis 只能是 {sorted(AXES)}，拿到 {axis!r}")
        rule = _text(row.get("rule"), f"{tag}.rule", 8, 160)
        if is_empty_claim(rule):
            raise SpecError(
                f"{tag}.rule 是空话：{rule!r}。"
                "「优美 / 细腻 / 有张力」这类词没有落点，改成可检验的写法："
                "给出数量、给出对照（用 A 不用 B），或改成负向约束。")
        if NUM_RE.search(rule) or any(w in rule for w in CONTRAST_WORDS):
            pass
        else:
            warnings.append(f"{tag}.rule 没有数字也没有对照，可能仍然不可执行：{rule}")
        _text(row.get("evidence"), f"{tag}.evidence", 4, 200)
        check = _text(row.get("check"), f"{tag}.check", 4, 200)
        if not (NUM_RE.search(check) or any(w in check for w in CONTRAST_WORDS + ["每", "至少", "至多"])):
            warnings.append(f"{tag}.check 不像可检验判据（没有数量或有无判断）：{check}")
        if row.get("brief"):
            _text(row["brief"], f"{tag}.brief", 2, 40)

    choices = spec.get("choices") or []
    _require(isinstance(choices, list), "choices 必须是数组")
    _require(len(choices) <= 8, "choices 最多 8 条")
    for i, row in enumerate(choices, 1):
        tag = f"choices[{i}]"
        _require(isinstance(row, dict), f"{tag} 必须是对象")
        for key in ("when", "use", "avoid"):
            _text(row.get(key), f"{tag}.{key}", 2, 120)

    neg = spec.get("negative") or {}
    _require(isinstance(neg, dict), "negative 必须是对象")
    words = neg.get("words") or []
    _require(isinstance(words, list), "negative.words 必须是数组")
    for i, w in enumerate(words, 1):
        s = _text(w, f"negative.words[{i}]", 1, 10)
        _require(not re.search(r"[，。；！？,.;!?]", s),
                 f"negative.words[{i}] 只能是词，不能是句子：{s!r}")
    for key in ("patterns", "topics"):
        items = neg.get(key) or []
        _require(isinstance(items, list), f"negative.{key} 必须是数组")
        for i, w in enumerate(items, 1):
            _text(w, f"negative.{key}[{i}]", 2, 120)

    anchors = spec.get("anchors") or []
    _require(isinstance(anchors, list), "anchors 必须是数组")
    _require(len(anchors) <= 3, f"anchors 最多 3 句（现在 {len(anchors)} 句），多了会变成让人抄")
    for i, a in enumerate(anchors, 1):
        _text(a, f"anchors[{i}]", 4, 200)

    checks = spec.get("selfcheck") or []
    _require(isinstance(checks, list), "selfcheck 必须是数组")
    for i, c in enumerate(checks, 1):
        _text(c, f"selfcheck[{i}]", 4, 120)

    metrics = spec.get("metrics") or {}
    _require(isinstance(metrics, dict), "metrics 必须是对象")
    for k, v in metrics.items():
        _require(isinstance(v, (int, float)), f"metrics.{k} 必须是数字")
    return warnings


# ---------------------------------------------------------------- 渲染

def pick_brief(row: dict) -> str:
    if row.get("brief"):
        return row["brief"].strip().rstrip("。")
    body = re.split(r"[。；]", row["rule"])[0].strip()
    return body


def render_short(spec: dict) -> str:
    """短版只留骨架：角色 + 任务首句 + 3 条要求 + 5 个禁用词 + 1 条自检。"""
    ident = spec["identity"]
    task = re.split(r"[。：:]", ident["task"])[0].strip() or ident["task"]
    briefs = [pick_brief(r) for r in spec["specs"][:3]]
    words = (spec.get("negative", {}).get("words") or [])[:5]
    checks = (spec.get("selfcheck") or [])[:1]
    parts = []
    if ident.get("role"):
        parts.append(ident["role"].rstrip("。") + "。")
    parts.append(task.rstrip("。") + "。")
    parts.append("要求：" + "；".join(briefs) + "。")
    if words:
        parts.append("不要出现：" + "、".join(words) + "。")
    if checks:
        parts.append(checks[0].rstrip("。") + "。")
    text = "".join(parts)
    if len(text) > 180:
        briefs = briefs[:2]
        text = ((ident.get("role") or "").rstrip("。") + "。" if ident.get("role") else "")
        text += task.rstrip("。") + "。要求：" + "；".join(briefs) + "。"
        if words:
            text += "不要出现：" + "、".join(words[:3]) + "。"
    return text


def render_spec_table(spec: dict) -> list[str]:
    out = ["| # | 轴 | 要求 | 原文证据 | 怎么算过 |", "| --- | --- | --- | --- | --- |"]
    for i, row in enumerate(spec["specs"], 1):
        ev = row["evidence"].replace("|", "\\|").replace("\n", " ")
        rule = row["rule"].replace("|", "\\|").replace("\n", " ")
        check = row["check"].replace("|", "\\|").replace("\n", " ")
        out.append(f"| {i} | {AXES[row['axis']]} | {rule} | {ev} | {check} |")
    return out


def render_metrics(spec: dict) -> list[str]:
    metrics = spec.get("metrics") or {}
    if not metrics:
        return []
    labels = {
        "sentence_mean": ("句均长", "字"),
        "sentence_short_ratio": ("短句率", ""),
        "paragraph_sent_mean": ("段均句数", "句"),
        "de_per_kchar": ("的地得密度", "/千字"),
        "comma_per_kchar": ("逗号密度", "/千字"),
        "dash_per_kchar": ("破折号密度", "/千字"),
        "first_person_per_kchar": ("第一人称", "/千字"),
        "quote_ratio": ("引语占比", ""),
        "avg_line_chars": ("行均长", "字"),
    }
    out = ["| 指标 | 目标 |", "| --- | --- |"]
    for key, val in metrics.items():
        label, unit = labels.get(key, (key, ""))
        shown = f"{val}{unit}" if unit else f"{val}"
        out.append(f"| {label} | {shown} |")
    return out


def render_standard(spec: dict, with_anchors: bool) -> str:
    ident = spec["identity"]
    meta = spec["meta"]
    neg = spec.get("negative") or {}
    lines: list[str] = []
    title = "标准版（含语感锚句）" if with_anchors else "标准版（不含原文句子）"
    lines.append(f"# 风格提示词 · {meta['name']} · {title}")
    lines.append("")
    lines.append(f"> 逆向自 {meta.get('source', '—')}，置信度 {meta.get('confidence', 'medium')}。"
                 "规格逐条执行，交付前跑一遍自检。")
    lines.append("")

    lines.append("## 1 角色")
    lines.append(ident.get("role") or "你是一位按规格写作的写作者，不以个人风格覆盖给定规格。")
    lines.append("")

    lines.append("## 2 任务")
    lines.append(ident["task"])
    lines.append("")

    lines.append("## 3 风格规格")
    lines.append(f"{len(spec['specs'])} 条，逐条执行。每条都附原文证据与验收判据——"
                 "判据不成立的段落重写，不要解释。")
    lines.append("")
    lines.extend(render_spec_table(spec))
    lines.append("")

    choices = spec.get("choices") or []
    if choices:
        lines.append("## 4 岔路选择")
        lines.append("风格不是形容词，是在每个岔路口的一致选择。遇到下列情形，按此取舍。")
        lines.append("")
        lines.append("| 当 | 用 | 不用 |")
        lines.append("| --- | --- | --- |")
        for row in choices:
            when = row["when"].replace("|", "\\|")
            use = row["use"].replace("|", "\\|")
            avoid = row["avoid"].replace("|", "\\|")
            lines.append(f"| {when} | {use} | {avoid} |")
        lines.append("")

    lines.append("## 5 负向约束")
    if neg.get("words"):
        lines.append("- 禁用词（一个都不许出现）：" + "、".join(neg["words"]))
    if neg.get("patterns"):
        lines.append("- 禁用结构：")
        for p in neg["patterns"]:
            lines.append(f"  - {p}")
    if neg.get("topics"):
        lines.append("- 不写的内容：" + "、".join(neg["topics"]))
    if not (neg.get("words") or neg.get("patterns") or neg.get("topics")):
        lines.append("- 无（若这一栏是空的，说明负向特征没提炼出来，回去看 references/02-anti-empty.md）")
    lines.append("")

    if with_anchors:
        lines.append("## 6 语感锚点")
        lines.append("只学语感，不抄句子：句式、停顿、用词层级照此对齐，内容换成新主题。")
        lines.append("")
        for i, a in enumerate(spec.get("anchors") or [], 1):
            lines.append(f"{i}. {a}")
        if not (spec.get("anchors") or []):
            lines.append("（无锚句。若要让模型更贴，请补 1–3 句原文，但同一句不得超过 30 字。）")
        lines.append("")

    checks = spec.get("selfcheck") or []
    lines.append(f"## {7 if with_anchors else 6} 交稿前自检")
    lines.append("逐条勾，勾不上就重写，不要交付后再解释。")
    lines.append("")
    for c in checks:
        lines.append(f"- [ ] {c}")
    extra = render_metrics(spec)
    if extra:
        lines.append("")
        lines.append("量化目标（原文本实测，允许 ±15% 偏差）：")
        lines.append("")
        lines.extend(extra)
    lines.append("")

    lines.append("---")
    lines.append("生成器 text-style-to-prompt v" + VERSION)
    return "\n".join(lines)


def normalise(spec: dict) -> dict:
    neg = spec.get("negative") or {}
    return {
        "meta": {
            "name": spec["meta"]["name"].strip(),
            "source": spec["meta"].get("source", ""),
            "confidence": spec["meta"].get("confidence", "medium"),
            "generator": f"text-style-to-prompt/{VERSION}",
        },
        "identity": {
            "role": (spec["identity"].get("role") or "").strip(),
            "task": spec["identity"]["task"].strip(),
        },
        "specs": [
            {
                "axis": r["axis"],
                "rule": r["rule"].strip(),
                "brief": (r.get("brief") or "").strip(),
                "evidence": r["evidence"].strip(),
                "check": r["check"].strip(),
            }
            for r in spec["specs"]
        ],
        "choices": [
            {"when": c["when"].strip(), "use": c["use"].strip(), "avoid": c["avoid"].strip()}
            for c in (spec.get("choices") or [])
        ],
        "negative": {
            "words": [w.strip() for w in (neg.get("words") or [])],
            "patterns": [p.strip() for p in (neg.get("patterns") or [])],
            "topics": [t.strip() for t in (neg.get("topics") or [])],
        },
        "anchors": [a.strip() for a in (spec.get("anchors") or [])],
        "selfcheck": [c.strip() for c in (spec.get("selfcheck") or [])],
        "metrics": dict(spec.get("metrics") or {}),
    }


def build(spec: dict) -> dict:
    clean = normalise(spec)
    return {
        "short": render_short(clean),
        "standard": render_standard(clean, with_anchors=False),
        "anchored": render_standard(clean, with_anchors=True),
    }


def load(path: str) -> dict:
    p = Path(path)
    if not p.is_file():
        raise SpecError(f"找不到 spec：{path}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SpecError(f"{path} 不是合法 JSON：{exc}") from exc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="spec JSON -> 三档风格提示词（严格校验，空话直接报错）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python build_prompt.py --spec specs/demo.json --check\n"
               "  python build_prompt.py --spec specs/demo.json --outdir out/demo\n"
               "  python build_prompt.py --spec specs/demo.json --fmt short --stdout")
    ap.add_argument("--spec", required=True, help="风格规格 JSON")
    ap.add_argument("--outdir", help="产物目录：prompt-short.txt / prompt-standard.md / prompt-anchored.md / prompt.json")
    ap.add_argument("--check", action="store_true", help="只校验，不生成")
    ap.add_argument("--fmt", choices=["short", "standard", "anchored", "all"], default="all")
    ap.add_argument("--stdout", action="store_true", help="打印到标准输出")
    args = ap.parse_args(argv)

    try:
        spec = load(args.spec)
        warnings = validate(spec)
    except SpecError as exc:
        print(f"[build_prompt] 校验未通过：{exc}", file=sys.stderr)
        return 2

    for w in warnings:
        print(f"[build_prompt] 提示：{w}", file=sys.stderr)

    if args.check:
        n_specs = len(spec["specs"])
        print(f"校验通过：{n_specs} 条规格，"
              f"{(spec.get('choices') or []) and len(spec['choices']) or 0} 条岔路，"
              f"{len((spec.get('negative') or {}).get('words') or [])} 个禁用词")
        return 0

    outputs = build(spec)
    if args.stdout:
        key = "standard" if args.fmt == "all" else args.fmt
        print(outputs[key])

    if args.outdir:
        out = Path(args.outdir)
        out.mkdir(parents=True, exist_ok=True)
        files = {
            "short": ("prompt-short.txt", outputs["short"] + "\n"),
            "standard": ("prompt-standard.md", outputs["standard"] + "\n"),
            "anchored": ("prompt-anchored.md", outputs["anchored"] + "\n"),
        }
        for key, (name, body) in files.items():
            if args.fmt in ("all", key):
                (out / name).write_text(body, encoding="utf-8")
        (out / "prompt.json").write_text(
            json.dumps(normalise(spec), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已生成：{out}（{args.fmt}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
