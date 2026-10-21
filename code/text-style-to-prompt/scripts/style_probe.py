#!/usr/bin/env python3
"""文本风格量化探针：把一段文本压成一份「可比较的指纹」。

读文本是主观的，比对必须是客观的。这个脚本只做后者：把「读起来短促」变成
「句均 12.4 字、短句率 0.58」，把「爱用破折号」变成「每千字 9.2 个」。
提炼提示词的那部分仍然是人的活（见 references/01-axes.md），这里只提供
人手不许瞎猜的那批数字。

输出是一份扁平 JSON，字段分七组：meta / sentence / paragraph / punct /
person / lexicon / dialog / layout。直接喂给 style_compare.py 做复刻验收。

纯标准库，零依赖。

用法：
    python scripts/style_probe.py --text examples/sample-input.md
    python scripts/style_probe.py --text a.txt --out out/a.json --pretty
    python scripts/style_probe.py --text a.txt --summary
    cat a.txt | python scripts/style_probe.py --stdin

口径说明（不要猜，都在 JSON 的 meta.unit 里）：
    中文语料 unit=char，短句 <=8 字，长句 >=40 字；
    西文语料 unit=word，短句 <=4 词，长句 >=20 词。
    chars 一律指「字母数字字符数」，不含空白与标点。
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import unicodedata
from collections import Counter
from pathlib import Path

VERSION = "1.0.0"

# ---------------------------------------------------------------- 口径常量

CJK = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
LATIN = re.compile(r"[A-Za-z]")
WORD = re.compile(r"[A-Za-z']+")

ZH_SHORT, ZH_LONG = 8, 40
EN_SHORT, EN_LONG = 4, 20

# 中文弯引号一律写成转义，源码里不出现这些字符本身
QUOTE_OPEN = "\u300c\u300e\u201c\""      # 「 『 “ "
QUOTE_CLOSE = "\u300d\u300f\u201d\""     # 」 』 ” "
QUOTE_RE = re.compile(
    "[" + re.escape(QUOTE_OPEN) + "]([^" + re.escape(QUOTE_OPEN + QUOTE_CLOSE) + "]{1,400})"
    "[" + re.escape(QUOTE_CLOSE) + "]"
)

FUNC_WORDS = ["的", "了", "是", "在", "就", "也", "都", "而", "却", "但", "很",
              "太", "更", "最", "把", "被", "着", "过", "吗", "呢", "吧", "啊"]

PERSON_PATTERNS = {
    "first": ["我", "俺", "咱", "笔者", "本人"],
    "first_plural": ["我们", "咱们", "我等"],
    "second": ["你", "你们", "您", "诸位", "各位", "读者"],
    "honorific": ["您"],
    "third": ["他", "她", "它", "他们", "她们", "它们"],
}

PUNCT_KEYS = {
    "comma": "，,",
    "full_stop": "。",
    "semicolon": "；;",
    "colon": "：:",
    "dash": "\u2014\u2015\u2013-",
    "ellipsis": "\u2026",
    "excl": "！!",
    "quest": "？?",
    "paren": "（）()",
    "book_title": "《》",
}

HEADING_RE = re.compile(
    r"^\s{0,3}(#{1,6}\s|第[一二三四五六七八九十百千0-9]+[章节篇回]|[一二三四五六七八九十]+[、.])")
LIST_RE = re.compile(r"^\s*([-*+]\s|\d+[.、)]\s)")


# ---------------------------------------------------------------- 基础工具

def content_len(s: str) -> int:
    """字母数字字符数：中文按字，西文按字母。空白标点全不计。"""
    return sum(1 for c in s if c.isalnum())


def is_emoji(ch: str) -> bool:
    cp = ord(ch)
    return (0x1F300 <= cp <= 0x1FAFF) or (0x2600 <= cp <= 0x27BF) or cp in (0x2B50, 0x2B55)


def split_sentences(text: str) -> list[str]:
    """按句末标点与换行切句。分号不算句界——分号连缀本身就是一种风格。"""
    parts = re.split(r"[。！？!?\u2026]+|(?<=[.])\s+|\n+", text)
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if p and content_len(p) > 0:
            out.append(p)
    return out


def split_paragraphs(text: str) -> list[str]:
    raw = re.split(r"\n\s*\n", text)
    paras = [p.strip() for p in raw if p.strip()]
    return paras or ([text.strip()] if text.strip() else [])


def tokens(text: str, unit: str) -> list[str]:
    """对比二元组用的序列：中文取字，西文取词。"""
    if unit == "word":
        return WORD.findall(text.lower())
    return [c.lower() for c in text if c.isalnum()]


def per_kchar(count: int, chars: int) -> float:
    return round(count * 1000.0 / chars, 2) if chars else 0.0


def pct(x: float) -> float:
    return round(x, 4)


# ---------------------------------------------------------------- 各维度

def probe_sentences(sents: list[str], unit: str) -> dict:
    lens = [content_len(s) if unit == "char" else len(WORD.findall(s)) for s in sents]
    if not lens:
        return {"count": 0, "mean": 0.0, "median": 0.0, "p90": 0, "stdev": 0.0,
                "short_ratio": 0.0, "long_ratio": 0.0, "max": 0}
    short_th, long_th = (ZH_SHORT, ZH_LONG) if unit == "char" else (EN_SHORT, EN_LONG)
    ordered = sorted(lens)
    p90 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))]
    return {
        "count": len(lens),
        "mean": round(statistics.fmean(lens), 2),
        "median": round(statistics.median(lens), 2),
        "p90": p90,
        "stdev": round(statistics.pstdev(lens), 2) if len(lens) > 1 else 0.0,
        "short_ratio": pct(sum(1 for n in lens if n <= short_th) / len(lens)),
        "long_ratio": pct(sum(1 for n in lens if n >= long_th) / len(lens)),
        "max": max(lens),
    }


def probe_paragraphs(paras: list[str], unit: str) -> dict:
    if not paras:
        return {"count": 0, "sent_mean": 0.0, "single_sent_ratio": 0.0, "chars_mean": 0.0}
    counts = [len(split_sentences(p)) or 1 for p in paras]
    clens = [content_len(p) for p in paras]
    return {
        "count": len(paras),
        "sent_mean": round(statistics.fmean(counts), 2),
        "single_sent_ratio": pct(sum(1 for n in counts if n == 1) / len(paras)),
        "chars_mean": round(statistics.fmean(clens), 1),
    }


def probe_punct(text: str, chars: int) -> dict:
    per = {}
    for key, chars_set in PUNCT_KEYS.items():
        if key == "dash":
            # 只认成对的中文破折号；西文单连字符不参与，否则代码/日期会污染
            n = text.count("\u2014\u2014") + text.count("\u2015\u2015")
        elif key == "ellipsis":
            n = text.count("\u2026\u2026") or text.count("\u2026")
        elif key == "paren":
            n = sum(text.count(c) for c in chars_set) / 2
        elif key == "book_title":
            n = sum(text.count(c) for c in chars_set) / 2
        else:
            n = sum(text.count(c) for c in chars_set)
        per[key] = per_kchar(int(round(n)), chars)
    per["total"] = round(sum(v for k, v in per.items() if k != "total"), 2)
    return per


def probe_person(text: str, chars: int) -> dict:
    out = {}
    for key, words in PERSON_PATTERNS.items():
        n = sum(text.count(w) for w in words)
        out[f"{key}_ratio"] = per_kchar(n, chars)
    return out


def probe_lexicon(text: str, chars: int, unit: str) -> dict:
    toks = tokens(text, unit)
    ttr = len(set(toks)) / len(toks) if toks else 0.0
    bigrams = Counter(zip(toks, toks[1:]))
    top = bigrams.most_common(20)
    covered = sum(c * 2 for _, c in top)
    return {
        "ttr": pct(ttr),
        "de_per_kchar": per_kchar(text.count("的") + text.count("地") + text.count("得"), chars),
        "function_per_kchar": per_kchar(sum(text.count(w) for w in FUNC_WORDS), chars),
        "bigram_repeat_ratio": pct(covered / len(toks)) if toks else 0.0,
        "top_bigrams": [["".join(k) if unit == "char" else " ".join(k), c] for k, c in top],
        "digit_per_kchar": per_kchar(sum(1 for c in text if c.isdigit()), chars),
        "latin_per_kchar": per_kchar(len(LATIN.findall(text)), chars),
    }


def probe_dialog(text: str, chars: int) -> dict:
    hits = QUOTE_RE.findall(text)
    inner = sum(content_len(h) for h in hits)
    return {
        "quote_count": len(hits),
        "quote_ratio": pct(inner / chars) if chars else 0.0,
        "quote_mean_len": round(inner / len(hits), 1) if hits else 0.0,
    }


def probe_layout(text: str) -> dict:
    lines = text.split("\n")
    non_empty = [ln for ln in lines if ln.strip()]
    total = len(lines) or 1
    avg_line = statistics.fmean([content_len(ln) for ln in non_empty]) if non_empty else 0.0
    return {
        "lines": len(lines),
        "avg_line_chars": round(avg_line, 1),
        "blank_line_ratio": pct(sum(1 for ln in lines if not ln.strip()) / total),
        "headings": sum(1 for ln in lines if HEADING_RE.match(ln)),
        "list_lines": sum(1 for ln in lines if LIST_RE.match(ln)),
        "emoji": sum(1 for c in text if is_emoji(c)),
    }


# ---------------------------------------------------------------- 汇总

def guess_unit(text: str) -> tuple[str, float]:
    cjk = len(CJK.findall(text))
    latin = len(LATIN.findall(text))
    zh_ratio = cjk / (cjk + latin) if (cjk + latin) else 0.0
    return ("char" if zh_ratio >= 0.4 else "word"), round(zh_ratio, 3)


def probe(text: str) -> dict:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    chars = content_len(text)
    unit, zh_ratio = guess_unit(text)
    paras = split_paragraphs(text)
    sents = split_sentences(text)
    layout = probe_layout(text)
    return {
        "meta": {
            "version": VERSION,
            "chars": chars,
            "unit": unit,
            "zh_ratio": zh_ratio,
            "paragraphs": len(paras),
            "sentences": len(sents),
            "lines": layout["lines"],
            "short_sample": chars < 800,
        },
        "sentence": probe_sentences(sents, unit),
        "paragraph": probe_paragraphs(paras, unit),
        "punct": probe_punct(text, chars),
        "person": probe_person(text, chars),
        "lexicon": probe_lexicon(text, chars, unit),
        "dialog": probe_dialog(text, chars),
        "layout": layout,
    }


def summary(data: dict) -> str:
    s, p, lx = data["sentence"], data["paragraph"], data["lexicon"]
    unit = "字" if data["meta"]["unit"] == "char" else "词"
    rows = [
        f"lang={data['meta']['unit']} chars={data['meta']['chars']} "
        f"paras={data['meta']['paragraphs']} sents={s['count']}",
        f"句长  均{s['mean']}{unit} 中位{s['median']} p90={s['p90']} "
        f"短句率{s['short_ratio']:.0%} 长句率{s['long_ratio']:.0%}",
        f"段构  段均{p['sent_mean']}句 单句成段{p['single_sent_ratio']:.0%} "
        f"段均{p['chars_mean']}字",
        f"标点  逗号{data['punct']['comma']}/千字 破折{data['punct']['dash']} "
        f"感叹{data['punct']['excl']} 问号{data['punct']['quest']} 省略{data['punct']['ellipsis']}",
        f"人称  我{data['person']['first_ratio']} 你{data['person']['second_ratio']} "
        f"您{data['person']['honorific_ratio']} 他{data['person']['third_ratio']}（每千字）",
        f"词汇  字种比{lx['ttr']:.2f} 的地得{lx['de_per_kchar']} 虚词{lx['function_per_kchar']} "
        f"二元重复{lx['bigram_repeat_ratio']:.0%}",
        f"对话  引号{data['dialog']['quote_count']}处 占比{data['dialog']['quote_ratio']:.0%}",
        f"排版  行均{data['layout']['avg_line_chars']}字 空行率"
        f"{data['layout']['blank_line_ratio']:.0%} 标题{data['layout']['headings']} "
        f"列表{data['layout']['list_lines']} emoji{data['layout']['emoji']}",
    ]
    if data["meta"]["short_sample"]:
        rows.append("注意  样本不足 800 字，指纹偏噪，置信度最多 medium")
    top = lx["top_bigrams"][:5]
    if top:
        rows.append("高频  " + " / ".join(f"{g}x{c}" for g, c in top))
    return "\n".join(rows)


def load_text(path: str | None, use_stdin: bool) -> str:
    if use_stdin or not path:
        return sys.stdin.read()
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"找不到文本文件：{path}")
    return p.read_text(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="文本风格量化探针：文本 -> 可比较的指纹 JSON",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python style_probe.py --text examples/sample-input.md --summary\n"
               "  python style_probe.py --text a.txt --out out/a.json --pretty")
    ap.add_argument("--text", help="待分析文本路径（.txt/.md，UTF-8）")
    ap.add_argument("--stdin", action="store_true", help="从标准输入读文本")
    ap.add_argument("--out", help="指纹 JSON 落盘路径")
    ap.add_argument("--pretty", action="store_true", help="JSON 缩进输岀")
    ap.add_argument("--summary", action="store_true", help="打印人读摘要（可与 --out 同用）")
    ap.add_argument("--quiet", action="store_true", help="不打印 JSON")
    args = ap.parse_args(argv)

    try:
        text = load_text(args.text, args.stdin)
    except FileNotFoundError as exc:
        print(f"[style_probe] {exc}", file=sys.stderr)
        return 2

    if not text.strip():
        print("[style_probe] 文本为空，无可分析内容", file=sys.stderr)
        return 2

    data = probe(text)
    data["meta"]["source"] = args.text or "<stdin>"
    payload = json.dumps(data, ensure_ascii=False, indent=2 if args.pretty else None)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload, encoding="utf-8")
    if not args.quiet and not args.summary:
        print(payload)
    if args.summary:
        print(summary(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
