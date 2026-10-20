#!/usr/bin/env python3
"""粵語 4 週學習計劃生成器 — 零依賴。

輸入一份學習者畫像（JSON），輸出一份可直接照做的 28 天計劃：
四週主題骨架 → 每日 20 分鐘五段式 → 逐日任務 → 間隔複習排程 → 每週檢核。

設計上的三個硬約束
------------------
1. **每日分鐘數必須守恆**：五段式按比例縮放後總和必須等於 minutes_per_day，
   多出／少掉的分鐘一律補到「講」那一段（口語練習永遠可以多吃時間）。
2. **第 7 日一定係輕日**：負荷減半、唔加新詞。沒有輕日的計劃一定在第 9 天崩。
3. **複習優先於新詞**：每日第一段永遠是複習隊列，新詞只是第二、五段的事。

用法
----
    python build_plan.py --spec specs/travel-beginner.json --outdir out/plan
    python build_plan.py --spec specs/travel-beginner.json --check     # 只驗 spec
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, timedelta
from pathlib import Path

LEVELS = {
    "beginner": {
        "label": "初級（A0–A1）",
        "new_per_day": 5,
        "sentence_len": 4,
        "rule": "一次只出 4–6 字的短句，每句最多 1 個新語法點；先聽後講，唔急住糾正聲調",
    },
    "elementary": {
        "label": "中初級（A2）",
        "new_per_day": 8,
        "sentence_len": 8,
        "rule": "可以出 8–12 字句，開始加入語氣助詞；糾正聚焦「會造成誤解」的三類錯",
    },
    "intermediate": {
        "label": "中級（B1）",
        "new_per_day": 12,
        "sentence_len": 14,
        "rule": "整段對話 + 復述；重點轉向語體（口語／書面語）、慣用語、語氣助詞的態度差別",
    },
}

GOALS = {
    "travel": {
        "label": "旅行（3–7 日自由行）",
        "weeks": [
            {"theme": "打招呼、數字、禮貌三件套",
             "scenes": ["入境櫃位對答", "酒店 check-in", "便利店買水"],
             "core": ["你好", "唔該", "多謝", "幾多錢", "唔使"]},
            {"theme": "食嘢：茶餐廳同街市",
             "scenes": ["茶餐廳點餐", "走冰少甜", "埋單"],
             "core": ["餐牌", "凍奶茶", "走冰", "好食", "埋單"]},
            {"theme": "行路同坐車：地鐵、巴士、的士",
             "scenes": ["問路", "轉車", "落車"],
             "core": ["直行", "轉左", "邊度", "落車", "幾多個站"]},
            {"theme": "突發情況 + 全程串演",
             "scenes": ["唔見咗銀包", "唔舒服睇醫生", "由入境到搭車一次過"],
             "core": ["救命", "差人", "唔見咗", "幫我", "講慢啲"]},
        ],
        "checkpoints": ["用粵語完成一次點餐（唔用英文）", "聽得明 3 句問路指示並複述",
                        "自我介紹 30 秒 + 講出今日行程",
                        "由入境到埋單，8 分鐘全程只用粵語"],
    },
    "conversation": {
        "label": "日常會話（同香港朋友傾偈）",
        "weeks": [
            {"theme": "自我介紹 + 你點呀",
             "scenes": ["第一次見面", "問對方做乜嘢工", "講住邊"],
             "core": ["我叫", "你點呀", "得閒", "住", "做嘢"]},
            {"theme": "時間、安排、約人",
             "scenes": ["約食飯", "改時間", "遲到道歉"],
             "core": ["聽日", "幾點", "得唔得", "遲到", "對唔住"]},
            {"theme": "講感受同想法",
             "scenes": ["講喜好", "講今日發生咩事", "表達同意／反對"],
             "core": ["鍾意", "覺得", "好似", "但係", "其實"]},
            {"theme": "語氣助詞 + 講故事",
             "scenes": ["講一次旅行經歷", "八卦式閒聊", "收線"],
             "core": ["喇", "喎", "啩", "嘅啫", "先"]},
        ],
        "checkpoints": ["連續 2 分鐘唔轉英文", "正確用 3 個語氣助詞",
                        "聽一段 30 秒港劇對白並講出大意",
                        "同語伴傾 10 分鐘唔轉英文，教練只記錯唔打斷"],
    },
    "work": {
        "label": "工作（香港／廣州職場）",
        "weeks": [
            {"theme": "職場稱謂 + 自我介紹",
             "scenes": ["入職介紹", "問同事嘢", "打招呼"],
             "core": ["早晨", "唔好意思", "請問", "麻煩你", "唔該"]},
            {"theme": "開會：數字、進度、deadline",
             "scenes": ["講進度", "問問題", "確認分工"],
             "core": ["進度", "搞掂", "跟進", "下個禮拜", "有冇問題"]},
            {"theme": "口語 ↔ 書面語轉換",
             "scenes": ["寫 email", "會議紀錄", "口頭匯報"],
             "core": ["嘅→的", "咗→了", "喺→在", "唔→不", "佢→他"]},
            {"theme": "電話、協商、婉拒",
             "scenes": ["接電話", "推遲", "禮貌拒絕"],
             "core": ["唔好意思", "要唔要", "睇下", "再傾", "冇問題"]},
        ],
        "checkpoints": ["用粵語講 1 分鐘工作匯報", "把 3 句口語改寫成書面語",
                        "電話情境角色扮演（對方追進度）",
                        "5 分鐘會議發言 + 寫 1 封粵語 email"],
    },
    "family": {
        "label": "家庭（同長輩／伴侶家人溝通）",
        "weeks": [
            {"theme": "稱謂同問候",
             "scenes": ["見家長", "打電話問候", "拜年"],
             "core": ["早晨", "食咗飯未", "身體好嗎", "唔該", "多謝"]},
            {"theme": "食飯枱上的對話",
             "scenes": ["夾餸", "讚好食", "話飽"],
             "core": ["好味", "食多啲", "飽喇", "唔使客氣", "慢慢食"]},
            {"theme": "講自己的生活",
             "scenes": ["講工作", "講計劃", "聽長輩講舊事"],
             "core": ["做嘢", "打算", "以前", "嗰陣時", "而家"]},
            {"theme": "關心同勸說",
             "scenes": ["勸休息", "講健康", "收線問候"],
             "core": ["早啲唞", "小心", "唔好", "記得", "得閒嚟坐"]},
        ],
        "checkpoints": ["同長輩傾 3 分鐘電話", "用 5 個稱謂正確叫人", "講一件童年往事 1 分鐘",
                        "同長輩食一餐飯，全程粵語"],
    },
    "exam": {
        "label": "考試／評核（粵語能力測試）",
        "weeks": [
            {"theme": "語音盤點：六調 + 入聲",
             "scenes": ["朗讀字表", "最小對立辨音", "聲調聽寫"],
             "core": ["詩史試時市事", "一六九十", "三心", "街雞"]},
            {"theme": "語法與助詞專項",
             "scenes": ["改錯句", "填空", "句子重組"],
             "core": ["咗", "緊", "過", "嘅", "喺"]},
            {"theme": "聽力 + 復述",
             "scenes": ["新聞摘要", "對話問答", "聽寫"],
             "core": ["數字", "時間", "地點", "轉折詞"]},
            {"theme": "模擬卷 + 弱項回補",
             "scenes": ["全真模擬", "錯題重做", "口試自錄"],
             "core": ["錯題本全部"]},
        ],
        "checkpoints": ["六調聽寫 ≥ 90%", "改錯 10 句 ≥ 8 句", "3 分鐘即興講話唔超過 3 個錯",
                        "全真模擬卷一次，錯題本清空一半"],
    },
}

# 五段式（20 分鐘基準），縮放時保持比例
BLOCKS = (
    ("熱身複習", 3, "打開複習隊列，只做「到期」的卡片；答錯的立刻造句一次"),
    ("聽＋影子跟讀", 5, "1 段 30–60 秒音檔，先盲聽兩次，再落後 1.5 秒跟讀"),
    ("講／角色扮演", 5, "用今日場景自問自答；指定要用滿 3 個新詞 + 1 個語氣助詞"),
    ("讀＋語法", 4, "讀 5 行對話，逐字對粵拼；只挑 1 個語法點講透"),
    ("寫＋錯題", 3, "寫 3 句自己的句子；把今日的錯記進錯題本（寫錯因，不寫正確答案）"),
)

DAY_PLAN = (
    ("新詞輸入", "新詞全部 + 聽辨：只求聽得出、讀得鬱，唔求記牢", 1.0),
    ("聽力日", "同一段音檔聽 3 次：盲聽 → 看粵拼 → 影子跟讀；唔加新詞", 0.0),
    ("角色扮演", "本週場景實演，教練模式（即時糾正）；錄音 1 次", 0.0),
    ("語法日", "1 個語法點 + 5 句替換練習；新詞只補一半", 0.5),
    ("新詞＋記憶法", "新詞全部 + 為每個詞造一個畫面或諧音鉤子", 1.0),
    ("綜合對話", "把本週兩個場景串成一段 8–12 輪對話；結束後自評", 0.0),
    ("輕日", "只做複習隊列 + 重聽一段舊音檔；負荷減半，唔加新詞", 0.0),
)

WEEKDAYS = ("一", "二", "三", "四", "五", "六", "日")

# 六題診斷，每題 0–2 分，總分 0–12（題目見 references/curriculum.md）
DIAG_KEYS = (
    ("tone_discrimination", "聲調辨別"),
    ("listen_repeat", "聽－複述"),
    ("self_intro", "自我介紹"),
    ("particle_recognition", "語氣助詞"),
    ("read_aloud", "朗讀"),
    ("comprehension", "聽解"),
)
# 總分下限 → (等級, 對應 spec.level)
DIAG_BANDS = ((2, "A0", "beginner"), (6, "A1", "beginner"),
              (10, "A2", "elementary"), (12, "B1", "intermediate"))


def diag_band(total: int) -> tuple:
    for top, code, level in DIAG_BANDS:
        if total <= top:
            return code, level
    return "B1", "intermediate"


# ------------------------------------------------------------------ 校驗

def load_spec(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("spec 必須係一個 JSON object")
    return data


def validate(spec: dict) -> tuple:
    errors, warns = [], []

    def need(key, allowed=None, kind=None):
        if key not in spec:
            errors.append(f"E001 缺少欄位 `{key}`")
            return None
        val = spec[key]
        if allowed is not None and val not in allowed:
            errors.append(f"E002 `{key}` = {val!r} 唔合法，可選：{sorted(allowed)}")
        if kind and not isinstance(val, kind):
            errors.append(f"E003 `{key}` 型別應該係 {kind.__name__}")
        return val

    need("level", allowed=set(LEVELS))
    need("goal", allowed=set(GOALS))
    minutes = need("minutes_per_day", kind=int)
    days = need("days_per_week", kind=int)
    need("romanization", allowed={"jyutping", "yale"})
    start = need("start_date")
    spec.setdefault("learner", "學習者")
    spec.setdefault("interests", [])
    spec.setdefault("weak_points", [])

    if isinstance(minutes, int):
        if minutes < 10:
            errors.append("E010 `minutes_per_day` 少於 10 分鐘：連熱身都做唔完")
        elif minutes < 15:
            warns.append("W010 每日少於 15 分鐘：只夠維持，唔夠推進；建議 15–25")
        elif minutes > 60:
            warns.append("W011 每日超過 60 分鐘：連續 4 週的維持率很低，建議 20–30 並拆兩次")
        if spec.get("level") == "beginner" and minutes > 45:
            warns.append("W012 初級一次過 45 分鐘以上：注意力會在第 20 分鐘斷，拆成兩段")

    if isinstance(days, int) and days > 6:
        warns.append("W013 一星期 7 日冇休息日：第 9–12 天最容易斷，建議 6 日 + 1 日自由")

    if spec.get("romanization") == "yale":
        warns.append("W014 用耶魯式：教材對得上，但詞表、字典、輸入法多數係粵拼，"
                     "本 skill 仍會同時保留粵拼對照")

    if spec.get("goal") == "work" and spec.get("level") == "beginner":
        warns.append("W015 職場目標但係初級起點：先用兩週補 100 核心詞，"
                     "第 3 週的書面語轉換才唔會變成背表格")

    if isinstance(start, str):
        try:
            d = date.fromisoformat(start)
            if d < date.today():
                warns.append(f"W016 `start_date` {start} 已經過去，日程會從該日開始排")
        except ValueError:
            errors.append("E004 `start_date` 必須係 YYYY-MM-DD")

    if not spec.get("weak_points"):
        warns.append("W017 冇填 `weak_points`：第 4 週的弱項回補會冇目標，"
                     "建議先做一次診斷（見 references/curriculum.md）")

    diag = spec.get("diagnostic")
    if isinstance(diag, dict):
        missing = [k for k, _ in DIAG_KEYS if k not in diag]
        if missing:
            warns.append(f"W019 診斷缺 {len(missing)} 題（{', '.join(missing)}），總分會偏低")
        total = sum(int(diag.get(k, 0)) for k, _ in DIAG_KEYS)
        code, level_from_diag = diag_band(total)
        if spec.get("level") and level_from_diag != spec["level"]:
            warns.append(f"W018 診斷 {total}/12 對應 **{code}**（{level_from_diag}），"
                         f"但 spec 寫 `level={spec['level']}`；"
                         f"兩者唔一致時以診斷為準，或者面談一次再定")

    return errors, warns


def scale_blocks(minutes: int) -> list:
    """把五段式縮放到指定分鐘數，總和必須精確等於 minutes。"""
    total = sum(b[1] for b in BLOCKS)
    scaled = []
    for name, base, task in BLOCKS:
        if name == "講／角色扮演":
            scaled.append([name, 0, task])  # 先佔位，尾數補進來
            continue
        scaled.append([name, max(1, round(base / total * minutes)), task])
    used = sum(b[1] for b in scaled)
    for blk in scaled:
        if blk[0] == "講／角色扮演":
            blk[1] = max(1, minutes - used)
            used += blk[1]
    drift = minutes - used
    if drift:
        for blk in scaled:
            if blk[0] == "講／角色扮演":
                blk[1] = max(1, blk[1] + drift)
                break
    return [(n, m, t) for n, m, t in scaled]


# ------------------------------------------------------------------ 生成

def build(spec: dict, outdir: Path) -> dict:
    level = LEVELS[spec["level"]]
    goal = GOALS[spec["goal"]]
    start = date.fromisoformat(spec["start_date"])
    name = spec.get("learner") or "學習者"
    minutes = int(spec["minutes_per_day"])
    per_day = level["new_per_day"]
    blocks = scale_blocks(minutes)

    rows = []
    for w_idx, week in enumerate(goal["weeks"], 1):
        w_start = start + timedelta(days=7 * (w_idx - 1))
        for d_idx, (focus, task, load) in enumerate(DAY_PLAN):
            day = w_start + timedelta(days=d_idx)
            new_n = int(round(per_day * load))
            scene = week["scenes"][d_idx % len(week["scenes"])]
            rows.append({
                "week": w_idx,
                "day": d_idx + 1,
                "date": day,
                "weekday": WEEKDAYS[day.weekday()],
                "theme": week["theme"],
                "focus": focus,
                "task": task,
                "scene": scene,
                "new_items": new_n,
                "light": d_idx == 6,
            })

    # 間隔複習排程：每週的新詞，在 D+1 / +2 / +4 / +7 / +15 / +30 回到隊列
    srs_offsets = (1, 2, 4, 7, 15, 30)
    srs = []
    for w_idx in range(1, 5):
        w_start = start + timedelta(days=7 * (w_idx - 1))
        total_new = sum(r["new_items"] for r in rows if r["week"] == w_idx)
        srs.append({
            "week": w_idx,
            "label": f"第 {w_idx} 週新詞（約 {total_new} 個）",
            "base": w_start,
            "dates": [w_start + timedelta(days=o) for o in srs_offsets],
        })

    outdir.mkdir(parents=True, exist_ok=True)
    md = render_md(spec, level, goal, start, blocks, rows, srs)
    (outdir / "plan.md").write_text(md, encoding="utf-8")

    with (outdir / "schedule.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "week", "day", "weekday", "theme", "focus",
                    "scene", "task", "new_items", "light_day"])
        for r in rows:
            w.writerow([r["date"].isoformat(), r["week"], r["day"], r["weekday"],
                        r["theme"], r["focus"], r["scene"], r["task"],
                        r["new_items"], "yes" if r["light"] else "no"])

    return {"plan": outdir / "plan.md", "schedule": outdir / "schedule.csv",
            "new_total": sum(r["new_items"] for r in rows)}


def render_md(spec, level, goal, start, blocks, rows, srs) -> str:
    name = spec.get("learner") or "學習者"
    end = start + timedelta(days=27)
    minutes = int(spec["minutes_per_day"])
    rom = "粵拼 Jyutping" if spec["romanization"] == "jyutping" else "耶魯 Yale（附粵拼對照）"
    out = []
    a = out.append

    a(f"# 四週粵語學習計劃 — {name}")
    a("")
    a(f"- 目標：**{goal['label']}**　起點：**{level['label']}**")
    a(f"- 期間：{start.isoformat()} → {end.isoformat()}（28 日，每週 {spec['days_per_week']} 日）")
    a(f"- 每日投入：{minutes} 分鐘　羅馬方案：{rom}")
    a(f"- 新詞總量：約 {sum(r['new_items'] for r in rows)} 個（每學習日 {level['new_per_day']} 個上限）")
    if spec.get("weak_points"):
        a(f"- 已知短板：{'、'.join(spec['weak_points'])}")
    if spec.get("interests"):
        a(f"- 可借力的興趣：{'、'.join(spec['interests'])}（例句、聽力材料優先從這裡取材）")
    a("")
    a(f"> 起點規則：{level['rule']}")
    a("")

    diag = spec.get("diagnostic")
    if isinstance(diag, dict):
        total = sum(int(diag.get(k, 0)) for k, _ in DIAG_KEYS)
        code, _ = diag_band(total)
        a("## 1. 診斷起點")
        a("")
        a("| 項 | 得分（0–2） | 判讀 |")
        a("| --- | --- | --- |")
        for key, label in DIAG_KEYS:
            score = int(diag.get(key, 0))
            note = {0: "未建立", 1: "聽得明／講得到一半", 2: "穩定，可以在此之上加料"}[max(0, min(2, score))]
            a(f"| {label} | {score} | {note} |")
        a(f"| **合計** | **{total}/12** | 對應 **{code}** |")
        a("")
        weak = sorted(((int(diag.get(k, 0)), lbl) for k, lbl in DIAG_KEYS))[:2]
        a(f"最需要補的兩項：**{weak[0][1]}**、**{weak[1][1]}**。"
          f"這兩項唔會單獨開課，而是塞進每日五段式 —— 弱項在「聽＋影子跟讀」和「講」兩段加做一輪。")
        a("")

    a("## 2. 四週骨架")
    a("")
    a("| 週 | 日期 | 主題 | 反覆出現的場景 | 核心詞 | 週末檢核 |")
    a("| --- | --- | --- | --- | --- | --- |")
    for i, wk in enumerate(goal["weeks"], 1):
        ws = start + timedelta(days=7 * (i - 1))
        we = ws + timedelta(days=6)
        a(f"| 第 {i} 週 | {ws.strftime('%m/%d')}–{we.strftime('%m/%d')} "
          f"| {wk['theme']} | {'、'.join(wk['scenes'])} "
          f"| {'、'.join(wk['core'])} | {goal['checkpoints'][i - 1]} |")
    a("")
    a("四週的關係不是「四份清單」，是**同一批詞反覆換場景**：第 1 週的 `唔該` 會在第 2 週變 `唔該埋單`，"
      "第 3 週變 `唔該借借`。所以每週的「核心詞」只有 5 個，但它們會以不同搭配出現 4 次。")
    a("")

    a(f"## 3. 每日 {minutes} 分鐘：五段式")
    a("")
    a("| 段落 | 分鐘 | 做乜 |")
    a("| --- | --- | --- |")
    for n, m, t in blocks:
        a(f"| {n} | {m} | {t} |")
    a(f"| **合計** | **{sum(m for _, m, _ in blocks)}** | 五段順序唔可以調：先複習、再輸入、再開口、最後落筆 |")
    a("")
    a("順序的理由：**開口要放在聽之後**（沒有輸入的輸出只是在重複自己的錯），"
      "**寫要放在最後**（寫是最慢的動作，放前面會吃掉全部時間）。")
    a("")

    a("## 4. 逐日安排")
    a("")
    for w_idx in range(1, 5):
        wk_rows = [r for r in rows if r["week"] == w_idx]
        ws = wk_rows[0]["date"]
        a(f"### 第 {w_idx} 週 · {ws.strftime('%m/%d')}（{goal['weeks'][w_idx - 1]['theme']}）")
        a("")
        a("| 日 | 日期 | 今日任務 | 場景 | 新詞 |")
        a("| --- | --- | --- | --- | --- |")
        for r in wk_rows:
            mark = "（輕日）" if r["light"] and r["focus"] != "輕日" else ""
            a(f"| 週{r['weekday']} | {r['date'].strftime('%m/%d')} | "
              f"**{r['focus']}**{mark}：{r['task']} | {r['scene']} | {r['new_items']} |")
        a("")

    a("## 5. 間隔複習排程")
    a("")
    a("| 內容 | 首學 | D+1 | D+2 | D+4 | D+7 | D+15 | D+30 |")
    a("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for s in srs:
        cells = " | ".join(d.strftime("%m/%d") for d in s["dates"])
        a(f"| {s['label']} | {s['base'].strftime('%m/%d')} | {cells} |")
    a("")
    a("規則：**複習卡片答錯兩次以上，就退回上一個間隔**，不要重排一輪。"
      "新詞入隊列時只寫三樣：漢字、粵拼、你自己的造句（不要抄例句）。")
    a("")

    a("## 6. 可持續性四條")
    a("")
    a("1. **最低版 5 分鐘**：狀態差的日子只做兩件事 —— 複習隊列 + 讀 5 行對話。做了就算過關。")
    a("2. **缺日不補**：跳過一日就直接接下一日，唔好把兩日疊埋。疊一次通常就斷一週。")
    a(f"3. **每週留一日自由**：計劃排 6 日，第 7 日只做「輕日」，用來吸收而唔係推進。")
    a("4. **錯題本只寫錯因**：寫「我漏咗 嘅」而唔係寫正確答案 —— 寫正確答案只會練到抄寫。")
    a("")

    a("## 7. 每週檢核")
    a("")
    a("| 週 | 檢核（做得到才算過） |")
    a("| --- | --- |")
    for i, cp in enumerate(goal["checkpoints"], 1):
        a(f"| 第 {i} 週 | {cp} |")
    a("")
    a("檢核唔過唔好前進：把該週的「講／角色扮演」日重做兩次，成本遠低於帶著錯往前跑。")
    a("")

    a("## 8. 下一步")
    a("")
    a("- 想即刻開第一課：把「第 1 週第 1 日」那 5 個新詞交給 `vocab_deck.py` 出卡片，"
      "再叫教練按 `SKILL.md` 的 M3 開一段角色扮演。")
    a("- 想先確認起點：做 `references/curriculum.md` 的六題診斷，把分數寫入 spec 重跑本腳本。")
    a("- 詞表、語法點、發音 drill 的細則在 `references/`，唔好塞進每日任務裡 —— 每日只帶走 1 個語法點。")
    a("")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="粵語 4 週計劃生成器")
    ap.add_argument("--spec", required=True)
    ap.add_argument("--outdir", default="out/plan")
    ap.add_argument("--check", action="store_true", help="只驗 spec，唔生成")
    args = ap.parse_args(argv)

    spec = load_spec(Path(args.spec))
    errors, warns = validate(spec)
    for w in warns:
        print(f"[warn ] {w}")
    for e in errors:
        print(f"[ERROR] {e}")
    if errors:
        print(f"\n{len(errors)} 個錯誤，已中止。")
        return 1
    if args.check:
        print("\nspec 通過。")
        return 0

    res = build(spec, Path(args.outdir))
    print(f"\n已生成：{res['plan']}")
    print(f"已生成：{res['schedule']}")
    print(f"28 日新詞合計：{res['new_total']} 個")
    return 0


if __name__ == "__main__":
    sys.exit(main())
