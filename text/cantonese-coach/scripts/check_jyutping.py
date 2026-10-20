#!/usr/bin/env python3
"""粵拼（Jyutping / LSHK）校驗器 — 零依賴，可獨立跑，也可被其他腳本 import。

為甚麼要這個腳本
----------------
語言模型寫粵拼的失敗模式非常穩定，而且自己看不出來：

1. 混入普通話拼音 —— zh / ch / sh / x / q / r / ü，或者用聲調符號 āáǎà
2. 漏聲調，或者用 7 / 8 / 9 這套舊方案的入聲調號
3. 韻母臆造 —— 把 goeng 寫成 goeng 之外的拼法、把 eoi 寫成 eui / oei
4. 漢字數同音節數對不上 —— 最常見是整句漏寫一個字的音節

第 4 條是最有價值的檢查：粵語幾乎是「一字一音節」，所以
`漢字數 == 音節數` 是一條很硬的等式，能抓住「睇唔明」只寫了兩節這種錯。

用法
----
    python check_jyutping.py --text "ngo5 sik6 gan2 faan6"
    python check_jyutping.py --pair "唔該=m4 goi1" --pair "埋單=maai4 daan1"
    python check_jyutping.py --json specs/deck-core-100.json
    echo "nei5 hou2" | python check_jyutping.py --stdin

退出碼：有任何 error 就 1，只有 warning 是 0。
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

# ------------------------------------------------------------------ 音系表

# 19 個聲母（含零聲母，即下面不列）。長的放前面，靠貪婪匹配吃掉 gw / kw / ng。
INITIALS = (
    "gw", "kw", "ng",
    "b", "p", "m", "f", "d", "t", "n", "l",
    "g", "k", "h", "w", "z", "c", "s", "j",
)

# 韻母（含成音節鼻音 m / ng）。同樣長的在前。
FINALS = (
    "aang", "aau", "aai", "aam", "aan", "aap", "aat", "aak", "aa",
    "oeng", "oet", "oek", "oe",
    "eoi", "eon", "eot",
    "eng", "ek", "ei", "eu", "em", "e",
    "ing", "iu", "im", "in", "ip", "it", "ik", "i",
    "ong", "ou", "oi", "on", "ot", "ok", "o",
    "ung", "ui", "un", "ut", "uk", "u",
    "yun", "yut", "yu",
    "ang", "au", "ai", "am", "an", "ap", "at", "ak", "a",
    "m", "ng",
)

INITIALS_BY_LEN = tuple(sorted(INITIALS, key=len, reverse=True))
FINALS_BY_LEN = tuple(sorted(FINALS, key=len, reverse=True))

# 普通話 / 英語式拼寫的常見錯誤來源
MANDARIN_HINTS = {
    "zh": "粵拼冇 zh，對應嘅係 z（知 zi1、中 zung1）",
    "ch": "粵拼冇 ch，對應嘅係 c（茶 caa4、出 ceot1）",
    "sh": "粵拼冇 sh，對應嘅係 s（三 saam1、食 sik6）",
    "x": "粵拼冇 x；普通話 x 通常落 s / z / c / h（心 sam1、知 zi1、前 cin4、香 hoeng1）",
    "q": "粵拼冇 q；普通話 q 通常落 c / z / h（七 cat1、千 cin1、去 heoi3）",
    "r": "粵拼冇 r；普通話 r 通常落 j（人 jan4、日 jat6）",
    "y": "粵拼用 j 而唔係 y（一 jat1、人 jan4、嘢 je5）",
    "v": "粵拼冇 v",
}

TONE_MARKS = "āáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ"

CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
SYLLABLE_RE = re.compile(r"^([a-z]+)(\d+)$")
BARE_RE = re.compile(r"^([a-z]+)$")


class Issue:
    __slots__ = ("level", "code", "msg")

    def __init__(self, level: str, code: str, msg: str):
        self.level = level
        self.code = code
        self.msg = msg

    def __repr__(self) -> str:  # pragma: no cover
        return f"{self.level.upper()} {self.code}: {self.msg}"

    def as_dict(self) -> dict:
        return {"level": self.level, "code": self.code, "msg": self.msg}


def _split_body(body: str):
    """把無聲調的音節切成 (聲母, 韻母)；切不開就回 (None, 猜測原因)。"""
    for ini in INITIALS_BY_LEN:
        if body.startswith(ini):
            rest = body[len(ini):]
            if rest in FINALS:
                return ini, rest
    if body in FINALS:  # 零聲母（啊 aa3、愛 oi3、唔 m4、五 ng5）
        return "", body
    return None, None


def _guess_initial(body: str):
    """切不開時，判斷是「聲母非法」還是「韻母非法」。"""
    for bad in ("zh", "ch", "sh"):
        if body.startswith(bad):
            return bad
    if body[:1] in "xyzqv":
        return body[:1]
    if body[:1] in "bpmfdtnlgkhwzcsj":
        return None  # 聲母沒問題，是韻母錯
    return body[:1]


def check_syllable(tok: str, pos: int) -> list:
    """校驗單個音節，例如 ngo5 / m4 / gwok3。"""
    out = []
    tag = f"第 {pos} 節「{tok}」"

    if not tok:
        return [Issue("error", "E101", f"{tag} 係空值")]

    if any(ch in TONE_MARKS for ch in tok):
        return [Issue("error", "E202",
                      f"{tag} 用咗聲調符號；粵拼一律用數字調號（ngo5，唔係 ngó）")]

    m = SYLLABLE_RE.match(tok)
    if not m:
        mb = BARE_RE.match(tok)
        if mb:
            out.append(Issue("error", "E105",
                             f"{tag} 冇聲調；粵拼每個音節必須帶 1–6 其中一個調號"))
            body = mb.group(1)
        else:
            out.append(Issue("error", "E102",
                             f"{tag} 唔係合法音節（只准 a–z + 1–6，音節之間要空格）"))
            return out
    else:
        body = m.group(1)
        tone_digits = m.group(2)
        if tone_digits in ("7", "8", "9"):
            out.append(Issue("warn", "W301",
                             f"{tag} 用咗 7/8/9；粵拼把入聲併入 1/3/6（一 jat1、百 baak3、十 sap6）"))
        elif len(tone_digits) > 1 or tone_digits not in ("1", "2", "3", "4", "5", "6"):
            out.append(Issue("error", "E105", f"{tag} 調號只可以係 1–6，收到「{tone_digits}」"))
            return out

    for bad, hint in MANDARIN_HINTS.items():
        if body.startswith(bad) and not body.startswith("ng"):
            out.append(Issue("error", "E201", f"{tag} 似普通話拼音：{hint}"))
            return out

    ini, fin = _split_body(body)
    if ini is None:
        guess = _guess_initial(body)
        if guess:
            hint = MANDARIN_HINTS.get(guess, f"粵拼冇「{guess}」呢個聲母")
            out.append(Issue("error", "E103", f"{tag} 聲母非法：{hint}"))
        else:
            near = difflib.get_close_matches(body[1:], FINALS, n=1, cutoff=0.6)
            tail = f"，係咪想寫「{body[0]}{near[0]}」？" if near else ""
            out.append(Issue("error", "E104", f"{tag} 韻母「{body[1:]}」唔存在{tail}"))
    return out


def check_syllables(jp: str) -> list:
    """校驗一整串粵拼（空白分隔）。"""
    if not jp or not jp.strip():
        return [Issue("error", "E101", "粵拼係空值")]
    toks = jp.strip().split()
    issues = []
    for i, tok in enumerate(toks, 1):
        issues.extend(check_syllable(tok, i))
    return issues


def check_pair(hz: str, jp: str) -> list:
    """校驗「漢字 + 粵拼」這一個條目，含漢字數／音節數對齊。"""
    issues = check_syllables(jp)
    n_char = len(CJK.findall(hz or ""))
    n_syl = len((jp or "").split())
    if n_char and n_syl and n_char != n_syl:
        issues.append(Issue(
            "error", "E301",
            f"「{hz}」{n_char} 個漢字，但粵拼有 {n_syl} 節；"
            f"粵語幾乎一字一音節，通常係漏寫或多寫咗一個字嘅音節"))
    return issues


def format_issues(issues, owner: str = "") -> str:
    if not issues:
        return ""
    label = {"error": "ERROR", "warn": "warn ", "info": "info "}
    lines = []
    for it in issues:
        head = f"{owner}：" if owner else ""
        lines.append(f"  [{label.get(it.level, it.level)}] "
                     f"{it.code} {head}{it.msg}")
    return "\n".join(lines)


# ------------------------------------------------------------------ CLI

def _iter_pairs(args):
    if args.text:
        yield "", args.text
    for p in args.pair or []:
        if "=" in p:
            hz, jp = p.split("=", 1)
        elif "\t" in p:
            hz, jp = p.split("\t", 1)
        else:
            yield "", p
            continue
        yield hz.strip(), jp.strip()
    if args.json:
        data = json.loads(Path(args.json).read_text(encoding="utf-8"))
        items = data["items"] if isinstance(data, dict) and "items" in data else data
        for row in items:
            yield str(row.get("hz", "")).strip(), str(row.get("jp", "")).strip()
    if args.file:
        for line in Path(args.file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                hz, jp = line.split("=", 1)
            elif "\t" in line:
                hz, jp = line.split("\t", 1)
            else:
                hz, jp = "", line
            yield hz.strip(), jp.strip()
    if args.stdin:
        for line in sys.stdin.read().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                hz, jp = line.split("=", 1)
            elif "\t" in line:
                hz, jp = line.split("\t", 1)
            else:
                hz, jp = "", line
            yield hz.strip(), jp.strip()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="粵拼校驗器")
    ap.add_argument("--text", help="一句粵拼，例如 \"ngo5 sik6 gan2 faan6\"")
    ap.add_argument("--pair", action="append", help="漢字=粵拼，可重複")
    ap.add_argument("--json", help="詞表 JSON（items[].hz / items[].jp）")
    ap.add_argument("--file", help="每行 漢字=粵拼")
    ap.add_argument("--stdin", action="store_true",
                    help="從標準輸入讀 漢字=粵拼（配合管道用）")
    args = ap.parse_args(argv)

    n_err = n_warn = 0
    for hz, jp in _iter_pairs(args):
        issues = check_pair(hz, jp) if hz else check_syllables(jp)
        n_err += sum(1 for i in issues if i.level == "error")
        n_warn += sum(1 for i in issues if i.level == "warn")
        label = f"{hz} = {jp}" if hz else jp
        if issues:
            print(f"✗ {label}")
            print(format_issues(issues))
        else:
            print(f"✓ {label}")

    if n_err or n_warn:
        print(f"\n合計：{n_err} 個錯誤、{n_warn} 個警告")
    return 1 if n_err else 0


if __name__ == "__main__":
    sys.exit(main())
