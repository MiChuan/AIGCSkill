#!/usr/bin/env python3
"""三個腳本的迴歸測試 —— 零依賴，直接跑：

    python tests/test_scripts.py

為甚麼要測：
粵拼校驗器是本 skill 的核心資產。它一旦放寬（例如把 syuet 當成合法），
之後所有詞表都會帶著錯音出去，而且沒人會發現 —— 因為錯的音看起來一樣「像粵拼」。
所以這裡把「該報錯的必須報錯」逐條釘住，而不只是測「合法的能通過」。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_plan  # noqa: E402
import check_jyutping as cj  # noqa: E402
import vocab_deck  # noqa: E402

PASSED = []


def ok(name: str) -> None:
    PASSED.append(name)
    print(f"  ✓ {name}")


def codes(issues) -> set:
    return {i.code for i in issues}


# ------------------------------------------------------------------ 粵拼校驗

def test_valid_syllables() -> None:
    for token in ("ngo5", "m4", "ng5", "gwok3", "syut3", "hoeng1", "ceot1",
                  "keoi5", "dek1", "gwaa3", "zeng3", "jyun2"):
        assert cj.check_syllables(token) == [], f"{token} 應該合法"
    ok("合法音節（含成音節鼻音 m4 / ng5、圓唇 eoi/oeng/yu、入聲 -t/-k）")


def test_mandarin_contamination() -> None:
    for token in ("zhong1", "chi1", "shi2", "xue2", "qian1", "ren2", "yu4"):
        issues = cj.check_syllables(token)
        assert "E201" in codes(issues), f"{token} 應該被判為普通話拼音：{issues}"
    ok("攔截普通話拼音（zh/ch/sh/x/q/r/y）")


def test_tone_errors() -> None:
    assert "E105" in codes(cj.check_syllables("jat"))       # 冇聲調
    assert "E105" in codes(cj.check_syllables("ngo0"))      # 聲調 0
    assert "E202" in codes(cj.check_syllables("ngó"))       # 用咗聲調符號
    assert "W301" in codes(cj.check_syllables("luk9"))      # 舊方案 7/8/9
    ok("攔截漏聲調 / 聲調 0 / 聲調符號 / 7-9 舊方案")


def test_bad_final_suggestion() -> None:
    issues = cj.check_syllables("syuet3")
    assert "E104" in codes(issues), issues
    assert any("syut" in i.msg for i in issues), "應該建議最接近的合法韻母 syut"
    ok("臆造韻母被攔，並給出近似建議")


def test_no_space() -> None:
    issues = cj.check_syllables("ngo5sik6faan6")
    assert "E102" in codes(issues)
    ok("連寫（冇空格）被攔")


def test_syllable_count() -> None:
    assert "E301" in codes(cj.check_pair("睇唔明", "tai2 m4"))    # 3 字 2 節
    assert "E301" in codes(cj.check_pair("我", "ngo5 hai6"))      # 1 字 2 節
    assert cj.check_pair("唔該", "m4 goi1") == []
    ok("漢字數 ≠ 音節數（漏寫／多寫音節）")


# ------------------------------------------------------------------ 計劃生成

def _spec(**over) -> dict:
    spec = {
        "learner": "Test",
        "level": "beginner",
        "goal": "travel",
        "minutes_per_day": 20,
        "days_per_week": 6,
        "romanization": "jyutping",
        "start_date": "2026-11-02",
        "weak_points": ["聲調"],
    }
    spec.update(over)
    return spec


def test_block_scaling_is_conserved() -> None:
    for minutes in (7, 10, 15, 20, 25, 30, 45, 60):
        blocks = build_plan.scale_blocks(minutes)
        total = sum(m for _, m, _ in blocks)
        assert total == minutes, f"{minutes} 分鐘縮放後變成 {total}"
        assert all(m >= 1 for _, m, _ in blocks), f"{minutes} 分鐘有段落被壓成 0"
    ok("五段式分鐘數守恆（7–60 分鐘皆可，無段落為 0）")


def test_validate_catches_mistakes() -> None:
    errs, _ = build_plan.validate(_spec(level="fluent"))
    assert any("E002" in e for e in errs), errs
    errs, _ = build_plan.validate(_spec(minutes_per_day=5))
    assert any("E010" in e for e in errs), errs
    _, warns = build_plan.validate(_spec(days_per_week=7))
    assert any("W013" in w for w in warns), warns
    _, warns = build_plan.validate(_spec(minutes_per_day=12))
    assert any("W010" in w for w in warns), warns
    ok("spec 校驗攔得住非法 level / 分鐘過少 / 七天全排 / 每日不足 15 分鐘")


def test_diagnostic_mismatch_warns() -> None:
    spec = _spec(level="beginner", diagnostic={
        "tone_discrimination": 2, "listen_repeat": 2, "self_intro": 2,
        "particle_recognition": 2, "read_aloud": 2, "comprehension": 2,
    })
    _, warns = build_plan.validate(spec)
    assert any("W018" in w for w in warns), warns
    ok("診斷 12/12 但 spec 寫 beginner → 報不一致")


def test_plan_structure() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        res = build_plan.build(_spec(), Path(tmp))
        md = Path(res["plan"]).read_text(encoding="utf-8")
        assert md.count("| 週") >= 28, "逐日表應該有 28 行"
        assert "輕日" in md
        rows = Path(res["schedule"]).read_text(encoding="utf-8-sig").strip().splitlines()
        assert len(rows) == 29, f"schedule.csv 應該有 28 日 + 表頭，實際 {len(rows)} 行"
        assert res["new_total"] > 0
    ok("生成 plan.md（28 日）與 schedule.csv（29 行）")


def test_light_day_is_day_seven() -> None:
    for goal in build_plan.GOALS:
        assert len(build_plan.GOALS[goal]["checkpoints"]) == 4, f"{goal} 檢核數不是 4"
    assert build_plan.DAY_PLAN[6][0] == "輕日"
    ok("每個目標都有 4 個週末檢核，第 7 日固定為輕日")


# ------------------------------------------------------------------ 詞表

def test_core_deck_is_clean() -> None:
    deck = vocab_deck.load_deck(ROOT / "specs" / "deck-core-100.json")
    errors, warns, tones = vocab_deck.audit(deck)
    assert not errors, errors
    assert len(deck["items"]) == 100
    tiers = {t: sum(1 for it in deck["items"] if it["tier"] == t) for t in (1, 2, 3)}
    assert tiers == {1: 30, 2: 45, 3: 25}, tiers
    ok("100 詞表：0 錯、分層 30/45/25")


def test_deck_tolerates_homophones() -> None:
    # 詩 si1 / 師 si1 是同音字，屬正常，唔應該報錯
    deck = {"expect": 2, "items": [
        {"hz": "詩", "jp": "si1", "en": "poem", "tier": 2, "ctx": "x"},
        {"hz": "師", "jp": "si1", "en": "teacher", "tier": 2, "ctx": "x"},
    ]}
    errors, warns, _ = vocab_deck.audit(deck)
    assert not errors, errors
    assert any(w.code == "I403" for w in warns), [w.code for w in warns]
    ok("同音字只報 info（I403），唔會誤殺")


def test_deck_catches_wrong_tone() -> None:
    # 同一個詞（多謝）出現兩個讀音 → 其中一處調號寫錯
    deck = {"expect": 2, "items": [
        {"hz": "多謝", "jp": "do1 ze6", "en": "thanks", "tier": 2, "ctx": "x"},
        {"hz": "多謝", "jp": "do1 ze2", "en": "thanks", "tier": 2, "ctx": "x"},
    ]}
    _, warns, _ = vocab_deck.audit(deck)
    assert any(w.code == "W403" for w in warns), [w.code for w in warns]
    ok("同一個詞兩種粵拼 → W403（調號寫錯的指紋）")


def test_deck_catches_missing_field() -> None:
    deck = {"expect": 1, "items": [{"hz": "你好", "jp": "nei5 hou2", "tier": 0}]}
    errors, warns, _ = vocab_deck.audit(deck)
    assert any(e.code == "E402" for e in errors), errors
    assert any(w.code == "W401" for w in warns), warns
    ok("缺 tier → E402；缺 en/ctx → W401")


def test_deck_rejects_count_mismatch() -> None:
    deck = {"expect": 100, "items": [{"hz": "你好", "jp": "nei5 hou2",
                                      "en": "hi", "tier": 1, "ctx": "x"}]}
    errors, _, _ = vocab_deck.audit(deck)
    assert any(e.code == "E401" for e in errors), errors
    ok("詞量與 expect 不符 → E401")


# ------------------------------------------------------------------

def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"跑 {len(tests)} 組測試\n")
    for fn in tests:
        fn()
    print(f"\n全部通過：{len(PASSED)} 組")
    return 0


if __name__ == "__main__":
    sys.exit(main())
