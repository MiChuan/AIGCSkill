#!/usr/bin/env python3
"""往返校验器 — 拿生成出来的音频的特征，跟原始素材的特征比对，给出 0–100 的复述得分。

为甚么要这一步
--------------
「提示词写得好不好」没法直接看，但可以间接测：用同一个 analyzer 去量化两种音频，
如果特征基本一致，说明这个 prompt 真的复述出了那段音频；如果不一致，
差距在哪一维、该改哪个槽位，也能直接指出来。

这是本 skill 唯一一条硬性验收：没有比对结果，就不许说「提示词写好了」。

用法
----
    python compare_features.py ref/features.json new/features.json
    python compare_features.py ref/features.json new/features.json --out out/cmp --gate 80

退出码：0 = 达到默认 / 指定门槛；1 = 未达标（配合 --gate 做 CI 式卡口）。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

# 每个轴：(权重, 容差, 槽位)。容差定得松不等于不负责任 ——
# 生成模型本来就不可能逐样本复刻，容差是「听感上算同一首」的边界。
#
# CRITICAL 里的维度不接受稀释：速度、亮度、律动密度、人声有无是「一听就知道不
# 是一条」的量。它们哪怕只错一个，剩下的维度给再多分也不该判 PASS —— 一堆次要
# 维度全绿，正好会把「速度完全错了」稀释成 86 分的好成绩，这是加权平均的通病。
CRITICAL = {"tempo_bpm", "centroid_hz", "onset_rate", "voiced_ratio"}

# ratio 轴的容差单位是**八度**（log2），不是百分比。
AXES = [
    ("tempo_bpm", "ratio", 0.14, 0.10, "rhythm_key"),     # ±0.10 八度 ≈ ±7%
    ("centroid_hz", "ratio", 0.16, 0.50, "timbre"),       # ±0.50 八度 ≈ ±41%
    ("onset_rate", "ratio", 0.12, 0.60, "rhythm_key"),
    ("voiced_ratio", "abs", 0.12, 0.18, "vocal"),
    ("flatness", "abs", 0.06, 0.18, "timbre"),
    ("lra_db", "abs", 0.06, 4.0, "mix"),
    ("crest_db", "abs", 0.05, 4.0, "mix"),
    ("rms_dbfs", "abs", 0.05, 4.0, "mix"),
    ("reverb_tail_ms", "ratio", 0.06, 1.00, "mix"),
    ("stereo_corr", "abs", 0.04, 0.18, "mix"),
    ("duration_s", "ratio", 0.05, 0.40, "structure"),
    ("key", "enum", 0.04, 0, "mood"),
    ("low_end_ratio", "ratio", 0.05, 0.60, "mix"),
]

SLOT_HINTS = {
    "rhythm_key": "改第 4 槽：BPM 与律动密度（别忘了 -gp 半速 / 倍速两个解）",
    "timbre": "改第 5 槽：亮度（质心）与纹理（谐波 / 噪声比）",
    "mix": "改第 8 槽：空间、低频占比、响度、声场宽度",
    "vocal": "改第 6 槽：人声有无与语种",
    "structure": "改第 7 槽：时长与段落结构",
    "mood": "改第 2 槽：调性与情绪色彩",
}


def _load(path: str) -> dict:
    p = Path(path)
    if not p.is_file():
        raise SystemExit(f"错误：找不到 {p}")
    rep = json.loads(p.read_text(encoding="utf-8"))
    if "features" not in rep:
        raise SystemExit(f"错误：{p} 不是 features.json")
    return rep["features"]


def _low_end(f: dict) -> float:
    return f.get("band_ratios", {}).get("sub", 0.0) + f.get("band_ratios", {}).get("bass", 0.0)


def _axis_value(f: dict, key: str):
    if key == "low_end_ratio":
        return _low_end(f)
    return f.get(key)


def compare(aref: dict, bnew: dict) -> dict:
    rows = []
    total_w = 0.0
    got = 0.0
    caps: list[str] = []
    for key, how, weight, tol, slot in AXES:
        a = _axis_value(aref, key)
        b = _axis_value(bnew, key)
        if a is None or b is None:
            continue
        comparable = True
        if key in CRITICAL and how == "ratio":
            # 两边都测不到（比如都没有节拍）时，这个维度无从比较，不参与否决
            comparable = not (float(a) <= 0 and float(b) <= 0)
        if how == "enum":
            ok = str(a) == str(b)
            score = 1.0 if ok else 0.0
            detail = f"{a} vs {b}"
        elif how == "abs":
            d = abs(float(a) - float(b))
            score = max(0.0, 1.0 - max(0.0, d - tol) / (tol * 2 + 1e-9))
            ok = d <= tol
            detail = f"{float(a):.3g} vs {float(b):.3g}（差 {d:.3g}，容差 {tol}）"
        else:
            fa, fb = float(a), float(b)
            if fa <= 0 and fb <= 0:
                score, ok, detail = 1.0, True, "两边都测不到"
            elif fa <= 0 or fb <= 0:
                score, ok = 0.0, False
                detail = f"{fa:.3g} vs {fb:.3g}（一边测不到，视为完全不同）"
            else:
                # 比例量在对数尺度上比。用 min/max 折 NATURAL 会把「亮 2.7 倍」
                # 压成「差 62.5%」，看着还能接受，实际一听就是两条完全不同的素材。
                # 换算成八度之后：0.5 个八度 ≈ 亮 1.41 倍，这是人耳朵能分辨的边界。
                oct_diff = abs(math.log2(max(fb, 1e-9) / max(fa, 1e-9)))
                score = max(0.0, 1.0 - max(0.0, oct_diff - tol) / (tol * 2 + 1e-9))
                ok = oct_diff <= tol
                ratio = max(fa, fb) / max(min(fa, fb), 1e-9)
                detail = (f"{fa:.3g} vs {fb:.3g}（{ratio:.2f} 倍 = {oct_diff:.2f} 个八度，"
                          f"容差 {tol:.2f}）")
        total_w += weight
        got += weight * score
        rows.append({"axis": key, "score": round(score, 3), "pass": ok,
                     "slot": slot, "detail": detail,
                     "critical": key in CRITICAL and comparable})
        if key in CRITICAL and comparable and score < 0.5:
            caps.append(f"{key} 得分只有 {score:.2f}（{detail}）")

    overall = round(100.0 * got / total_w, 1) if total_w else 0.0
    if caps:
        overall = min(overall, 59.0)
    if overall >= 80:
        verdict = "PASS — 这条 prompt 基本复述出了目标音频，可以交付"
    elif overall >= 60:
        verdict = "ITERATE — 骨架对了，按下面的提示改对应槽位后再生成一次"
    else:
        verdict = "MISMATCH — prompt 没说到点上，先回填「只能你答的问题」，别硬调参数"
    if caps:
        verdict = "MISMATCH — 关键维度有硬伤：" + "；".join(caps)
    return {"score": overall, "verdict": verdict, "rows": rows, "caps": caps}


def _render(res: dict, ref_path: str, new_path: str) -> str:
    lines = [f"# 复述校验：{Path(ref_path).parent.name or Path(ref_path).stem} → "
             f"{Path(new_path).parent.name or Path(new_path).stem}", ""]
    lines.append(f"**得分 {res['score']} / 100** — {res['verdict']}")
    lines.append("")
    lines.append("| 维度 | 得分 | 原素材 vs 生成 | 该改哪 |")
    lines.append("|---|---|---|---|")
    for r in res["rows"]:
        flag = "✓" if r["pass"] else "✗"
        lines.append(f"| {r['axis']} {flag} | {r['score']:.2f} | {r['detail']} | {SLOT_HINTS.get(r['slot'], r['slot'])} |")
    lines.append("")
    failed = [r for r in res["rows"] if not r["pass"]]
    if failed:
        lines.append("## 优先修这几项")
        lines.append("")
        order = sorted(failed, key=lambda r: r["score"])
        for r in order[:5]:
            lines.append(f"- **{r['axis']}**（{r['score']:.2f}）：{SLOT_HINTS.get(r['slot'], '')}")
            lines.append(f"  - 依据：{r['detail']}")
    else:
        lines.append("全部维度在容差内。")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="对比两份 features.json，给「提示词有没有复述出来」打分")
    ap.add_argument("reference", help="原始素材的 features.json")
    ap.add_argument("candidate", help="生成结果的 features.json")
    ap.add_argument("--out", help="输出目录，写入 compare.md 与 compare.json")
    ap.add_argument("--gate", type=float, default=0.0,
                    help="分数门槛，低于它就用退出码 1 报错（建议 80）")
    args = ap.parse_args(argv)

    res = compare(_load(args.reference), _load(args.candidate))
    md = _render(res, args.reference, args.candidate)
    print(md)

    if args.out:
        od = Path(args.out)
        od.mkdir(parents=True, exist_ok=True)
        (od / "compare.md").write_text(md + "\n", encoding="utf-8")
        (od / "compare.json").write_text(json.dumps(res, ensure_ascii=False, indent=2) + "\n",
                                         encoding="utf-8")
        print(f"\n已写入 {od}", file=sys.stderr)

    if args.gate and res["score"] < args.gate:
        print(f"\n未达门槛 {args.gate}（实际 {res['score']}）", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
