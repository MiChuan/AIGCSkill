#!/usr/bin/env python3
"""自测：不用任何外部素材，现场合成探针音频，验证「测量 → 标签 → 提示词」整条链。

为甚么要自测
------------
这类脚本的错法很隐蔽：质心算错 10 倍、速度差一个八度、立体声相关永远是 1.0 ——
结果照样能出 JSON，照样能拼出通顺的提示词，只是全错。所以每个测量量都必须有一个
已知答案的输入去怼它：1 kHz 正弦的质心就该是 1000 Hz 上下，120 BPM 的点击声
就只能报 120±4。这类断言有一半是「反向断言」——噪音的平坦度必须**不**低。

跑法
----
    python selftest.py                 # 原地合成，不落盘
    python selftest.py --emit out/probes   # 顺手把探针 wav 留下来，可听、可复用
    python selftest.py -v              # 打印每条断言

退出码：全部通过 0，有失败 1。
"""

from __future__ import annotations

import argparse
import json
import math
import random
import struct
import sys
import tempfile
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import analyze_audio as A  # noqa: E402
import build_prompt as B  # noqa: E402
import compare_features as C  # noqa: E402

SR = 16000
FAILS: list[str] = []
CHECKS = [0]


# ------------------------------------------------------------------ 断言

def check(cond: bool, msg: str, detail: str = "") -> None:
    CHECKS[0] += 1
    if not cond:
        FAILS.append(f"{msg}  {detail}".strip())
        print(f"  ✗ {msg}  {detail}")
    elif VERBOSE:
        print(f"  ✓ {msg}")


def near(val: float, target: float, tol: float, msg: str) -> None:
    check(abs(val - target) <= tol, msg, f"实测 {val:.3g}，期望 {target:.3g}±{tol:.3g}")


# ------------------------------------------------------------------ 探针合成

def _write_wav(path: Path, samples: list[float], sr: int = SR) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        frames = bytearray()
        for v in samples:
            frames += struct.pack("<h", max(-32768, min(32767, int(v * 32767))))
        w.writeframes(bytes(frames))


def _kick(amp: float = 0.9, dur: float = 0.28, f0: float = 95.0, f1: float = 45.0) -> list[float]:
    n = int(dur * SR)
    out = []
    phase = 0.0
    for i in range(n):
        t = i / n
        f = f0 + (f1 - f0) * t
        phase += 2 * math.pi * f / SR
        env = math.exp(-5.5 * t * dur / 0.28)
        out.append(amp * env * math.sin(phase))
    return out


def _hat(amp: float = 0.16, dur: float = 0.05, rng: random.Random | None = None) -> list[float]:
    rng = rng or random.Random(3)
    n = int(dur * SR)
    return [amp * math.exp(-60.0 * i / SR) * rng.uniform(-1, 1) for i in range(n)]


def probe_kick_120(dur: float = 4.0) -> list[float]:
    """120 BPM：每 0.5 s 一记底鼓。唯一正确答案 120。"""
    n = int(dur * SR)
    out = [0.0] * n
    k = _kick()
    i = 0
    while i < n:
        for j, v in enumerate(k):
            if i + j < n:
                out[i + j] += v
        i += int(0.5 * SR)
    return out


def probe_kick_80_with_hat(dur: float = 5.0) -> list[float]:
    """80 BPM 底鼓 + 反拍踩镲。正确答案 80。"""
    n = int(dur * SR)
    out = [0.0] * n
    k = _kick()
    h = _hat()
    beat = 60.0 / 80.0
    t = 0.0
    while t < dur:
        i = int(t * SR)
        for j, v in enumerate(k):
            if i + j < n:
                out[i + j] += v
        j0 = int((t + beat / 2) * SR)
        for j, v in enumerate(h):
            if j0 + j < n:
                out[j0 + j] += v
        t += beat
    return out


def probe_sine(freq: float = 1000.0, dur: float = 3.0, amp: float = 0.7) -> list[float]:
    """1 kHz 正弦：质心应当≈1000 Hz，平坦度必须很低。"""
    n = int(dur * SR)
    return [amp * math.sin(2 * math.pi * freq * i / SR) for i in range(n)]


def probe_noise(dur: float = 3.0, amp: float = 0.35) -> list[float]:
    """白噪：平坦度必须高，质心必须远高于正弦。"""
    rng = random.Random(7)
    n = int(dur * SR)
    return [amp * rng.uniform(-1, 1) for _ in range(n)]


def probe_sweep(dur: float = 4.0, f0: float = 200.0, f1: float = 4000.0) -> list[float]:
    """指数扫频：质心对时间的斜率必须为正。"""
    n = int(dur * SR)
    out = []
    phase = 0.0
    for i in range(n):
        t = i / n
        f = f0 * (f1 / f0) ** t
        phase += 2 * math.pi * f / SR
        out.append(0.5 * math.sin(phase))
    return out


def probe_bass_only(dur: float = 3.0) -> list[float]:
    """55 Hz 正弦：低频占比必须压过中高频。"""
    n = int(dur * SR)
    return [0.8 * math.sin(2 * math.pi * 55 * i / SR) for i in range(n)]


def probe_silence(dur: float = 1.0) -> list[float]:
    return [0.0] * int(dur * SR)


PROBES = {
    "kick-120": probe_kick_120,
    "kick-80-hat": probe_kick_80_with_hat,
    "sine-1k": probe_sine,
    "noise": probe_noise,
    "sweep": probe_sweep,
    "bass-55": probe_bass_only,
    "silence": probe_silence,
}


def make_all(outdir: Path | None = None) -> dict[str, Path]:
    """合成全部探针；给了 outdir 就落盘，返回 {名字: 路径}。"""
    d = outdir or Path(tempfile.mkdtemp(prefix="apr-probe-"))
    paths = {}
    for name, fn in PROBES.items():
        p = d / f"{name}.wav"
        _write_wav(p, fn())
        paths[name] = p
    return paths


# ------------------------------------------------------------------ 用例

def test_decoder(p: dict[str, Path]) -> None:
    print("\n[解码与截断]")
    rep = A.analyze(p["sine-1k"])
    check(rep["meta"]["decoder"] == "wave", "wav 走原生解码器", rep["meta"]["decoder"])
    near(rep["features"]["duration_s"], 3.0, 0.02, "时长读数")
    check(rep["meta"]["channels"] == 1, "单声道被识别为 1 声道", str(rep["meta"]["channels"]))

    rep2 = A.analyze(p["kick-120"], max_seconds=2.0, window="start")
    near(rep2["features"]["duration_s"], 2.0, 0.02, "max-seconds 截断生效")
    check(rep2["meta"]["truncated"] is True, "截断标记为真")

    rep3 = A.analyze(p["kick-120"], max_seconds=2.0, window="center")
    check(rep3["meta"]["window"] == "center", "window 参数被记录")

    try:
        A.analyze(p["silence"])
        check(False, "静音必须抛 SilentAudio")
    except A.SilentAudio:
        check(True, "静音必须抛 SilentAudio")

    try:
        A.analyze(p["sine-1k"].with_suffix(".mp3"))
        check(False, "缺失文件必须报错")
    except A.AudioError:
        check(True, "缺失文件必须报错")


def test_tempo(p: dict[str, Path]) -> None:
    print("\n[速度估计]")
    f = A.analyze(p["kick-120"])["features"]
    near(f["tempo_bpm"], 120.0, 4.0, "120 BPM 底鼓测速")
    check(f["beat_clarity"] > 1.5, "120 BPM 节拍清晰度应偏高", f"{f['beat_clarity']:.2f}")
    check(0.8 <= f["onset_rate"] <= 3.2, "起音密度在 1–3 /s", f"{f['onset_rate']:.2f}")

    f2 = A.analyze(p["kick-80-hat"])["features"]
    near(f2["tempo_bpm"], 80.0, 4.0, "80 BPM 测速（带反拍镲）")

    f3 = A.analyze(p["noise"])["features"]
    check(f3["tempo_bpm"] == 0, "白噪不能报出速度", f"{f3['tempo_bpm']:.1f} BPM")

    f4 = A.analyze(p["sweep"])["features"]
    check(f4["tempo_bpm"] == 0, "连续扫频不能报出速度", f"{f4['tempo_bpm']:.1f} BPM")

    f5 = A.analyze(p["bass-55"])["features"]
    check(f5["tempo_bpm"] == 0, "持续低频正弦不能报出速度", f"{f5['tempo_bpm']:.1f} BPM")

    check(f["voiced_ratio"] < 0.05, "底鼓循环不能被当成人声", f"{f['voiced_ratio']:.2f}")
    check(f5["voiced_ratio"] < 0.05, "单乐器持续音不能被当成人声", f"{f5['voiced_ratio']:.2f}")

    t = A.derive_traits(f)
    check(t["tempo_band"] == "up", "120 BPM 落在 up 档", t["tempo_band"])
    check(A.derive_traits(f2)["tempo_band"] == "slow", "80 BPM 落在 slow 档")


def test_spectrum(p: dict[str, Path]) -> None:
    print("\n[频谱测量]")
    s = A.analyze(p["sine-1k"])["features"]
    near(s["centroid_hz"], 1000.0, 120.0, "1 kHz 正弦的质心")
    check(s["flatness"] < 0.15, "正弦平坦度必须低", f"{s['flatness']:.3f}")
    check(s["rolloff85_hz"] < 1400, "正弦 85% 滚降应在基频附近", f"{s['rolloff85_hz']:.0f}")

    n = A.analyze(p["noise"])["features"]
    check(n["flatness"] > 0.35, "白噪平坦度必须高", f"{n['flatness']:.3f}")
    check(n["centroid_hz"] > 2500, "白噪质心应远高于 1 kHz", f"{n['centroid_hz']:.0f}")
    check(n["zcr"] > s["zcr"], "白噪过零率应高于正弦")

    sw = A.analyze(p["sweep"])["features"]
    check(sw["centroid_slope_hz_s"] > 300, "扫频质心斜率必须为正", f"{sw['centroid_slope_hz_s']:.0f}")
    check(sw["centroid_std_hz"] > 400, "扫频质心方差应大", f"{sw['centroid_std_hz']:.0f}")

    b = A.analyze(p["bass-55"])["features"]
    low = b["band_ratios"]["sub"] + b["band_ratios"]["bass"]
    check(low > 0.8, "55 Hz 正弦低频占比应 > 0.8", f"{low:.2f}")
    check(b["band_ratios"]["air"] < 0.01, "55 Hz 正弦几乎没有空气感")

    t = A.derive_traits(s)
    check(t["brightness"] in ("dark", "neutral"), "正弦的亮度标签", t["brightness"])
    check(A.derive_traits(n)["texture"] in ("noisy", "mixed"), "白噪的纹理标签")
    check(A.derive_traits(b)["low_end"] in ("heavy", "overwhelming"), "低频正弦的低频标签")


def test_loudness(p: dict[str, Path]) -> None:
    print("\n[电平与动态]")
    s = A.analyze(p["sine-1k"])["features"]
    near(s["peak_dbfs"], 20 * math.log10(0.7), 1.5, "0.7 幅度正弦的峰值 dBFS")
    check(s["crest_db"] < 6, "稳态正弦的波峰因数应小", f"{s['crest_db']:.1f}")
    check(s["lra_db"] < 3, "稳态正弦动态范围应小", f"{s['lra_db']:.1f}")

    k = A.analyze(p["kick-120"])["features"]
    check(k["crest_db"] > s["crest_db"], "瞬态底鼓的波峰因数应大于稳态正弦")
    check(k["lra_db"] > 6, "底鼓循环动态范围应明显大于 6 dB", f"{k['lra_db']:.1f}")

    t = A.derive_traits(s)
    check(t["dynamics"] in ("compressed", "controlled"), "稳态正弦的动态标签", t["dynamics"])
    check(t["energy"] in ("quiet", "moderate", "loud", "hot"), "能量标签在合法集合内", t["energy"])


def test_extra(p: dict[str, Path]) -> None:
    print("\n[调性 / 空间 / 立体声 / 类型]")
    s = A.analyze(p["sine-1k"])["features"]
    check(s["key"] in A.PC_NAMES, "调性主音在 12 音名内", s["key"])
    check(s["key_mode"] in ("major", "minor"), "调式只可能是 major / minor")
    check(0.0 <= s["key_confidence"] <= 1.0, "调性置信度在 0–1")
    check(len(s["chroma"]) == 12 and abs(sum(s["chroma"]) - 1.0) < 0.02, "色度归一化")

    check(s["stereo_corr"] > 0.999, "单声道源的左右相关应为 1", f"{s['stereo_corr']:.3f}")
    check(A.derive_traits(s)["width"] == "mono", "单声道宽度标签")

    k = A.analyze(p["kick-120"])["features"]
    check(k["reverb_tail_ms"] < 800, "干声底鼓不应报出长尾", f"{k['reverb_tail_ms']}")
    check(s["fade_in_s"] < 0.2, "稳态正弦无明显淡入", f"{s['fade_in_s']}")

    kind_sfx = A.guess_kind({"duration_s": 1.2, "crest_db": 18.0, "beat_clarity": 0.4,
                             "voiced_ratio": 0.0, "onset_rate": 6.0}, {})
    check(kind_sfx["kind"] == "sfx", "1.2s 高波峰因数应判为音效")

    kind_mus = A.guess_kind({"duration_s": 30.0, "crest_db": 12.0, "beat_clarity": 4.0,
                             "voiced_ratio": 0.1, "onset_rate": 3.0}, {})
    check(kind_mus["kind"] == "music", "30s 强节拍应判为音乐")

    kind_amb = A.guess_kind({"duration_s": 30.0, "crest_db": 6.0, "beat_clarity": 0.3,
                             "voiced_ratio": 0.0, "onset_rate": 0.2}, {})
    check(kind_amb["kind"] == "ambience", "无节拍长音频应判为环境声")

    kind_voc = A.guess_kind({"duration_s": 20.0, "crest_db": 10.0, "beat_clarity": 0.5,
                             "voiced_ratio": 0.7, "onset_rate": 2.0}, {})
    check(kind_voc["kind"] == "voice", "高有声帧占比应判为人声")


def test_traits_schema() -> None:
    print("\n[标签字典完整性]")
    fake = {
        "tempo_bpm": 100.0, "centroid_hz": 1500.0, "rms_dbfs": -20.0, "flatness": 0.2,
        "onset_rate": 3.0, "lra_db": 9.0, "reverb_tail_ms": 150, "stereo_corr": 0.8,
        "side_ratio": 0.02, "band_ratios": {"sub": 0.1, "bass": 0.2}, "voiced_ratio": 0.1,
    }
    t = A.derive_traits(fake)
    allowed = {
        "tempo_band": ("very_slow", "slow", "midtempo", "up", "fast", "frantic", "free"),
        "brightness": ("very_dark", "dark", "neutral", "bright", "glass"),
        "energy": ("quiet", "moderate", "loud", "hot"),
        "texture": ("tonal", "mixed", "noisy"),
        "density": ("sparse", "medium", "dense"),
        "dynamics": ("compressed", "controlled", "wide"),
        "space": ("dry", "room", "hall", "cavern"),
        "width": ("mono", "narrow", "wide", "very_wide"),
        "low_end": ("light", "balanced", "heavy", "overwhelming"),
        "vocal_presence": ("instrumental", "some", "prominent"),
    }
    check(set(t) == set(allowed), "标签键集合固定", str(sorted(set(allowed) ^ set(t))))
    for k, vals in allowed.items():
        check(t[k] in vals, f"{k} 取值合法", f"{t[k]} ∉ {vals}")


def test_builder(p: dict[str, Path]) -> None:
    print("\n[提示词组装]")
    lex = json.loads((HERE.parent / "data" / "lexicon.json").read_text(encoding="utf-8"))
    rep = A.analyze(p["kick-120"])
    rep["kind"] = A.guess_kind(rep["features"], rep["traits"])

    res = B.build(rep, {}, lex, None, "both")
    check(res["kind"] == "music", "底鼓循环被判为音乐", res["kind"])
    check(len(res["prompt_zh"]) > 40 and len(res["prompt_en"]) > 40, "长提示词非空")

    # 追溯：每一个短语都必须带得出依据
    untraced = []
    for name, items in res["slots_zh"].items():
        for txt, tr in items:
            if not txt or not tr:
                untraced.append(name)
    check(not untraced, "没有无依据的短语", ",".join(untraced))

    order = lex["slot_order"]["music"]
    check(res["order"] == order, "槽位顺序取自词表")
    check(all(k in res["slots_zh"] for k in order), "八个槽位都在")

    # 有人暗 EDIT：带节拍的东西不能再判成「无节拍」
    joined = res["prompt_zh"] + res["prompt_en"]
    check("无节拍" not in res["prompt_zh"] and "beatless" not in res["prompt_en"],
          "有 120 BPM 时不许出现「无节拍」", joined[:60])

    # 用户说的东西优先级最高
    res2 = B.build(rep, {"instruments_seen": ["马林巴"], "usage": "广告片头"},
                   lex, None, "both")
    check("马林巴" in res2["prompt_zh"], "用户指定的乐器必须进 prompt")
    check("广告片头" in res2["prompt_zh"], "用户指定的用途必须进 prompt")
    check(res2["prompt_zh"].index("马林巴") < res2["prompt_zh"].index("底鼓"),
          "用户指定的乐器排在机器推断前面")

    # 人声：analysis 判不出来的时候不许替用户编
    check("纯器乐" in res["prompt_zh"], "无有声段时判为纯器乐")
    res3 = B.build(rep, {"vocal": {"present": True}}, lex, None, "both")
    check("人声" in res3["prompt_zh"], "用户声明有人声时必须体现")
    check(any("未知" in q for q in res3["open_questions"]),
          "有人声但没给语种时必须列为待答问题", str(res3["open_questions"]))

    # 负向提示不能无限膨胀
    for lang in ("negatives_zh", "negatives_en"):
        check(0 < len(res[lang]) <= 8, f"{lang} 数量在 1–8 之间", str(len(res[lang])))

    # 参数：BPM / 备选 BPM / 时长都要带上
    check(res["params"]["bpm"] == 120, "BPM 整数进参数", str(res["params"]["bpm"]))
    check(res["params"]["bpm_alt"] == 60, "备选 BPM 也给出", str(res["params"]["bpm_alt"]))
    check(res["params"]["duration_s"] == 4.0, "时长进参数", str(res["params"]["duration_s"]))

    check(len(res["Confidence"]) == 4, "把握度按四个维度给分")
    check(all(0 <= s <= 1 for _, s, _ in res["Confidence"]), "把握度在 0–1")


def test_builder_sfx(p: dict[str, Path]) -> None:
    print("\n[音效提示词]")
    lex = json.loads((HERE.parent / "data" / "lexicon.json").read_text(encoding="utf-8"))
    short = A.analyze(p["kick-120"], max_seconds=2.0, window="start")
    short["kind"] = A.guess_kind(short["features"], short["traits"])
    res = B.build(short, {}, lex, "sfx", "both")
    check(res["kind"] == "sfx", "强制 sfx 类型生效")
    check(any(q for q in res["open_questions"] if "声源" in q or "物体" in q or "动作" in q),
          "没给声源时必须列为待答问题", str(res["open_questions"]))
    check("约 2.00 秒" in res["prompt_zh"], "时长写入音效提示词")
    check(all(k in res["slots_zh"] for k in lex["slot_order"]["sfx"]), "音效槽位齐全")


def test_compare() -> None:
    print("\n[往返校验]")
    base = {"tempo_bpm": 120.0, "centroid_hz": 1500.0, "flatness": 0.2, "onset_rate": 3.0,
            "lra_db": 9.0, "crest_db": 14.0, "rms_dbfs": -16.0, "reverb_tail_ms": 200,
            "voiced_ratio": 0.0, "stereo_corr": 0.9, "duration_s": 30.0, "key": "A",
            "band_ratios": {"sub": 0.1, "bass": 0.2}}

    r = C.compare(base, dict(base))
    check(r["score"] == 100.0, "跟自己比必须满分", str(r["score"]))
    check(r["verdict"].startswith("PASS"), "满分判 PASS")

    off = dict(base, tempo_bpm=80.0)
    r2 = C.compare(base, off)
    check(r2["score"] < 60, "速度差 40 BPM 必须掉到 60 分以下", str(r2["score"]))
    check("MISMATCH" in r2["verdict"], "关键维度出错就判 MISMATCH", r2["verdict"])
    check(bool(r2["caps"]), "关键维度硬伤要写进 caps")

    off2 = dict(base, centroid_hz=4000.0)
    check(C.compare(base, off2)["score"] < 60, "质心差 2.7 倍必须掉到 60 以下")

    near = dict(base, centroid_hz=1560.0, tempo_bpm=124.0, onset_rate=3.4,
                reverb_tail_ms=230, duration_s=32.0, rms_dbfs=-15.0)
    r3 = C.compare(base, near)
    check(r3["score"] >= 80, "各维度小偏差仍应判 PASS", str(r3["score"]))

    both_silent = C.compare(dict(base, tempo_bpm=0), dict(base, tempo_bpm=0))
    check(both_silent["score"] >= 80, "两边都没节拍时不该被判为硬伤", str(both_silent["score"]))
    check(all(s >= 0 and s <= 1 for alarm in [row["score"] for row in r3["rows"]] for s in [alarm]),
          "得分落在 0–1")


# ------------------------------------------------------------------ 入口

VERBOSE = False


def main(argv: list[str] | None = None) -> int:
    global VERBOSE
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="无外部素材的自测：合成探针 → 测量 → 标签 → 提示词")
    ap.add_argument("--emit", help="把探针 wav 写到这个目录")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    VERBOSE = args.verbose

    paths = make_all(Path(args.emit) if args.emit else None)
    if args.emit:
        print(f"探针已写入 {args.emit}")

    test_decoder(paths)
    test_tempo(paths)
    test_spectrum(paths)
    test_loudness(paths)
    test_extra(paths)
    test_traits_schema()
    test_builder(paths)
    test_builder_sfx(paths)
    test_compare()

    print("\n" + "─" * 46)
    if FAILS:
        print(f"失败 {len(FAILS)} / {CHECKS[0]} 项")
        return 1
    print(f"全部通过：{CHECKS[0]} 项断言")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
