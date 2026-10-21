#!/usr/bin/env python3
"""复刻验收：两份风格指纹差多少，够不够像。

这是整个 skill 里唯一「不许自说自话」的一步。写完提示词之后，用提示词重新写
一段，再拿新文本去和原文本比；比不过就是提示词没写对，改提示词，不是改结论。

七个轴各自打分，再按权重合成总分。分数是**启发式**，量的是一致性，不是好坏：
它只能回答「像不像」，不能回答「写得好不好」。所以阈值只用来卡「不能明显不像」，
过线不等于合格作品。

纯标准库，零依赖。

用法：
    python scripts/style_compare.py --ref out/ref.json --cand out/cand.json
    python scripts/style_compare.py --ref a.json --cand b.json --min 80 --axis-min 60
    python scripts/style_compare.py --ref a.json --cand b.json --json out/cmp.json

退出码：0 过线；1 未过线；2 输入有问题。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

VERSION = "1.0.0"

# (轴 key, 中文名, 权重, [(字段路径, 相对差下限, 中文名)])
AXES = [
    ("sentence", "句法节奏", 0.22, [
        ("sentence.mean", 5.0, "句均长"),
        ("sentence.p90", 10.0, "p90 句长"),
        ("sentence.short_ratio", 0.05, "短句率"),
        ("sentence.long_ratio", 0.05, "长句率"),
        ("sentence.stdev", 3.0, "句长波动"),
    ]),
    ("paragraph", "段落推进", 0.16, [
        ("paragraph.sent_mean", 1.0, "段均句数"),
        ("paragraph.single_sent_ratio", 0.05, "单句成段率"),
        ("paragraph.chars_mean", 20.0, "段均字数"),
    ]),
    ("punct", "标点习惯", 0.16, [
        ("punct.comma", 5.0, "逗号/千字"),
        ("punct.full_stop", 5.0, "句号/千字"),
        ("punct.semicolon", 3.0, "分号/千字"),
        ("punct.dash", 3.0, "破折号/千字"),
        ("punct.excl", 3.0, "感叹号/千字"),
        ("punct.quest", 3.0, "问号/千字"),
        ("punct.ellipsis", 3.0, "省略号/千字"),
    ]),
    ("person", "人称姿态", 0.10, [
        ("person.first_ratio", 2.0, "第一人称/千字"),
        ("person.second_ratio", 2.0, "第二人称/千字"),
        ("person.third_ratio", 2.0, "第三人称/千字"),
        ("person.honorific_ratio", 1.0, "敬称您/千字"),
    ]),
    ("lexicon", "用词密度", 0.20, [
        ("lexicon.ttr", 0.05, "字种比"),
        ("lexicon.de_per_kchar", 5.0, "的地得/千字"),
        ("lexicon.function_per_kchar", 10.0, "虚词/千字"),
        ("lexicon.bigram_repeat_ratio", 0.05, "二元重复率"),
        ("lexicon.digit_per_kchar", 3.0, "数字/千字"),
        ("lexicon.latin_per_kchar", 3.0, "西文/千字"),
    ]),
    ("dialog", "引语占比", 0.06, [
        ("dialog.quote_ratio", 0.03, "引号内占比"),
        ("dialog.quote_mean_len", 5.0, "引语均长"),
    ]),
    ("layout", "版面形态", 0.10, [
        ("layout.avg_line_chars", 5.0, "行均长"),
        ("layout.blank_line_ratio", 0.02, "空行率"),
        ("layout.headings", 2.0, "标题行数"),
        ("layout.list_lines", 2.0, "列表行数"),
        ("layout.emoji", 1.0, "emoji 数"),
    ]),
]


class CompareError(Exception):
    pass


def dig(data: dict, path: str):
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise CompareError(f"指纹缺少字段 {path}，可能不是 style_probe 的输出")
        cur = cur[part]
    if not isinstance(cur, (int, float)):
        raise CompareError(f"字段 {path} 不是数字：{cur!r}")
    return float(cur)


def rel_diff(a: float, b: float, floor: float) -> float:
    """相对差，归一到 0..1。floor 防止 0.001 vs 0.002 这类无关小值被判成天差地别。"""
    return min(1.0, abs(a - b) / max(abs(a), abs(b), floor))


def compare(ref: dict, cand: dict) -> dict:
    axis_rows = []
    total = 0.0
    weights = 0.0
    for key, label, weight, metrics in AXES:
        rows = []
        for path, floor, mlabel in metrics:
            a = dig(ref, path)
            b = dig(cand, path)
            rel = rel_diff(a, b, floor)
            rows.append({
                "path": path,
                "label": mlabel,
                "ref": round(a, 4),
                "cand": round(b, 4),
                "delta": round(b - a, 4),
                "rel": round(rel, 4),
            })
        score = round(100.0 * (1.0 - sum(r["rel"] for r in rows) / len(rows)), 1)
        axis_rows.append({"key": key, "label": label, "weight": weight,
                          "score": score, "metrics": rows})
        total += score * weight
        weights += weight
    total = round(total / weights, 1) if weights else 0.0

    warnings = []
    ru = ref.get("meta", {}).get("unit")
    cu = cand.get("meta", {}).get("unit")
    if ru and cu and ru != cu:
        warnings.append(f"两份文本语料口径不同（ref={ru} / cand={cu}），句长类指标不可比")
    if ref.get("meta", {}).get("short_sample"):
        warnings.append("参照文本不足 800 字，指纹本身偏噪，分数仅供参考")
    if cand.get("meta", {}).get("short_sample"):
        warnings.append("复刻文本不足 800 字，短样本会放大指标波动")

    return {"version": VERSION, "total": total, "axes": axis_rows, "warnings": warnings}


def verdict(result: dict, min_total: float, min_axis: float) -> tuple[bool, list[str]]:
    reasons = []
    if result["total"] < min_total:
        reasons.append(f"总分 {result['total']} < {min_total}")
    for axis in result["axes"]:
        if axis["score"] < min_axis:
            reasons.append(f"{axis['label']} 轴 {axis['score']} < {min_axis}")
    return (not reasons), reasons


def to_markdown(result: dict, passed: bool, reasons: list[str],
                min_total: float, min_axis: float) -> str:
    head = "PASS" if passed else "FAIL"
    out = [f"### 复刻验收：{result['total']} / 100  {head}", ""]
    out.append("| 轴 | 权重 | 得分 | 最偏的三项 |")
    out.append("| --- | --- | --- | --- |")
    for axis in result["axes"]:
        worst = sorted(axis["metrics"], key=lambda m: -m["rel"])[:3]
        detail = "；".join(
            f"{m['label']} {m['ref']}→{m['cand']}" for m in worst if m["rel"] > 0.05
        ) or "都很接近"
        out.append(f"| {axis['label']} | {axis['weight']:.2f} | {axis['score']} | {detail} |")
    out.append("")
    if reasons:
        out.append("未过线：" + "；".join(reasons))
    if result["warnings"]:
        out.append("提示：" + "；".join(result["warnings"]))
    out.append("")
    out.append(f"阈值：总分 >= {min_total}，每轴 >= {min_axis}。"
               "分数只说明像不像，不说明写得好不好。")
    return "\n".join(out)


def load(path: str) -> dict:
    p = Path(path)
    if not p.is_file():
        raise CompareError(f"找不到指纹文件：{path}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CompareError(f"{path} 不是合法 JSON：{exc}") from exc
    if "meta" not in data:
        raise CompareError(f"{path} 缺少 meta，不是 style_probe 的输出")
    return data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="复刻验收：比对两份风格指纹",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python style_compare.py --ref out/ref.json --cand out/cand.json\n"
               "  python style_compare.py --ref a.json --cand b.json --min 80 --json out/cmp.json")
    ap.add_argument("--ref", required=True, help="参照指纹（原文本）")
    ap.add_argument("--cand", required=True, help="待验指纹（复刻文本）")
    ap.add_argument("--min", type=float, default=75.0, help="总分门槛，默认 75")
    ap.add_argument("--axis-min", type=float, default=55.0, help="单轴门槛，默认 55")
    ap.add_argument("--json", dest="json_out", help="结果 JSON 落盘")
    args = ap.parse_args(argv)

    try:
        ref, cand = load(args.ref), load(args.cand)
        result = compare(ref, cand)
    except CompareError as exc:
        print(f"[style_compare] {exc}", file=sys.stderr)
        return 2

    passed, reasons = verdict(result, args.min, args.axis_min)
    print(to_markdown(result, passed, reasons, args.min, args.axis_min))
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(result, verdict="PASS" if passed else "FAIL",
                       reasons=reasons, min_total=args.min, min_axis=args.axis_min)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
