#!/usr/bin/env python3
"""提示词组装器 — 把 features.json 翻译成可直接喂给音频生成模型的 prompt。

为甚么要有这一层
----------------
analyze_audio.py 产出的是数字，但生成模型只吃自然语言的连串短语。
这中间的翻译是最容易「悄悄编造」的一步：看到质心 900 Hz 就写 moody jazz，
看到 120 BPM 就写 four-on-the-floor，写出来很顺，回头完全没法验证。

所以本脚本有一条硬约束：**每个进 prompt 的短语都要带一条 trace** ——
它来自哪个测量量、当时的读数是多少、阈值是多少。
没有 trace 的短语只能来自用户 spec（人也得负责），否则不允许进入输出。

用法
----
    python build_prompt.py out/track/features.json --outdir out/track
    python build_prompt.py features.json --spec specs/demo.json --outdir out/track
    python build_prompt.py features.json --kind sfx --outdir out/sfx --lang en
    python build_prompt.py features.json --print           # 只打到 stdout，不落盘

退出码：0 = 正常；2 = 输入问题；1 = 组装出的 prompt 里有槽位为空且无法给出候选。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import analyze_audio as A  # noqa: E402

LEX_DEFAULT = HERE.parent / "data" / "lexicon.json"


class BuildError(RuntimeError):
    pass


# ------------------------------------------------------------------ 规则求值

def _get_path(d: dict, dotted: str):
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def eval_when(when: dict, f: dict, t: dict) -> tuple[float, list[str], list[str]]:
    """对一个 when 条件块求值，返回 (命中率, 命中项, 未命中项)。

    支持四种条件写法，都在 lexicon.json 里：
    - 数值区间：{"tempo_bpm": [70, 100]}
    - 单边阈值：{"band_ratios.air_min": 0.05}
    - 标签命中：{"traits": {"brightness": ["dark", "very_dark"]}}
    - 枚举相等：{"key_mode": "minor"} —— 只在 key_confidence ≥ 0.55 时才算数，
      否则「瞎猜出来的小调」也能把情绪带偏
    """
    gated = False
    if _KEY_CONF(f) < 0.55 and "key_mode" in when:
        gated = True
    if _KEY_CONF(f) < 0.55 and "key_mode" in when:
        # key_mode 常常是某条规则的**区分性**条件。判调不可信时如果直接把它删掉，
        # 规则会靠剩下的通用条件（偏暗 + 大动态）拿到满分 —— 于是任何暗一点的素材
        # 都会被判成「忧郁小调」。所以这里算作未命中，而不是忽略。
        when = {k: v for k, v in when.items() if k != "key_mode"}

    hit: list[str] = []
    if gated:
        miss = [f"key_mode 不可信（key_confidence={_KEY_CONF(f):.2f} < 0.55）"]
    else:
        miss = []
    for key, want in when.items():
        if key == "traits":
            for tk, allowed in want.items():
                label = f"{tk}={t.get(tk)}"
                (hit if t.get(tk) in allowed else miss).append(label)
            continue
        if key.endswith("_min") or key.endswith("_max"):
            base = key.rsplit("_", 1)[0]
            val = _get_path(f, base)
            if val is None:
                miss.append(f"{key}(缺字段)")
                continue
            ok = val >= want if key.endswith("_min") else val <= want
            (hit if ok else miss).append(f"{base}={round(val, 4)}{'>=' if key.endswith('_min') else '<='}{want}")
            continue
        if isinstance(want, list) and len(want) == 2 and all(isinstance(x, (int, float)) for x in want):
            val = _get_path(f, key)
            if val is None:
                miss.append(f"{key}(缺字段)")
                continue
            ok = want[0] <= val <= want[1]
            (hit if ok else miss).append(f"{key}={round(val, 3)}∈[{want[0]},{want[1]}]")
            continue
        if isinstance(want, bool):
            val = _get_path(f, key)
            (hit if bool(val) is want else miss).append(f"{key}={val}")
            continue
        val = _get_path(f, key)
        (hit if val == want else miss).append(f"{key}={val}(要 {want})")

    total = len(hit) + len(miss)
    return (len(hit) / total if total else 0.0), hit, miss


def _KEY_CONF(f: dict) -> float:
    return float(f.get("key_confidence", 0) or 0)


def rank(bank: list[dict], f: dict, t: dict, threshold: float = 0.6) -> list[dict]:
    """给规则库排序。三条过滤串起来：

    1. `unless` —— 否决项，命中任意一条直接出局。这是防止「有拍的东西被判成无拍捣音墙」
       的唯一有效手段；光靠打分，一个 5 条件的规则漏掉 1 条还能拿 0.8 分。
    2. `must` —— 必要条件，缺一条出局。
    3. `when` —— 软性打分，只有过了前两关才轮到它。
    """
    out = []
    for rule in bank:
        veto = rule.get("unless", {})
        if veto:
            _, vhit, _ = eval_when(veto, f, t)
            if vhit:
                continue
        need = rule.get("must", {})
        if need:
            nscore, nhit, nmiss = eval_when(need, f, t)
            if nscore < 1.0:
                continue
        score, hit, miss = eval_when(rule.get("when", {}), f, t)
        if score < threshold:
            continue
        out.append({"rule": rule, "score": round(score, 3), "hit": hit, "miss": miss})
    out.sort(key=lambda x: -x["score"])
    return out


# ------------------------------------------------------------------ 槽位组装

def _words(item: dict, lang: str) -> list[str]:
    w = item.get("words", {})
    return list(w.get(lang, []))


def _desc(lex: dict, group: str, name: str, lang: str) -> tuple[str, str]:
    d = lex["descriptors"].get(group, {}).get(name, {})
    return d.get(lang, ""), d.get("trace", "")


def build_music(f: dict, t: dict, lex: dict, spec: dict, lang: str) -> tuple[dict, list[str]]:
    """音乐八槽。返回 (slots, open_questions)。"""
    specs_ints = spec.get("instruments_seen") or []
    open_q: list[str] = []
    S: dict[str, list[tuple[str, str]]] = {k: [] for k in lex["slot_order"]["music"]}

    # 1. 类型与场景 —— 只给候选，第一名不达标就明说「没判定出来」
    gen = rank(lex["genre_bank"], f, t)
    if spec.get("genre_hint"):
        S["scene_genre"].append((spec["genre_hint"], "user:genre_hint"))
    if gen:
        top = gen[0]
        text = (top["rule"]["en"] if lang == "en" else top["rule"]["zh"]) if len(top["rule"]["zh"] or "") > 0 else top["rule"]["en"]
        S["scene_genre"].append((top["rule"]["en"] if lang == "en" else top["rule"]["zh"],
                                 f"genre:{top['rule']['id']} 命中率 {top['score']:.2f} | {' '.join(top['hit'][:3])}"))
        scene_words = _words(top["rule"], lang)
        if scene_words:
            S["scene_genre"].append(("、".join(scene_words) if lang == "zh" else ", ".join(scene_words),
                                     f"genre:{top['rule']['id']} 自带风格词"))
        if len(gen) > 1 and gen[1]["score"] >= 0.6:
            alt = gen[1]["rule"]
            open_q.append(f"还像 {alt['en']} / {alt['zh']}（命中率 {gen[1]['score']:.2f}），二选一由你定")
    else:
        open_q.append("类型没判定出来（所有候选命中率都 < 0.6）：告诉我是哪种风格，第 1 槽才有依据")

    # 2. 情绪
    # 情绪门槛 0.8：情绪是唯一一个没有「错也不会立刻被发现」的槽位，
    # 宁可少写一个氛围词，也不要给一段明朗的曲子扣上「紧张压抑」。
    moods = rank(lex["mood_bank"], f, t, threshold=0.8)
    if moods:
        for m in moods[:2]:
            r = m["rule"]
            S["mood"].append((r["en"] if lang == "en" else r["zh"],
                              f"mood:{r['id']} 命中率 {m['score']:.2f} | {' '.join(m['hit'][:3])}"))
    if f.get("key_confidence", 0) >= 0.55:
        mode = f.get("key_mode", "")
        S["mood"].append((f"{f['key']} {'major — brighter color' if lang == 'en' and mode == 'major' else 'minor — darker color' if lang == 'en' else ('大调偏亮' if mode == 'major' else '小调偏暗')}",
                          f"key={f['key']} {mode} 置信 {f['key_confidence']:.2f}"))

    # 3. 配器 —— 用户说的一律优先，机器推断降一级摆后面。
    #    乐器门槛 0.85 而不是 0.6：三条件的规则漏一条，靠软打分也能混进来，
    #    结果就是一段光秃秃的鼓循环被配上「钢琴 + 弦乐」。
    insts = rank(lex["instrument_bank"], f, t, threshold=0.85)
    for i in specs_ints:
        S["instrumentation"].append((str(i), "user:instruments_seen"))
    if insts:
        names = [x["rule"]["en"] if lang == "en" else x["rule"]["zh"] for x in insts[:3]]
        S["instrumentation"].append(("、".join(names) if lang == "zh" else ", ".join(names),
                                     "推断：" + "; ".join(f"{x['rule']['id']}({x['score']:.2f})" for x in insts[:4])))
    if not insts and not specs_ints:
        open_q.append("配器没推断出任何乐器：直接说你听到了什么（例：钢琴 + 贝斯 + 鼓）")

    # 4. 速度 / 调性 / 律动
    if f.get("tempo_bpm", 0) > 0:
        bpm_txt = f"{round(f['tempo_bpm'])} BPM"
        if f.get("tempo_alt_bpm", 0) > 0:
            bpm_txt += (f"（或按 {round(f['tempo_alt_bpm'])} BPM 数，半速 / 倍速听感都成立）"
                        if lang == "zh" else f" (equally countable as {round(f['tempo_alt_bpm'])} BPM)")
        S["rhythm_key"].append((bpm_txt, f"tempo_bpm={f['tempo_bpm']} 强度 {f.get('tempo_strength', 0)} 清晰度 {f['beat_clarity']}"))
    else:
        S["rhythm_key"].append(("无明确节拍（自由速度）" if lang == "zh" else "no steady pulse (free time)",
                                f"tempo_strength={f.get('tempo_strength', 0)} 未达门槛"))
    if f.get("key_confidence", 0) >= 0.55:
        S["rhythm_key"].append((f"调性 {f['key']} {f['key_mode']}" if lang == "zh"
                                else f"in {f['key']} {f['key_mode']}",
                                f"key_confidence={f['key_confidence']:.2f}"))
    groove_from_map = {
        "sparse": ("开阔不塞满" if lang == "zh" else "open, unhurried"),
        "medium": ("中等密度律动" if lang == "zh" else "moderately busy groove"),
        "dense": ("密集律动" if lang == "zh" else "busy, tightly packed groove"),
    }
    S["rhythm_key"].append((groove_from_map[t["density"]],
                            f"onset_rate={f['onset_rate']}/s → density={t['density']}"))

    # 5. 音色与制作
    for group, key in (("brightness", "centroid_hz"), ("texture", "flatness")):
        txt, tr = _desc(lex, group, t[group], lang)
        S["timbre"].append((txt, f"{key}={f.get(key)} → {group}={t[group]}（判据 {tr}）"))

    # 6. 人声
    vocal_spec = spec.get("vocal") or {}
    if vocal_spec.get("present") is False:
        S["vocal"].append(("纯器乐，无人声" if lang == "zh" else "strictly instrumental, no vocals",
                           "user:vocal.present=false"))
    elif t["vocal_presence"] == "prominent" or vocal_spec.get("present") is True:
        parts = []
        if vocal_spec.get("language"):
            parts.append(str(vocal_spec["language"]))
        if vocal_spec.get("gender"):
            parts.append(str(vocal_spec["gender"]))
        lead = ("、".join(parts) + " 人声" if lang == "zh" and parts else
                (" ".join(parts) + " lead vocal" if parts else ("有人声" if lang == "zh" else "lead vocal present")))
        S["vocal"].append((lead, f"voiced_ratio={f['voiced_ratio']} 段数={f.get('voiced_segments', 0)}"
                                 f" f0≈{f.get('f0_hz', 0):.0f}Hz"))
        if not vocal_spec.get("language"):
            open_q.append("有人声但语言未知：告诉我语种，否则模型会默认吐英文 vocal")
    elif t["vocal_presence"] == "some":
        S["vocal"].append(("零星人声 / 人声采样（不确定）" if lang == "zh" else "occasional vocal texture (uncertain)",
                           f"voiced_ratio={f['voiced_ratio']} 落在 some 档"))
        open_q.append("voiced_ratio 落在中间档：到底是有人声哼唱还是乐器音的误判，你听一下定")
    else:
        S["vocal"].append(("纯器乐，无人声" if lang == "zh" else "strictly instrumental, no vocals",
                           f"voiced_ratio={f['voiced_ratio']} → instrumental"))

    # 7. 结构
    structure = _structure(f, lex, lang)
    S["structure"].append((structure, f"duration_s={f['duration_s']}"))
    if spec.get("usage"):
        S["structure"].append((str(spec["usage"]), "user:usage"))

    # 8. 混音与母带
    width_txt, width_tr = _desc(lex, "width", t["width"], lang)
    S["mix"].append((width_txt, f"stereo_corr={f['stereo_corr']} side_ratio={f['side_ratio']} → {t['width']}"))
    space_txt, space_tr = _desc(lex, "space", t["space"], lang)
    S["mix"].append((space_txt, f"reverb_tail_ms={f['reverb_tail_ms']} → {t['space']}"))
    low_txt, low_tr = _desc(lex, "low_end", t["low_end"], lang)
    S["mix"].append((low_txt, f"sub+bass={round(f['band_ratios'].get('sub', 0) + f['band_ratios'].get('bass', 0), 3)} → {t['low_end']}"))
    energy_txt, energy_tr = _desc(lex, "energy", t["energy"], lang)
    S["mix"].append((energy_txt, f"rms_dbfs={f['rms_dbfs']} → {t['energy']}"))
    if f.get("clip_ratio", 0) > 0.001:
        S["mix"].append(("注意：素材本身已削波，生成时留 headroom" if lang == "zh"
                         else "source is clipping, leave headroom",
                         f"clip_ratio={f['clip_ratio']}"))

    return S, open_q


def _structure(f: dict, lex: dict, lang: str) -> str:
    for row in lex["structures"]:
        if f["duration_s"] <= row["max_duration_s"]:
            return row["en"] if lang == "en" else row["zh"]
    return lex["structures"][-1]["zh"]


def build_sfx(f: dict, t: dict, lex: dict, spec: dict, lang: str) -> tuple[dict, list[str]]:
    """音效七槽：这里机器能给的只有包络与频谱，声源与用途只能人来补。"""
    open_q: list[str] = []
    S: dict[str, list[tuple[str, str]]] = {k: [] for k in lex["slot_order"]["sfx"]}

    if spec.get("source_action"):
        S["source_action"].append((str(spec["source_action"]), "user:source_action"))
    else:
        S["source_action"].append(("【待填：这是什么东西在发声 / 什么动作】" if lang == "zh"
                                   else "[FILL: what object and what action]", "缺 UserKnowledge"))
        open_q.append("音效必须说清「什么物体、什么动作」——这步机器听不出来，只能你补")

    mats = rank(lex["material_bank"], f, t, threshold=0.5)
    if mats:
        names = [x["rule"]["en"] if lang == "en" else x["rule"]["zh"] for x in mats[:2]]
        S["material"].append(("、".join(names) if lang == "zh" else " / ".join(names),
                              "推断：" + "; ".join(f"{x['rule']['id']}({x['score']:.2f})" for x in mats[:2])))

    crest = f.get("crest_db", 0)
    if lang == "zh":
        attack = ("attack 极快，瞬态锐利" if crest > 14 else
                  "attack 偏软，能听出起振过程" if crest < 10 else "attack 适中的瞬态")
    else:
        attack = ("very fast attack, sharp transient" if crest > 14 else
                  "soft attack with audible onset ramp" if crest < 10 else "moderate attack")
    tail = f.get("reverb_tail_ms", 0)
    decay = (f"衰减到 -20 dB 约 {tail} ms" if lang == "zh" else f"decays roughly {tail} ms to -20 dB")
    S["envelope"].append((f"{attack}，{decay}", f"crest_db={crest} reverb_tail_ms={tail}"))
    if f.get("fade_out_s", 0) < 0.05:
        S["envelope"].append(("尾巴被硬切，生成时留自然衰减" if lang == "zh" else "tail is cut off, render natural decay",
                             f"fade_out_s={f['fade_out_s']}"))

    br_txt, br_tr = _desc(lex, "brightness", t["brightness"], lang)
    tx_txt, tx_tr = _desc(lex, "texture", t["texture"], lang)
    bands = f["band_ratios"]
    tilt = max(bands, key=lambda k: bands[k])
    tilt_map = {"sub": ("能量集中在超低频" if lang == "zh" else "energy centered in sub"),
                "bass": ("能量集中在低频" if lang == "zh" else "energy centered in bass"),
                "lowmid": ("能量集中在低中频" if lang == "zh" else "energy centered in low-mids"),
                "mid": ("能量集中在中频" if lang == "zh" else "energy centered in mids"),
                "highmid": ("能量集中在中高频" if lang == "zh" else "energy centered in high-mids"),
                "air": ("能量集中在高频空气区" if lang == "zh" else "energy centered in air band")}
    S["spectral"].append((f"{br_txt}；{tx_txt}；{tilt_map[tilt]}",
                          f"centroid_hz={f['centroid_hz']} flatness={f['flatness']} band_ratios 最大项={tilt}"))
    if f.get("centroid_slope_hz_s", 0) > 300:
        S["spectral"].append(("音高 / 亮度随时间上扬" if lang == "zh" else "pitch or brightness rises over time",
                              f"centroid_slope_hz_s={f['centroid_slope_hz_s']}"))

    sp_txt, sp_tr = _desc(lex, "space", t["space"], lang)
    S["space"].append((sp_txt, f"reverb_tail_ms={tail} → {t['space']}"))

    S["duration"].append((f"约 {f['duration_s']:.2f} 秒" if lang == "zh" else f"about {f['duration_s']:.2f} seconds",
                          f"duration_s={f['duration_s']}"))

    if bands.get("air", 0) < 0.01 and t["space"] in ("hall", "cavern"):
        S["perspective"].append(("远处录的：高频衰减明显" if lang == "zh" else "recorded at distance with rolled-off highs",
                                 f"air={bands.get('air')} space={t['space']}"))
    elif t["space"] == "dry":
        S["perspective"].append(("近距拾音，几乎无房间反射" if lang == "zh" else "close-miked, almost no room reflection",
                                 f"reverb_tail_ms={tail}"))
    else:
        S["perspective"].append(("中距离拾音" if lang == "zh" else "mid-distance recording", f"reverb_tail_ms={tail}"))

    if spec.get("usage"):
        S["use_case"].append((str(spec["usage"]), "user:usage"))
    else:
        S["use_case"].append(("【待填：用在哪】" if lang == "zh" else "[FILL: where it is used]", "缺 UserKnowledge"))
        open_q.append("音效的用途（游戏提示音？视频转场？）没给，模型不知道该做多长多重")
    return S, open_q


def build_ambience(f: dict, t: dict, lex: dict, spec: dict, lang: str) -> tuple[dict, list[str]]:
    open_q: list[str] = []
    S: dict[str, list[tuple[str, str]]] = {k: [] for k in lex["slot_order"]["ambience"]}
    sp_txt, _ = _desc(lex, "space", t["space"], lang)
    S["space"].append((sp_txt, f"reverb_tail_ms={f['reverb_tail_ms']} → {t['space']}"))
    if spec.get("space"):
        S["space"].append((str(spec["space"]), "user:space"))
    else:
        S["space"].append(("【待填：什么空间】" if lang == "zh" else "[FILL: which space]", "缺 UserKnowledge"))
        open_q.append("环境声的空间主体（咖啡馆 / 雨夜街道 / 机房）必须用户指定")
    mats = rank(lex["material_bank"], f, t, threshold=0.4)
    if mats:
        names = [x["rule"]["en"] if lang == "en" else x["rule"]["zh"] for x in mats[:2]]
        S["sources"].append(("可能声源：" + "、".join(names) if lang == "zh" else "possible sources: " + ", ".join(names),
                             "推断：" + "; ".join(f"{x['rule']['id']}({x['score']:.2f})" for x in mats[:2])))
    if spec.get("instruments_seen"):
        S["sources"].append(("、".join(str(x) for x in spec["instruments_seen"]), "user:instruments_seen"))
    br_txt, _ = _desc(lex, "brightness", t["brightness"], lang)
    tx_txt, _ = _desc(lex, "texture", t["texture"], lang)
    S["texture"].append((f"{br_txt}；{tx_txt}", f"centroid_hz={f['centroid_hz']} flatness={f['flatness']}"))
    S["distance"].append((("远景" if t["space"] in ("hall", "cavern") else "中近景") if lang == "zh"
                          else ("distant" if t["space"] in ("hall", "cavern") else "mid to close"),
                          f"reverb_tail_ms={f['reverb_tail_ms']}"))
    if spec.get("time_weather"):
        S["time_weather"].append((str(spec["time_weather"]), "user:time_weather"))
    dens_map = {"sparse": ("安静少变化" if lang == "zh" else "calm, few events"),
                "medium": ("偶有事件穿插" if lang == "zh" else "occasional events"),
                "dense": ("持续有事件流动" if lang == "zh" else "continuous activity")}
    S["motion"].append((dens_map[t["density"]], f"onset_rate={f['onset_rate']}/s"))
    loop = ("首尾能量接近，可直接循环" if f.get("loop_friendly") else "首尾不齐，需淡入淡出")
    S["loop"].append((loop if lang == "zh" else ("seamless loop-able" if f.get("loop_friendly") else "needs fade in/out"),
                      f"loop_friendly={f.get('loop_friendly')}"))
    S["mix"].append((_desc(lex, "width", t["width"], lang)[0], f"stereo_corr={f['stereo_corr']}"))
    if spec.get("usage"):
        S["use_case"] = [(str(spec["usage"]), "user:usage")]
    return S, open_q


def build_voice(f: dict, t: dict, lex: dict, spec: dict, lang: str) -> tuple[dict, list[str]]:
    open_q: list[str] = []
    S: dict[str, list[tuple[str, str]]] = {k: [] for k in lex["slot_order"]["voice"]}
    f0 = f.get("f0_hz", 0)
    if f0:
        reg = ("低音区（男声 / 女中音下区）" if f0 < 140 else "中音区" if f0 < 220 else "偏高音区") if lang == "zh" else (
            "low register" if f0 < 140 else "mid register" if f0 < 220 else "high register")
        S["register"].append((f"{reg}，f0≈{f0:.0f} Hz", f"f0_hz={f0}"))
    else:
        open_q.append("没测到稳定音高：人声混合里有很强的器乐成分，或素材本身就是气声 / 念白")
    jitter = f.get("pitch_std_semitones", 0)
    if jitter > 2.5:
        S["delivery"].append(("音高起伏大，像说话 / 念白而非演唱" if lang == "zh"
                              else "speech-like delivery rather than sung lines",
                              f"pitch_std_semitones={jitter}"))
    elif jitter > 0:
        S["delivery"].append(("音高较稳定，接近演唱" if lang == "zh" else "fairly stable pitch, closer to singing",
                              f"pitch_std_semitones={jitter}"))
    vs = spec.get("vocal") or {}
    for key, slot, trace in (("language", "timbre", "user:vocal.language"),
                             ("gender", "timbre", "user:vocal.gender"),
                             ("emotion", "emotion", "user:vocal.emotion"),
                             ("pace", "pace", "user:vocal.pace")):
        if vs.get(key):
            S[slot].append((str(vs[key]), trace))
    if not vs.get("language"):
        S["timbre"].append(("【待填：语种 / 音色描述】" if lang == "zh" else "[FILL: language / timbre]", "缺 UserKnowledge"))
        open_q.append("人声的语种、年龄感、音色形容词必须用户指定")
    sp_txt, _ = _desc(lex, "space", t["space"], lang)
    S["recording"].append((sp_txt, f"reverb_tail_ms={f['reverb_tail_ms']}"))
    br_txt, _ = _desc(lex, "brightness", t["brightness"], lang)
    tx_txt, _ = _desc(lex, "texture", t["texture"], lang)
    S["recording"].append((f"{br_txt}；{tx_txt}", f"centroid_hz={f['centroid_hz']} flatness={f['flatness']}"))
    S["duration"].append((f"约 {f['duration_s']:.1f} 秒" if lang == "zh" else f"about {f['duration_s']:.1f} s",
                          f"duration_s={f['duration_s']}"))
    if spec.get("usage"):
        S["use_case"].append((str(spec["usage"]), "user:usage"))
    else:
        S["use_case"].append(("【待填：用途】" if lang == "zh" else "[FILL: use case]", "缺 UserKnowledge"))
    return S, open_q


BUILDERS = {"music": build_music, "sfx": build_sfx, "ambience": build_ambience, "voice": build_voice}


# ------------------------------------------------------------------ 组装与输出

def _join(slots: dict, order: list[str], lang: str) -> str:
    joiner = "，" if lang == "zh" else ", "
    parts: list[str] = []
    for name in order:
        texts = [txt for txt, _ in slots.get(name, [])]
        texts = [t for t in texts if t]
        if texts:
            parts.append(joiner.join(texts))
    return joiner.join(parts)


def _negatives(f: dict, t: dict, spec: dict, kind: str, lex: dict, lang: str) -> list[tuple[str, str]]:
    neg = lex["negatives"]
    out: list[tuple[str, str]] = []
    for w, tr in _take(neg["common"], lang, "common"):
        out.append((w, tr))
    if t["vocal_presence"] == "instrumental":
        out.extend(_take(neg["if_instrumental"], lang, "instrumental"))
    if t["brightness"] in ("very_dark", "dark"):
        out.extend(_take(neg["if_dark"], lang, "dark"))
    if t["space"] == "dry":
        out.extend(_take(neg["if_dry"], lang, "dry"))
    if t["density"] == "sparse":
        out.extend(_take(neg["if_sparse"], lang, "sparse"))
    if t["low_end"] in ("heavy", "overwhelming"):
        out.extend(_take(neg["if_heavy_low"], lang, "heavy low"))
    if t["texture"] == "tonal":
        out.extend(_take(neg["if_noisy"], lang, "tonal"))
    if t["dynamics"] == "compressed":
        out.extend(_take(neg["if_compressed"], lang, "compressed"))
    if kind == "sfx":
        out.extend(_take(neg["sfx_common"], lang, "sfx"))
        if t["space"] == "dry":
            out.extend(_take(neg["sfx_dry"], lang, "sfx dry"))
    for item in spec.get("negative", []) or []:
        out.append((str(item), "user:negative"))
    seen = set()
    uniq = []
    for w, tr in out:
        if w not in seen:
            seen.add(w)
            uniq.append((w, tr))
    # 八条封顶。负向提示不是写得越多越保险：堆到十几条时，模型会为了满足「不要 X」
    # 而牺牲真正重要的特征，而且你没法判断到底哪条在起作用。
    return uniq[:8]


def _take(node: dict, lang: str, trace: str) -> list[tuple[str, str]]:
    return [(w, trace) for w in node.get(lang, [])]


def _params(f: dict, t: dict, spec: dict) -> dict:
    return {
        "duration_s": round(f["duration_s"], 2),
        "suggested_render_seconds": spec.get("duration_target") or int(round(f["duration_s"])),
        "bpm": round(f["tempo_bpm"]) if f.get("tempo_bpm", 0) > 0 else None,
        "bpm_alt": round(f["tempo_alt_bpm"]) if f.get("tempo_alt_bpm", 0) > 0 else None,
        "key": f"{f['key']} {f['key_mode']}" if f.get("key_confidence", 0) >= 0.55 else None,
        "key_confidence": f.get("key_confidence"),
        "reference_rms_dbfs": f["rms_dbfs"],
        "reference_peak_dbfs": f["peak_dbfs"],
        "suggested_ceiling_dbfs": -1.0,
        "sample_rate_hz": 48000,
        "stereo": t["width"] != "mono",
    }


def _confidence(f: dict, gen: list[dict], kind: str) -> list[tuple[str, float, str]]:
    """每一维单独给分，不做加权总分 —— 总分会把「类型猜得很准」掩盖掉「配器全靠猜」。"""
    if f.get("tempo_bpm", 0) > 0:
        tempo_score = min(1.0, f.get("tempo_strength", 0) * 0.7 + f.get("beat_clarity", 0) / 12.0)
        tempo_note = f"strength={f.get('tempo_strength')} clarity={f.get('beat_clarity')}"
    else:
        tempo_score = 0.0 if kind == "music" else 0.3
        tempo_note = "测不到明确节拍" if kind == "music" else "非音乐类型，不需要节拍"
    return [
        ("速度", tempo_score, tempo_note),
        ("调性", f.get("key_confidence", 0), f"key_confidence={f.get('key_confidence')}"),
        ("类型", gen[0]["score"] if gen else 0.0, gen[0]["rule"]["id"] if gen else "无候选达标"),
        ("配器", 0.35, "机器只能给候选乐器，未经听感确认"),
    ]


def build(rep: dict, spec: dict, lex: dict, kind: str | None, lang: str) -> dict:
    f = rep["features"]
    t = rep["traits"]
    kind = kind or rep.get("kind", {}).get("kind") or "music"
    if kind not in BUILDERS:
        raise BuildError(f"未知类型 {kind}，只能是 {list(BUILDERS)}")

    zh_slots, zh_q = BUILDERS[kind](f, t, lex, spec, "zh")
    en_slots, en_q = BUILDERS[kind](f, t, lex, spec, "en")
    order = lex["slot_order"][kind]
    gen = rank(lex["genre_bank"], f, t) if kind == "music" else []

    res = {
        "kind": kind,
        "slots_zh": {k: v for k, v in zh_slots.items()},
        "slots_en": {k: v for k, v in en_slots.items()},
        "order": order,
        "prompt_zh": _join(zh_slots, order, "zh"),
        "prompt_en": _join(en_slots, order, "en"),
        "negatives_zh": _negatives(f, t, spec, kind, lex, "zh"),
        "negatives_en": _negatives(f, t, spec, kind, lex, "en"),
        "params": _params(f, t, spec),
        "Confidence": _confidence(f, gen, kind),
        "open_questions": sorted(set(zh_q + en_q)),
        "features": f,
        "traits": t,
        "warnings": rep.get("warnings", []),
        "genre_candidates": [{"id": g["rule"]["id"], "score": g["score"], "hit": g["hit"], "miss": g["miss"]}
                             for g in gen[:3]],
    }
    short_zh = "，".join(x for x in [
        (gen[0]["rule"]["zh"] if gen else ""),
        zh_slots.get("rhythm_key", [("", "")])[0][0] if zh_slots.get("rhythm_key") else "",
        spec.get("usage", ""),
    ] if x)
    res["short_zh"] = short_zh[:200]
    short_en = ", ".join(x for x in [
        (gen[0]["rule"]["en"] if gen else ""),
        en_slots.get("rhythm_key", [("", "")])[0][0] if en_slots.get("rhythm_key") else "",
        spec.get("usage_en", spec.get("usage", "")) if spec else "",
    ] if x)
    res["short_en"] = short_en[:200]
    return res


# ------------------------------------------------------------------ 渲染

def _render_markdown(res: dict, lex: dict, lang: str) -> str:
    order = res["order"]
    cap = lex["slot_captions"]
    lines: list[str] = []
    lines.append(f"# 提示词（{res['kind']}）")
    lines.append("")
    lines.append("> 每个短语后面都跟着「依据」，没有依据的不会出现在下面。带【待填】的位置是机器听不出来的，必须你补。")
    lines.append("")

    lines.append("## 追溯表")
    lines.append("")
    lines.append("| 槽位 | 措辞（中） | 措辞（EN） | 依据 |")
    lines.append("|---|---|---|---|")
    for name in order:
        zh = "；".join(txt for txt, _ in res["slots_zh"].get(name, [])) or "—"
        en = "; ".join(txt for txt, _ in res["slots_en"].get(name, [])) or "—"
        traces = "<br>".join(sorted({tr for _, tr in res["slots_zh"].get(name, []) +
                                     res["slots_en"].get(name, [])})) or "—"
        lines.append(f"| {cap.get(name, {}).get('zh', name)} | {zh} | {en} | {traces} |")
    lines.append("")

    lines.append("## 长提示词")
    lines.append("")
    lines.append("**中文**")
    lines.append("")
    lines.append("```")
    lines.append(res["prompt_zh"])
    lines.append("```")
    lines.append("")
    lines.append("**English**")
    lines.append("")
    lines.append("```")
    lines.append(res["prompt_en"])
    lines.append("```")
    lines.append("")

    lines.append("## 短提示词（喂给 style / tags 字段）")
    lines.append("")
    lines.append("```")
    lines.append(res["short_zh"])
    lines.append(res["short_en"])
    lines.append("```")
    lines.append("")

    lines.append("## 负向提示")
    lines.append("")
    lines.append("```")
    lines.append("；".join(w for w, _ in res["negatives_zh"]))
    lines.append("; ".join(w for w, _ in res["negatives_en"]))
    lines.append("```")
    lines.append("")

    lines.append("## 参数")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(res["params"], ensure_ascii=False, indent=2))
    lines.append("```")
    lines.append("")

    lines.append("## 把握度")
    lines.append("")
    lines.append("| 维度 | 分数 | 说明 |")
    lines.append("|---|---|---|")
    for name, score, note in res["Confidence"]:
        lines.append(f"| {name} | {score:.2f} | {note} |")
    lines.append("")

    if res["open_questions"]:
        lines.append("## 只能你答的问题")
        lines.append("")
        for q in res["open_questions"]:
            lines.append(f"- {q}")
        lines.append("")

    if res["warnings"]:
        lines.append("## 素材本身的坑")
        lines.append("")
        for w in res["warnings"]:
            lines.append(f"- {w}")
        lines.append("")

    lines.append("## 复述验证")
    lines.append("")
    lines.append("生成完以后，把新音频重新过一遍分析，再跟原始特征比对：")
    lines.append("")
    lines.append("```bash")
    lines.append("python analyze_audio.py new.wav --out out/new")
    lines.append("python compare_features.py out/track/features.json out/new/features.json")
    lines.append("```")
    lines.append("")
    lines.append("分数 ≥ 80 才算「提示词真的复述出来了这段音频」。")
    return "\n".join(lines)


def _target_files(res: dict, lex: dict, outdir: Path) -> None:
    tdir = outdir / "targets"
    tdir.mkdir(parents=True, exist_ok=True)
    for name, cfg in lex["targets"].items():
        limit = cfg.get("limit_chars", 800)
        if name == "elevenlabs-sfx":
            payload = {
                "text": res["prompt_en"][:limit],
                "duration_seconds": max(0.5, min(20.0, res["params"]["duration_s"])),
                "prompt_influence": 0.3,
                "note": cfg["zh"]["style_hint"],
            }
            (tdir / f"{name}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            continue
        body = res["prompt_en"][:limit] if name != "stable-audio" else res["prompt_en"][:limit]
        lines = [f"# {name}", f"# {cfg['zh']['style_hint']}", "", body, "", "## negative",
                 "; ".join(w for w, _ in res["negatives_en"]), "",
                 f"## params: bpm={res['params']['bpm']} duration={res['params']['suggested_render_seconds']}s"
                 f" key={res['params']['key']}"]
        (tdir / f"{name}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ------------------------------------------------------------------ CLI

def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    ap = argparse.ArgumentParser(
        description="把 features.json 翻译成可追溯的音频生成提示词",
        epilog="示例：\n"
               "  python build_prompt.py out/track/features.json --outdir out/track\n"
               "  python build_prompt.py features.json --spec specs/demo.json --kind sfx\n",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("features", help="analyze_audio.py 产出的 features.json")
    ap.add_argument("--outdir", help="输出目录；不指定就只打到 stdout")
    ap.add_argument("--spec", help="用户补充信息的 JSON（用途 / 乐器 / 人声 / 负向）")
    ap.add_argument("--kind", choices=sorted(BUILDERS), help="覆盖自动判定的类型")
    ap.add_argument("--lexicon", default=str(LEX_DEFAULT), help="词表路径")
    ap.add_argument("--print", dest="do_print", action="store_true", help="把 markdown 打到 stdout")
    args = ap.parse_args(argv)

    fp = Path(args.features)
    if not fp.is_file():
        print(f"错误：找不到 {fp}", file=sys.stderr)
        return 2
    try:
        rep = json.loads(fp.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"错误：{fp} 不是合法 JSON（{exc}）", file=sys.stderr)
        return 2
    if "features" not in rep:
        print("错误：这不是 analyze_audio.py 的 features.json（缺 features 字段）", file=sys.stderr)
        return 2

    lex_path = Path(args.lexicon)
    if not lex_path.is_file():
        print(f"错误：找不到词表 {lex_path}", file=sys.stderr)
        return 2
    lex = json.loads(lex_path.read_text(encoding="utf-8"))

    spec = {}
    if args.spec:
        sp = Path(args.spec)
        if not sp.is_file():
            print(f"错误：找不到 spec {sp}", file=sys.stderr)
            return 2
        spec = json.loads(sp.read_text(encoding="utf-8"))

    if "traits" not in rep:
        rep["traits"] = A.derive_traits(rep["features"])
    if "kind" not in rep:
        rep["kind"] = A.guess_kind(rep["features"], rep["traits"])

    try:
        res = build(rep, spec, lex, args.kind, "both")
    except BuildError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    md = _render_markdown(res, lex, "both")
    if args.do_print or not args.outdir:
        print(md)
    if args.outdir:
        od = Path(args.outdir)
        od.mkdir(parents=True, exist_ok=True)
        (od / "prompt.md").write_text(md + "\n", encoding="utf-8")
        (od / "prompt.json").write_text(json.dumps(res, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (od / "negative.txt").write_text(
            "；".join(w for w, _ in res["negatives_zh"]) + "\n" +
            "; ".join(w for w, _ in res["negatives_en"]) + "\n", encoding="utf-8")
        _target_files(res, lex, od)
        print(f"已写入 {od}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
