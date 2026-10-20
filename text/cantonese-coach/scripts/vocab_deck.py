#!/usr/bin/env python3
"""詞表 → 卡片／CSV／drill sheet，順手把粵拼校驗一次 — 零依賴。

輸入一份詞表 JSON（items[].hz / jp / en / tier / ctx / note），輸出三種東西：

1. `<deck>.md`      分層詞表，帶聲調序列（5-2 這種），適合貼在筆記裡
2. `<deck>.csv`     Anki / Excel 可直讀（UTF-8 BOM，Excel 唔會亂碼）
3. `<deck>-drill.txt` 遮住粵拼的默寫紙，同一批詞的另一種用法

順帶做四項檢查（除了粵拼本身）：
- 詞量是否達到 `expect`
- 同一個詞在不同條目裡讀音唔一致 → 其中一處調號寫錯（同音字只報 info，唔誤殺）
- 同一個漢字在同一條目裡讀音唔一致 → 要麼是變音（合理），要麼是寫錯
- 一級詞（tier 1）佔比過高 → 說明「核心」沒有分層，等於沒有優先級

用法
----
    python vocab_deck.py --deck specs/deck-core-100.json --outdir out/deck
    python vocab_deck.py --deck specs/deck-core-100.json --tier 1 --limit 10
    python vocab_deck.py --deck specs/deck-core-100.json --check
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_jyutping import CJK, Issue, check_pair, format_issues  # noqa: E402

TIER_LABEL = {
    1: "第一層：冇佢活唔到",
    2: "第二層：旅行現場一定撞到",
    3: "第三層：傾偈講感受先用",
}

# 公認的多讀字，出現不一致時降級成 info（變音／文白異讀，屬正常）
MULTI_READING_OK = set("聽行長重間好會得地相數中為著")


def tone_pattern(jp: str) -> str:
    tones = [ch for tok in jp.split() for ch in tok if ch.isdigit()]
    return "-".join(tones)


def load_deck(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if "items" not in data or not isinstance(data["items"], list):
        raise ValueError("詞表 JSON 必須有 items 陣列")
    return data


def audit(deck: dict, strict: bool = True) -> tuple:
    errors, warns = [], []
    items = deck["items"]
    expect = deck.get("expect")
    if expect and len(items) != expect:
        msg = f"詞量 {len(items)} ≠ expect {expect}"
        (errors if strict else warns).append(Issue("error" if strict else "warn", "E401", msg))

    by_hz = defaultdict(set)
    by_jp = defaultdict(set)
    for i, it in enumerate(items, 1):
        hz, jp = str(it.get("hz", "")).strip(), str(it.get("jp", "")).strip()
        for issue in check_pair(hz, jp):
            errors.append(issue) if issue.level == "error" else warns.append(issue)
        for key in ("en", "ctx", "tier"):
            if not it.get(key):
                warns.append(Issue("warn", "W401", f"第 {i} 條「{hz}」缺 `{key}`"))
        by_hz[hz].add(jp)
        by_jp[jp].add(hz)

    # 逐字讀音一致性（要左右對齊，所以單獨一輪）
    char_syl = defaultdict(set)
    for it in items:
        chars, syls = CJK.findall(str(it.get("hz", ""))), str(it.get("jp", "")).split()
        if len(chars) == len(syls):
            for ch, syl in zip(chars, syls):
                char_syl[ch].add(syl)
    for ch, syls in char_syl.items():
        if len(syls) > 1:
            detail = "、".join(sorted(syls))
            if ch in MULTI_READING_OK:
                warns.append(Issue("info", "I401",
                                   f"「{ch}」有兩種讀音（{detail}）—— 多讀字，正常；"
                                   f"教學時要指出係邊個詞讀邊個音"))
            else:
                warns.append(Issue("warn", "W402",
                                   f"「{ch}」在詞表裡出現兩種讀音（{detail}）—— "
                                   f"可能其中一條寫錯，亦可能係變音，要人手確認"))

    for hz, jps in by_hz.items():
        if len(jps) > 1:
            warns.append(Issue("warn", "W403",
                               f"「{hz}」在不同條目有兩種粵拼（{'、'.join(sorted(jps))}）—— "
                               f"同一個詞唔應該有兩個讀音，其中一處大概率寫錯"))

    for jp, hzs in by_jp.items():
        if len(hzs) > 1:
            warns.append(Issue("info", "I403",
                               f"粵拼「{jp}」對應多個漢字（{'、'.join(sorted(hzs))}）—— "
                               f"粵語同音字好常見，屬正常；只需確認唔係同一條重複收錄"))

    tiers = Counter(int(it.get("tier", 0)) for it in items)
    if tiers.get(1, 0) > len(items) * 0.45:
        warns.append(Issue("warn", "W404",
                           f"第一層佔 {tiers[1]}/{len(items)}：分層等於冇分層，"
                           f"核心詞應該壓到 30% 左右"))
    if 0 in tiers:
        errors.append(Issue("error", "E402", f"有 {tiers[0]} 條目缺 tier（1/2/3）"))

    tones = Counter(t for it in items for t in tone_pattern(str(it.get("jp", ""))).split("-") if t)
    total = sum(tones.values()) or 1
    if tones.get("1", 0) / total > 0.45:
        warns.append(Issue("warn", "W405",
                           f"調 1 佔 {tones['1'] / total:.0%}：粵語裡調 1 冇咁多，"
                           f"可能有人把高平調一律寫成 1"))
    return errors, warns, tones


def render_md(deck: dict, items: list, tones: Counter) -> str:
    a = []
    a.append(f"# {deck.get('title') or deck.get('deck')}")
    a.append("")
    if deck.get("desc"):
        a.append(f"> {deck['desc']}")
        a.append("")
    a.append(f"共 {len(items)} 條。聲調序列用 `粵拼` 的數字組成（`5-2` = 低升 + 高升）。")
    a.append("")
    for tier in sorted({int(it.get("tier", 0)) for it in items}):
        group = [it for it in items if int(it.get("tier", 0)) == tier]
        a.append(f"## {TIER_LABEL.get(tier, f'第 {tier} 層')} — {len(group)} 條")
        a.append("")
        a.append("| # | 漢字 | 粵拼 | 調 | 意思 | 語境 | 備註 |")
        a.append("| --- | --- | --- | --- | --- | --- | --- |")
        for n, it in enumerate(group, 1):
            jp = str(it["jp"]).strip()
            a.append(f"| {n} | {it['hz']} | `{jp}` | {tone_pattern(jp)} "
                     f"| {it.get('en', '')} | {it.get('ctx', '')} | {it.get('note', '')} |")
        a.append("")
    if tones:
        a.append("## 聲調分佈")
        a.append("")
        a.append("| 調 | 1 高平 | 2 高升 | 3 中平 | 4 低降 | 5 低升 | 6 低平 |")
        a.append("| --- | --- | --- | --- | --- | --- | --- |")
        total = sum(tones.values())
        a.append("| 次數 | " + " | ".join(str(tones.get(str(t), 0)) for t in range(1, 7)) + " |")
        a.append("| 佔比 | " + " | ".join(
            f"{tones.get(str(t), 0) / total:.0%}" for t in range(1, 7)) + " |")
        a.append("")
        a.append("調 2 同調 5 都係升調，但起點唔同（高升 vs 低升），"
                 "這是英語母語者最常合併的一對；表裡調 2 與調 5 的詞要成對練。")
        a.append("")
    return "\n".join(a)


def write_csv(path: Path, items: list) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Front", "Back", "Tags"])
        for it in items:
            back = f"{it['jp']}\n{it.get('en', '')}"
            if it.get("note"):
                back += f"\n({it['note']})"
            w.writerow([it["hz"], back, f"cantonese tier{it.get('tier', '')} {it.get('ctx', '')}"])


def write_drill(path: Path, items: list) -> None:
    lines = ["# 默寫紙", "",
             "用法：睇漢字寫粵拼（連聲調數字），寫完再翻到最後對答案。",
             "第二輪倒過來：蓋住漢字，只睇粵拼讀出嚟。", ""]
    for n, it in enumerate(items, 1):
        lines.append(f"{n:>3}. {it['hz']}")
        lines.append("     粵拼：__________________________")
    lines.append("")
    lines.append("## 答案")
    lines.append("")
    for n, it in enumerate(items, 1):
        lines.append(f"{n:>3}. {it['hz']}  {it['jp']}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="粵語詞表 → 卡片")
    ap.add_argument("--deck", required=True)
    ap.add_argument("--outdir", default="out/deck")
    ap.add_argument("--tier", type=int, choices=(1, 2, 3))
    ap.add_argument("--limit", type=int)
    ap.add_argument("--check", action="store_true", help="只校驗，唔輸出")
    args = ap.parse_args(argv)

    deck = load_deck(Path(args.deck))
    errors, warns, tones = audit(deck)
    for w in warns:
        print(f"[{w.level:5}] {w.code} {w.msg}")
    for e in errors:
        print(f"[ERROR] {e.code} {e.msg}")

    items = deck["items"]
    if args.tier:
        items = [it for it in items if int(it.get("tier", 0)) == args.tier]
    if args.limit:
        items = items[: args.limit]

    print(f"\n校驗：{len(errors)} 錯 / "
          f"{sum(1 for w in warns if w.level == 'warn')} 警告 / "
          f"{sum(1 for w in warns if w.level == 'info')} 提示"
          f"；本次輸出 {len(items)} 條")
    if errors:
        print("有錯誤，已中止（先修粵拼，唔好出卡）。")
        return 1
    if args.check:
        return 0

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    stem = deck.get("deck", "deck")
    md_path = outdir / f"{stem}.md"
    md_path.write_text(render_md(deck, items, tones), encoding="utf-8")
    csv_path = outdir / f"{stem}.csv"
    write_csv(csv_path, items)
    drill_path = outdir / f"{stem}-drill.txt"
    write_drill(drill_path, items)

    print(f"已生成：{md_path}")
    print(f"已生成：{csv_path}（Anki 直接 import，欄位 Front/Back/Tags）")
    print(f"已生成：{drill_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
