#!/usr/bin/env python3
"""音频逆向分析器 — 零第三方依赖，可独立跑，也可被 build_prompt.py / selftest.py import。

为甚么要这个脚本
----------------
「听一遍，写个提示词」之所以不可复现，是因为人耳给的是印象，不是数值。
同一个音频，两个人写出两个风格标签，谁都对，谁都没用。

本脚本只做一件事：把音频变成一组**可复核的数字**（速度、调性、明暗、密度、
动态、空间、低频占比……），并顺手把数字按固定阈值翻成标签。
标签只是「候选」，真正的判断在 build_prompt.py 的追溯表里 —— 每一句话都要能
指回这里的某个数值。

支持格式
--------
- `.wav`：原生解析（PCM 8/16/24/32 位，单/多声道），零依赖
- 其余（.mp3/.m4a/.flac/.ogg/.opus/.aac/.wma）：调用 ffmpeg 解码
  没有 ffmpeg 时**不会**猜，直接报错并给出三条可执行的转码路径

用法
----
    python analyze_audio.py track.mp3
    python analyze_audio.py track.wav --out out/track --max-seconds 90
    python analyze_audio.py track.wav --window start --summary
    python analyze_audio.py --help

退出码：0 = 正常出结果；2 = 输入/解码问题；3 = 信号不可分析（近乎静音）。
"""

from __future__ import annotations

import argparse
import array
import json
import math
import os
import shutil
import subprocess
import sys
import wave
from pathlib import Path

# ------------------------------------------------------------------ 常量

TARGET_SR = 16000          # 分析采样率：Nyquist 8 kHz，覆盖音乐主体与音效高频
FRAME = 2048               # 64 ms 窗
HOP = 512                  # 32 ms 步长 → 包络帧率 31.25 fps
EPS = 1e-12

BANDS = (
    ("sub", 20.0, 60.0),
    ("bass", 60.0, 250.0),
    ("lowmid", 250.0, 600.0),
    ("mid", 600.0, 2000.0),
    ("highmid", 2000.0, 6000.0),
    ("air", 6000.0, 8000.0),
)

PC_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")

# 判定「有没有拍」的两个门槛，必须同时满足。
# 少任何一个都会出问题：只看强度，一段持续低音正弦能以 0.95 的强度报出 75 BPM
# （窗—信号相位差周期抖动造成的假周期）；只看清晰度，白噪也能凑到 2.9。
T_MIN_STRENGTH = 0.45     # 归一化自相关峰值：有多大比例起音能量是周期性的
T_MIN_CLARITY = 3.2       # 峰值相关 ÷ 平均绝对相关：峰是否真的突出于背景

# Krumhansl-Kessler 调性轮廓（大调 / 小调），用于色度相关性判调
KK_MAJOR = (6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88)
KK_MINOR = (6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17)


class AudioError(RuntimeError):
    """解码 / 输入层面的错误，退出码 2。"""


class SilentAudio(AudioError):
    """信号无法分析（静音或近乎静音），退出码 3。"""


# ------------------------------------------------------------------ FFT

_TABLES: dict[int, tuple[list[float], list[float]]] = {}


def _tables(n: int) -> tuple[list[float], list[float]]:
    """n 点 FFT 的旋转因子（只算前 n/2 个，靠对称性复用）。"""
    t = _TABLES.get(n)
    if t is None:
        half = n // 2
        re = [math.cos(-2.0 * math.pi * k / n) for k in range(half)]
        im = [math.sin(-2.0 * math.pi * k / n) for k in range(half)]
        t = (re, im)
        _TABLES[n] = t
    return t


def fft_inplace(re: list[float], im: list[float]) -> None:
    """迭代式 radix-2 FFT，原地改 re / im。长度必须是 2 的幂。"""
    n = len(re)
    if n & (n - 1):
        raise ValueError("FFT 长度必须是 2 的幂")
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            re[i], re[j] = re[j], re[i]
            im[i], im[j] = im[j], im[i]

    tw_re, tw_im = _tables(n)
    length = 2
    while length <= n:
        half = length >> 1
        step = n // length
        for start in range(0, n, length):
            k = 0
            for p in range(start, start + half):
                q = p + half
                wr = tw_re[k]
                wi = tw_im[k]
                br = re[q]
                bi = im[q]
                tr = wr * br - wi * bi
                ti = wr * bi + wi * br
                re[q] = re[p] - tr
                im[q] = im[p] - ti
                re[p] += tr
                im[p] += ti
                k += step
        length <<= 1


def _hann(n: int) -> list[float]:
    return [0.5 - 0.5 * math.cos(2.0 * math.pi * i / (n - 1)) for i in range(n)]


# ------------------------------------------------------------------ 解码

def find_ffmpeg() -> str | None:
    """按 PATH → 常见安装位置找 ffmpeg。"""
    hit = shutil.which("ffmpeg")
    if hit:
        return hit
    cands = [
        r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe"),
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/usr/bin/ffmpeg",
    ]
    for c in cands:
        if c and Path(c).is_file():
            return c
    return None


def _read_wav(path: Path) -> tuple[array.array, array.array]:
    """读 PCM WAV，返回 (left, right) 的 float32 数组，取值 -1..1。"""
    try:
        w = wave.open(str(path), "rb")
    except wave.Error as exc:
        raise AudioError(
            f"不是可解析的 PCM WAV（{exc}）。若这是压缩 WAV / RF64，请装 ffmpeg 后重试。"
        ) from exc
    with w:
        ch = w.getnchannels()
        sw = w.getsampwidth()
        n = w.getnframes()
        raw = w.readframes(n)
    if sw == 1:
        vals = [(b - 128) / 128.0 for b in raw]
    elif sw == 2:
        a = array.array("h")
        a.frombytes(raw[: len(raw) - len(raw) % 2])
        if sys.byteorder == "big":
            a.byteswap()
        vals = [v / 32768.0 for v in a]
    elif sw == 3:
        vals = []
        for i in range(0, len(raw) - 2, 3):
            b0, b1, b2 = raw[i], raw[i + 1], raw[i + 2]
            v = b0 | (b1 << 8) | (b2 << 16)
            if v & 0x800000:
                v -= 1 << 24
            vals.append(v / 8388608.0)
    elif sw == 4:
        a = array.array("i")
        a.frombytes(raw[: len(raw) - len(raw) % 4])
        if sys.byteorder == "big":
            a.byteswap()
        vals = [v / 2147483648.0 for v in a]
    else:
        raise AudioError(f"不支持的采样位宽：{sw * 8} bit")

    if ch == 1:
        left = array.array("f", vals)
        right = array.array("f", vals)
    else:
        left = array.array("f", vals[0::ch])
        right = array.array("f", vals[1::ch])
    return left, right


def _read_via_ffmpeg(path: Path, ffmpeg: str, sr: int) -> tuple[array.array, array.array]:
    cmd = [
        ffmpeg, "-v", "error", "-nostdin", "-i", str(path),
        "-ac", "2", "-ar", str(sr), "-f", "s16le", "-acodec", "pcm_s16le", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AudioError(f"ffmpeg 调用失败：{exc}") from exc
    if proc.returncode != 0 or not proc.stdout:
        msg = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        tail = " / ".join(msg[-3:]) if msg else "无输出"
        raise AudioError(f"ffmpeg 解码失败（{path.name}）：{tail}")
    a = array.array("h")
    a.frombytes(proc.stdout[: len(proc.stdout) - len(proc.stdout) % 2])
    if sys.byteorder == "big":
        a.byteswap()
    left = array.array("f", (a[i] / 32768.0 for i in range(0, len(a), 2)))
    right = array.array("f", (a[i] / 32768.0 for i in range(1, len(a), 2)))
    return left, right


def _resample(x: array.array, src_sr: int, dst_sr: int) -> array.array:
    """线性插值重采样。分析用途，不做抗镜像滤波。"""
    if src_sr == dst_sr or not len(x):
        return x
    ratio = dst_sr / float(src_sr)
    n_out = int(len(x) * ratio)
    out = array.array("f", bytes(4 * n_out))
    last = len(x) - 1
    for i in range(n_out):
        pos = i / ratio
        i0 = int(pos)
        if i0 >= last:
            out[i] = x[last]
            continue
        frac = pos - i0
        out[i] = x[i0] * (1.0 - frac) + x[i0 + 1] * frac
    return out


def load_audio(path: str | Path, sr: int = TARGET_SR,
               max_seconds: float = 60.0, window: str = "center",
               ffmpeg: str | None = None) -> dict:
    """解码成 (left, right) @ sr，并按 max_seconds 截取。"""
    p = Path(path)
    if not p.is_file():
        raise AudioError(f"找不到文件：{p}")

    if p.suffix.lower() == ".wav":
        left, right = _read_wav(p)
        src_sr = 0
        with wave.open(str(p), "rb") as w:
            src_sr = w.getframerate()
        decoder = "wave"
        if src_sr != sr:
            left = _resample(left, src_sr, sr)
            right = _resample(right, src_sr, sr)
    else:
        exe = ffmpeg or find_ffmpeg()
        if not exe:
            raise AudioError(
                "这个格式需要 ffmpeg 解码，但没找到 ffmpeg。三条路任选一条：\n"
                "  1) Windows: winget install Gyan.FFmpeg   然后重开终端\n"
                "  2) 用任意工具把它转成 wav: ffmpeg -i in.mp3 -ac 2 -ar 16000 out.wav\n"
                "  3) 直接分析转好的 wav（本脚本对 wav 零依赖）"
            )
        left, right = _read_via_ffmpeg(p, exe, sr)
        src_sr = sr
        decoder = f"ffmpeg:{Path(exe).name}"

    total = len(left)
    if total == 0:
        raise SilentAudio("解码后长度为 0，文件可能是空的或全静音。")

    limit = int(max_seconds * sr)
    if total > limit > 0:
        if window == "start":
            start = 0
        else:
            start = max(0, (total - limit) // 2)
        left = left[start:start + limit]
        right = right[start:start + limit]

    # 直流去偏：先算均值再减，避免偏移把质心拉低
    n = len(left)
    mean = (sum(left) + sum(right)) / (2.0 * n)
    mono = array.array("f", ((left[i] + right[i]) * 0.5 - mean for i in range(n)))

    # 左右是否逐样本相同（单声道源，或解码器上混出来的假立体声）
    diff_max = 0.0
    for i in range(0, n, 8):
        d = abs(left[i] - right[i])
        if d > diff_max:
            diff_max = d
            if diff_max > 1e-6:
                break
    channels = 1 if diff_max <= 1e-6 else 2

    return {
        "audio": {"path": str(p), "decoder": decoder, "source_sr": src_sr,
                  "analysis_sr": sr, "channels": channels,
                  "seconds_analyzed": round(n / sr, 3),
                  "truncated": total > limit > 0, "window": window},
        "left": left, "right": right, "mono": mono,
    }


# ------------------------------------------------------------------ 单帧测量

def _frame_stats(x: list[float], sr: int, n: int) -> dict:
    """一帧的时域 + 频域测量。"""
    # --- 时域
    prev = x[0]
    zc = 0
    for v in x:
        if (v >= 0) != (prev >= 0):
            zc += 1
        prev = v
    rms = math.sqrt(sum(v * v for v in x) / n + EPS)
    peak = max(max(x), -min(x))

    # --- 频域
    re = [x[i] * _HANN[i] for i in range(n)]
    im = [0.0] * n
    fft_inplace(re, im)
    half = n // 2
    bin_hz = sr / float(n)
    mag = [0.0] * (half + 1)
    total_mag = 0.0
    for b in range(half + 1):
        m = math.sqrt(re[b] * re[b] + im[b] * im[b])
        mag[b] = m
        total_mag += m
    total_mag = max(total_mag, EPS)

    # 质心 / 滚降 / 平坦度
    cnum = 0.0
    for b in range(1, half + 1):
        cnum += b * bin_hz * mag[b]
    centroid = cnum / total_mag

    acc = 0.0
    rolloff = 0.0
    thresh = 0.85 * total_mag
    for b in range(1, half + 1):
        acc += mag[b]
        if acc >= thresh:
            rolloff = b * bin_hz
            break

    log_sum = 0.0
    lin_sum = 0.0
    band_e = {k: 0.0 for k, _, _ in BANDS}
    band_edges = [(k, int(lo / bin_hz), int(hi / bin_hz)) for k, lo, hi in BANDS]
    for k, lo_b, hi_b in band_edges:
        lo_b = max(1, lo_b)
        hi_b = min(half, hi_b)
        s = 0.0
        for b in range(lo_b, hi_b + 1):
            s += mag[b] * mag[b]
        band_e[k] = s
    for b in range(1, half + 1):
        v = mag[b]
        log_sum += math.log(v + 1e-9)
        lin_sum += v
    flatness = math.exp(log_sum / half) / (lin_sum / half + EPS)

    return {
        "rms": rms, "peak": peak, "zcr": zc / float(n),
        "centroid": centroid, "rolloff": rolloff, "flatness": flatness,
        "mag": mag, "band_e": band_e, "total_mag": total_mag,
    }


_HANN = _hann(FRAME)


def _movavg(x: list[float], w: int) -> list[float]:
    n = len(x)
    if n == 0:
        return []
    w = max(1, min(w, n))
    pre = [0.0] * (n + 1)
    for i, v in enumerate(x):
        pre[i + 1] = pre[i] + v
    out = [0.0] * n
    h = w // 2
    for i in range(n):
        a = max(0, i - h)
        b = min(n, i + h + 1)
        out[i] = (pre[b] - pre[a]) / (b - a)
    return out


def _pitch_of_frame(mag: list[float], bin_hz: float) -> tuple[float, float]:
    """谐波求和（HPS 思路）估 f0：返回 (f0_hz, 显著度 0..1)。"""
    half = len(mag) - 1
    b_lo = max(2, int(60.0 / bin_hz))
    b_hi = min(half, int(400.0 / bin_hz))
    total = sum(mag) + EPS
    best_score = 0.0
    best_f0 = 0.0
    for b in range(b_lo, b_hi + 1):
        s = 0.0
        for h in range(1, 9):
            idx = b * h
            if idx > half:
                break
            s += mag[idx] / h
        if s > best_score:
            best_score = s
            best_f0 = b * bin_hz
    return best_f0, min(1.0, best_score / total * 3.0)


def _centroid_slope(centroids: list[float], rms: list[float], sr: int) -> float:
    """质心随时间的线性斜率（Hz/s），用于判断扫频 / 渐亮。"""
    pts = [(i * HOP / sr, c) for i, c in enumerate(centroids) if rms[i] > 0.02]
    if len(pts) < 4:
        return 0.0
    mx = sum(p[0] for p in pts) / len(pts)
    my = sum(p[1] for p in pts) / len(pts)
    num = sum((p[0] - mx) * (p[1] - my) for p in pts)
    den = sum((p[0] - mx) ** 2 for p in pts)
    return num / den if den > EPS else 0.0


def _tempo_from_env(env: list[float], fps: float) -> tuple[float, float, float]:
    """对起音包络做自相关测速，返回 (BPM, 清晰度, 峰值相关)。

    返回两个数而不是一个数，是因为单个数救不了你：
    - **峰值相关**（0..1）：有多大比例的起音能量是周期性的。低于 0.25 基本就是没有拍。
    - **清晰度**：峰值相关 ÷ 平均绝对相关。同样峰值下， Emerging 噪声会让这个值虚高，
      所以它只在配合上一个数使用时才有意义。

    另外两个刻意的取舍：
    1. **不加谐波加权**。加了会偏向「更快的那个解」—— 反拍有镲时能被拉到倍速。
    2. **折进 70–170 BPM**。这是人实际数拍子的区间，超出就折八度，避免 80 报成 160。
    """
    n = len(env)
    if n < 16:
        return 0.0, 0.0, 0.0
    mean = sum(env) / n
    env0 = [v - mean for v in env]
    e0 = sum(v * v for v in env0) / n
    if e0 < EPS:
        return 0.0, 0.0, 0.0

    lag_lo = max(2, int(fps * 60.0 / 240.0))
    lag_hi = min(n // 2, int(fps * 60.0 / 40.0))
    if lag_hi <= lag_lo:
        return 0.0, 0.0, 0.0

    ac: dict[int, float] = {}
    for lag in range(lag_lo, lag_hi + 1):
        s = 0.0
        for i in range(n - lag):
            s += env0[i] * env0[i + lag]
        ac[lag] = s / (n - lag) / e0

    best_lag = max(ac, key=lambda k: ac[k])
    strength = max(0.0, ac[best_lag])
    if strength <= 0:
        return 0.0, 0.0, 0.0
    mad = sum(abs(v) for v in ac.values()) / len(ac)
    clarity = strength / mad if mad > EPS else 0.0
    return round(_bpm_of_lag(best_lag, ac, fps), 2), round(clarity, 3), round(strength, 3)


def _bpm_of_lag(lag: int, ac: dict[int, float], fps: float) -> float:
    lag = int(lag)
    a = ac.get(lag - 1, 0.0)
    b = ac.get(lag, 0.0)
    c = ac.get(lag + 1, 0.0)
    den = a - 2 * b + c
    frac = 0.5 * (a - c) / den if abs(den) > 1e-9 else 0.0
    frac = max(-0.5, min(0.5, frac))
    bpm = 60.0 * fps / (lag + frac)
    while bpm < 70.0:
        bpm *= 2
    while bpm > 170.0:
        bpm /= 2
    return bpm


def _onset_envelope(flux: list[float]) -> list[float]:
    """去趋势的正向谱通量：减去局部均值，只留「突然变响」的那部分。"""
    base = _movavg(flux, 17)
    return [max(0.0, flux[i] - base[i]) for i in range(len(flux))]


# ------------------------------------------------------------------ 主分析
    """质心随时间的线性斜率（Hz/s），用于判断扫频 / 渐亮。"""
    pts = [(i * HOP / sr, c) for i, c in enumerate(centroids) if rms[i] > 0.02]
    if len(pts) < 4:
        return 0.0
    mx = sum(p[0] for p in pts) / len(pts)
    my = sum(p[1] for p in pts) / len(pts)
    num = sum((p[0] - mx) * (p[1] - my) for p in pts)
    den = sum((p[0] - mx) ** 2 for p in pts)
    return num / den if den > EPS else 0.0


# ------------------------------------------------------------------ 主分析

def analyze(path: str | Path, sr: int = TARGET_SR, max_seconds: float = 60.0,
            window: str = "center", ffmpeg: str | None = None,
            progress: bool = False) -> dict:
    loaded = load_audio(path, sr=sr, max_seconds=max_seconds, window=window, ffmpeg=ffmpeg)
    mono: array.array = loaded["mono"]
    left: array.array = loaded["left"]
    right: array.array = loaded["right"]
    n = len(mono)
    dur = n / float(sr)

    n_frames = max(1, (n - FRAME) // HOP + 1)
    pad = FRAME * 2
    buf = list(mono) + [0.0] * pad
    half = FRAME // 2
    bin_hz = sr / float(FRAME)
    low_bins = max(4, int(250.0 / bin_hz))   # 低频起音包络：脚踩的那一路（底鼓 / 贝斯）

    rms_list: list[float] = []
    centroid_list: list[float] = []
    rolloff_list: list[float] = []
    flat_list: list[float] = []
    zcr_list: list[float] = []
    flux_list: list[float] = []
    low_flux_list: list[float] = []
    chroma = [0.0] * 12
    band_sum = {k: 0.0 for k, _, _ in BANDS}
    prev_mag: list[float] | None = None
    mag_sum_all = 0.0
    voiced = 0
    f0s: list[float] = []
    frame_cand: list[tuple[float, float]] = []

    # 色度用的 bin → 音级查表（55 Hz – 5 kHz）
    bin_hz = sr / float(FRAME)
    half = FRAME // 2
    pc_of_bin: list[tuple[int, int, float]] = []
    for b in range(2, half + 1):
        f = b * bin_hz
        if 55.0 <= f <= 5000.0:
            pc = int(round(12 * math.log2(f / 440.0) + 9)) % 12
            pc_of_bin.append((b, pc, 1.0))

    for fi in range(n_frames):
        x = buf[fi * HOP: fi * HOP + FRAME]
        st = _frame_stats(x, sr, FRAME)
        rms_list.append(st["rms"])
        centroid_list.append(st["centroid"])
        rolloff_list.append(st["rolloff"])
        flat_list.append(st["flatness"])
        zcr_list.append(st["zcr"])
        for k in band_sum:
            band_sum[k] += st["band_e"][k]
        mag = st["mag"]
        mag_sum_all += st["total_mag"]

        if prev_mag is not None:
            d = 0.0
            low_d = 0.0
            for b in range(1, low_bins + 1):
                diff = mag[b] - prev_mag[b]
                if diff > 0:
                    low_d += diff
            for b in range(low_bins + 1, half + 1):
                diff = mag[b] - prev_mag[b]
                if diff > 0:
                    d += diff
            flux_list.append((d + low_d) / (st["total_mag"] + EPS))
            low_flux_list.append(low_d / (st["total_mag"] + EPS))
        else:
            flux_list.append(0.0)
            low_flux_list.append(0.0)
        prev_mag = mag

        for b, pc, wgt in pc_of_bin:
            chroma[pc] += mag[b] * wgt

        cand = 0.0
        if st["rms"] > 1e-6:
            f0, sal = _pitch_of_frame(mag, bin_hz)
            if sal > 0.25 and st["flatness"] < 0.45 and f0 >= 70.0:
                cand = f0
        frame_cand.append((st["rms"], cand))

        if progress and fi % 200 == 0:
            print(f"\r  分析中 {fi}/{n_frames} 帧", end="", file=sys.stderr, flush=True)

    if progress:
        print("\r  分析完成" + " " * 24, file=sys.stderr)

    # ---------------- 电平
    peak = max(max(left), -min(left)) if n else 0.0
    rms_all = math.sqrt(sum(v * v for v in mono) / max(1, n) + EPS)
    peak_dbfs = 20 * math.log10(max(peak, 1e-6))
    rms_dbfs = 20 * math.log10(max(rms_all, 1e-6))
    crest = peak_dbfs - rms_dbfs
    clip_ratio = sum(1 for v in mono if abs(v) > 0.999) / max(1, n)

    frame_db = [20 * math.log10(max(r, 1e-6)) for r in rms_list]
    srt = sorted(frame_db)
    lra = srt[int(0.95 * (len(srt) - 1))] - srt[int(0.05 * (len(srt) - 1))] if srt else 0.0

    # ---------------- 速度
    # 优先用 250 Hz 以下的起音包络 —— 那是「脚会跟着跺」的那条线。
    # 反拍有镲之类的素材，全频段包络会被拉到倍速；低频包络是唯一稳定的解。
    fps = sr / float(HOP)
    env_full = _onset_envelope(flux_list)
    env_low = _onset_envelope(low_flux_list)
    bpm_full, clr_full, str_full = _tempo_from_env(env_full, fps)
    bpm_low, clr_low, str_low = _tempo_from_env(env_low, fps)

    tempo_source = "none"
    has_low = str_low >= T_MIN_STRENGTH and clr_low >= T_MIN_CLARITY
    has_full = str_full >= T_MIN_STRENGTH and clr_full >= T_MIN_CLARITY
    if has_low:
        tempo_bpm, beat_clarity, tempo_source = bpm_low, clr_low, "low-band"
    elif has_full:
        tempo_bpm, beat_clarity, tempo_source = bpm_full, clr_full, "full-band"
    else:
        tempo_bpm, beat_clarity = 0.0, max(clr_low, clr_full)
    tempo_strength = max(str_low, str_full)

    tempo_alt = 0.0
    if tempo_bpm > 0:
        tempo_alt = tempo_bpm * 2 if tempo_bpm * 2 <= 200 else round(tempo_bpm / 2, 2)

    env = env_full
    mean_env = sum(env) / max(1, len(env))
    std_env = math.sqrt(sum((v - mean_env) ** 2 for v in env) / max(1, len(env)))

    # ---------------- 起音密度
    onset_thr = mean_env + 0.8 * std_env
    onsets = 0
    for i in range(2, len(env) - 2):
        if env[i] > onset_thr and env[i] == max(env[i - 2:i + 3]):
            onsets += 1
    onset_rate = onsets / dur if dur > 0 else 0.0

    # ---------------- 人声线索
    # 单帧「有谐波」说明不了任何事：一记底鼓的谐波比人声还干净。
    # 人声的判别特征是**持续且音高连续** —— 所以只统计 ≥5 帧（160 ms）、相邻帧半音差
    # 在 1.5 以内的连续段。瞬态再亮也串不成这么长一条线。
    voiced = 0
    f0s: list[float] = []
    voiced_runs = 0
    mx_rms = max(rms_list) if rms_list else 0.0
    energy_gate = 0.02 * mx_rms
    run: list[float] = []

    def _close(a: float, b: float) -> bool:
        return abs(12.0 * math.log2(max(a, 1.0) / max(b, 1.0))) < 1.5

    for rms_v, cand in frame_cand:
        sz = rms_v > energy_gate
        ok = bool(sz and cand > 0 and (not run or _close(cand, run[-1])))
        if ok:
            run.append(cand)
        else:
            if len(run) >= 5:
                voiced_runs += 1
                voiced += len(run)
                f0s.extend(run)
            run = [cand] if (sz and cand > 0) else []
    if len(run) >= 5:
        voiced_runs += 1
        voiced += len(run)
        f0s.extend(run)

    # ---------------- 频段占比
    band_total = sum(band_sum.values()) + EPS
    ratios = {k: round(v / band_total, 4) for k, v in band_sum.items()}

    # ---------------- 调性
    csum = sum(chroma) + EPS
    chroma_n = [c / csum for c in chroma]
    key, mode, key_conf = _estimate_key(chroma_n)

    # ---------------- 立体声
    step = 8
    l2 = r2 = lr = side = mid = 0.0
    for i in range(0, n, step):
        a = left[i]
        b = right[i]
        l2 += a * a
        r2 += b * b
        lr += a * b
        side += (a - b) ** 2
        mid += (a + b) ** 2
    stereo_corr = lr / math.sqrt(l2 * r2 + EPS) if l2 > EPS and r2 > EPS else 1.0
    side_ratio = side / (mid + EPS)

    # ---------------- 尾音 / 淡入淡出 / 静音占比
    reverb_ms = _reverb_tail(rms_list, sr)
    fade_in, fade_out = _fades(rms_list, sr)
    peak_frame = max(rms_list) if rms_list else 0.0
    silence_ratio = sum(1 for r in rms_list if r < peak_frame * 0.01) / max(1, len(rms_list))
    loop_ok = _loop_friendly(rms_list)

    feat = {
        "duration_s": round(dur, 3),
        "peak_dbfs": round(peak_dbfs, 2),
        "rms_dbfs": round(rms_dbfs, 2),
        "crest_db": round(crest, 2),
        "lra_db": round(max(0.0, lra), 2),
        "clip_ratio": round(clip_ratio, 6),
        "tempo_bpm": round(tempo_bpm, 2),
        "tempo_alt_bpm": round(tempo_alt, 2),
        "tempo_source": tempo_source,
        "beat_clarity": round(beat_clarity, 3),
        "tempo_strength": round(tempo_strength, 3),
        "onset_rate": round(onset_rate, 3),
        "flux_ratio": round(sum(flux_list) / max(1, len(flux_list)), 4),
        "centroid_hz": round(sum(centroid_list) / max(1, len(centroid_list)), 1),
        "centroid_std_hz": round(_std(centroid_list), 1),
        "centroid_slope_hz_s": round(_centroid_slope(centroid_list, rms_list, sr), 1),
        "rolloff85_hz": round(sum(rolloff_list) / max(1, len(rolloff_list)), 1),
        "flatness": round(sum(flat_list) / max(1, len(flat_list)), 4),
        "zcr": round(sum(zcr_list) / max(1, len(zcr_list)), 4),
        "band_ratios": ratios,
        "key": key,
        "key_mode": mode,
        "key_confidence": round(key_conf, 3),
        "chroma": [round(c, 4) for c in chroma_n],
        "voiced_ratio": round(voiced / max(1, n_frames), 3),
        "voiced_segments": voiced_runs,
        "f0_hz": round(_median(f0s), 1) if f0s else 0.0,
        "pitch_std_semitones": round(_pitch_std(f0s), 2),
        "stereo_corr": round(max(-1.0, min(1.0, stereo_corr)), 3),
        "side_ratio": round(side_ratio, 4),
        "reverb_tail_ms": int(reverb_ms),
        "fade_in_s": round(fade_in, 2),
        "fade_out_s": round(fade_out, 2),
        "silence_ratio": round(silence_ratio, 3),
        "loop_friendly": loop_ok,
    }

    traits = derive_traits(feat)
    warnings: list[str] = []
    if peak_dbfs < -60:
        raise SilentAudio(f"峰值只有 {peak_dbfs:.1f} dBFS，基本是静音，无法分析。")
    if peak_dbfs > -0.1 or clip_ratio > 0.001:
        warnings.append("存在削波：第 5 项「动态」读数会偏乐观，成片判断需打七折")
    if dur < 3.0:
        warnings.append(f"时长只有 {dur:.1f}s：速度与调性估计不可靠，建议按音效路径处理")
    if ratios.get("air", 0) < 0.005:
        warnings.append("6–8 kHz 几乎为空：可能是有损压缩或本来就偏暗，亮度标签可信度下降")
    if abs(stereo_corr - 1.0) < 0.001 and side_ratio < 1e-6:
        warnings.append("双声道完全相同（假立体声）：宽度标签按单声道处理")
    if feat["key_confidence"] < 0.55:
        warnings.append("调性相关性偏低：可能无明确调中心（音效 / 无调性音乐），key 字段仅供参考")

    return {
        "meta": dict(loaded["audio"], analyzer_version="1.0.0"),
        "features": feat,
        "traits": traits,
        "warnings": warnings,
    }


# ------------------------------------------------------------------ 派生标签

def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((v - m) ** 2 for v in xs) / (len(xs) - 1))


def _median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    h = len(s) // 2
    return s[h] if len(s) % 2 else 0.5 * (s[h - 1] + s[h])


def _pitch_std(f0s: list[float]) -> float:
    if len(f0s) < 3:
        return 0.0
    med = _median(f0s)
    if med <= 0:
        return 0.0
    vals = [12.0 * math.log2(f / med) for f in f0s]
    return _std(vals)


def _reverb_tail(rms_list: list[float], sr: int) -> float:
    """起音后衰减 20 dB 所需时间的粗估（毫秒），取中位数。"""
    if len(rms_list) < 8:
        return 0.0
    mx = max(rms_list)
    if mx <= 0:
        return 0.0
    onsets = [i for i in range(2, len(rms_list) - 2)
              if rms_list[i] > 0.6 * mx and rms_list[i] == max(rms_list[i - 2:i + 3])]
    tails: list[float] = []
    for i in onsets[:20]:
        target = rms_list[i] * 0.1
        for j in range(i + 1, min(len(rms_list), i + 120)):
            if rms_list[j] <= target:
                tails.append((j - i) * HOP / sr * 1000.0)
                break
    return _median(tails) if tails else 0.0


def _fades(rms_list: list[float], sr: int) -> tuple[float, float]:
    if not rms_list:
        return 0.0, 0.0
    mx = max(rms_list)
    if mx <= 0:
        return 0.0, 0.0
    half = 0.5 * mx
    fi = 0
    for i, r in enumerate(rms_list):
        if r >= half:
            fi = i
            break
    fo = len(rms_list) - 1
    for i in range(len(rms_list) - 1, -1, -1):
        if rms_list[i] >= half:
            fo = len(rms_list) - 1 - i
            break
    frame_s = HOP / sr
    return fi * frame_s, fo * frame_s


def _loop_friendly(rms_list: list[float]) -> bool:
    if len(rms_list) < 6:
        return False
    mx = max(rms_list)
    if mx <= 0:
        return False
    head = sum(rms_list[:3]) / 3.0
    tail = sum(rms_list[-3:]) / 3.0
    return abs(head - tail) < 0.15 * mx and head > 0.05 * mx and tail > 0.05 * mx


def _estimate_key(chroma: list[float]) -> tuple[str, str, float]:
    """色度与 KK 轮廓做旋转相关，返回 (主音, 大小调, 置信度)。"""
    best = (-2.0, "C", "major")
    scores: list[float] = []
    for mode, prof in (("major", KK_MAJOR), ("minor", KK_MINOR)):
        for root in range(12):
            rot = [prof[(i - root) % 12] for i in range(12)]
            sc = _corr(chroma, rot)
            scores.append(sc)
            if sc > best[0]:
                best = (sc, PC_NAMES[root], mode)
    scores.sort(reverse=True)
    conf = 0.0
    if len(scores) >= 2:
        conf = max(0.0, min(1.0, (scores[0] - scores[1]) / 0.25))
    return best[1], best[2], conf


def _corr(a: list[float], b: list[float]) -> float:
    n = len(a)
    ma = sum(a) / n
    mb = sum(b) / n
    num = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    da = math.sqrt(sum((v - ma) ** 2 for v in a))
    db = math.sqrt(sum((v - mb) ** 2 for v in b))
    return num / (da * db + EPS)


def derive_traits(f: dict) -> dict:
    """数值 → 标签。阈值集中在这里，改判定规则只改这个函数。"""
    def pick(value: float, table: list[tuple[float, str]], default: str) -> str:
        for edge, name in table:
            if value < edge:
                return name
        return default

    bpm = f["tempo_bpm"]
    # 边界加 0.5：让 80.0 BPM 明确落在 slow 而不是踩到 midtempo 的线上
    tempo_band = pick(bpm, [(60.5, "very_slow"), (80.5, "slow"), (105.5, "midtempo"),
                            (125.5, "up"), (150.5, "fast")], "frantic")
    if bpm <= 0:
        tempo_band = "free"

    c = f["centroid_hz"]
    brightness = pick(c, [(500, "very_dark"), (1200, "dark"), (2500, "neutral"),
                          (4500, "bright")], "glass")

    rms = f["rms_dbfs"]
    energy = pick(rms, [(-30, "quiet"), (-18, "moderate"), (-10, "loud")], "hot")

    fl = f["flatness"]
    texture = pick(fl, [(0.15, "tonal"), (0.4, "mixed")], "noisy")

    orate = f["onset_rate"]
    density = pick(orate, [(2, "sparse"), (5, "medium")], "dense")

    lra = f["lra_db"]
    dynamics = pick(lra, [(6, "compressed"), (12, "controlled")], "wide")

    tail = f["reverb_tail_ms"]
    space = pick(tail, [(80, "dry"), (300, "room"), (900, "hall")], "cavern")

    corr = f["stereo_corr"]
    sr_ = f["side_ratio"]
    if corr > 0.995 and sr_ < 1e-5:
        width = "mono"
    else:
        width = pick(corr, [(0.6, "very_wide"), (0.9, "wide")], "narrow")

    low = f["band_ratios"].get("sub", 0) + f["band_ratios"].get("bass", 0)
    low_end = pick(low, [(0.15, "light"), (0.35, "balanced"), (0.55, "heavy")], "overwhelming")

    vr = f["voiced_ratio"]
    vocal = pick(vr, [(0.08, "instrumental"), (0.25, "some")], "prominent")

    return {
        "tempo_band": tempo_band,
        "brightness": brightness,
        "energy": energy,
        "texture": texture,
        "density": density,
        "dynamics": dynamics,
        "space": space,
        "width": width,
        "low_end": low_end,
        "vocal_presence": vocal,
    }


def guess_kind(f: dict, t: dict) -> dict:
    """粗判这是音乐 / 音效 / 人声 / 环境声，带一个 0..1 的把握度。"""
    dur = f.get("duration_s", 0.0)
    beat = f.get("beat_clarity", 0.0)
    vr = f.get("voiced_ratio", 0.0)
    onsets = f.get("onset_rate", 0.0)
    t_bpm = f.get("tempo_bpm", 0.0)

    crest = f.get("crest_db", 0.0)
    if dur < 3.5 and crest > 10:
        return {"kind": "sfx", "confidence": 0.8,
                "why": f"时长 {dur:.1f}s < 3.5s 且波峰因数 {crest:.1f} dB 偏高（瞬态型）"}
    if vr > 0.35 and beat < 2.5 and dur >= 3.5:
        return {"kind": "voice", "confidence": 0.55,
                "why": f"有声帧占比 {vr:.2f}，但节拍清晰度只有 {beat:.2f}（更像人声而非编曲）"}
    if t_bpm > 0 and beat >= 3.0:
        return {"kind": "music", "confidence": round(min(0.95, 0.55 + beat / 12.0), 2),
                "why": f"节拍清晰度 {beat:.2f}、锁定 {t_bpm:.0f} BPM，起音密度 {onsets:.1f}/s"}
    if t_bpm > 0 or (onsets > 1.5 and dur >= 3.5):
        return {"kind": "music", "confidence": 0.5,
                "why": f"有周期性起音（{onsets:.1f}/s），但清晰度 {beat:.2f} 偏软，可能是松散的节奏型"}
    return {"kind": "ambience", "confidence": 0.5,
            "why": f"无明显节拍（{beat:.2f}），起音稀疏（{onsets:.1f}/s），事件型内容不明"}


# ------------------------------------------------------------------ CLI

def _summary(rep: dict) -> str:
    f, t = rep["features"], rep["traits"]
    kind = guess_kind(f, t)
    lines = [
        "─" * 62,
        f"文件      {Path(rep['meta']['path']).name}"
        f"  ({f['duration_s']:.1f}s, {rep['meta']['decoder']})",
        f"判定类型  {kind['kind']} (把握 {kind['confidence']:.2f}) ← {kind['why']}",
        "─" * 62,
        f"速度      {f['tempo_bpm']:.1f} BPM   节拍清晰度 {f['beat_clarity']:.2f}"
        f"   起音 {f['onset_rate']:.1f}/s",
        f"调性      {f['key']} {f['key_mode']}   置信 {f['key_confidence']:.2f}",
        f"电平      RMS {f['rms_dbfs']:.1f} dBFS   峰 {f['peak_dbfs']:.1f} dBFS"
        f"   波峰因数 {f['crest_db']:.1f} dB   动态 {f['lra_db']:.1f} dB",
        f"明暗      质心 {f['centroid_hz']:.0f} Hz   滚降85 {f['rolloff85_hz']:.0f} Hz"
        f"   斜率 {f['centroid_slope_hz_s']:+.0f} Hz/s",
        f"纹理      平坦度 {f['flatness']:.3f} (" + t["texture"] + ")"
        f"   ZCR {f['zcr']:.3f}",
        "频段占比  " + "  ".join(f"{k} {v * 100:.0f}%" for k, v in f["band_ratios"].items()),
        f"空间      尾音 {f['reverb_tail_ms']} ms (" + t["space"] + ")"
        f"   立体声相关 {f['stereo_corr']:.2f} (" + t["width"] + ")",
        f"人声      有声帧 {f['voiced_ratio']:.2f}   f0 {f['f0_hz']:.0f} Hz"
        f"   音高抖动 {f['pitch_std_semitones']:.2f} 半音",
        "标签      " + " / ".join(f"{k}={v}" for k, v in t.items()),
    ]
    for w in rep["warnings"]:
        lines.append("注意      " + w)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

    ap = argparse.ArgumentParser(
        description="把音频拆成可复核的数字，并按固定阈值翻成标签。",
        epilog="示例：\n"
               "  python analyze_audio.py track.mp3 --out out/track\n"
               "  python analyze_audio.py loop.wav --window start --summary\n"
               "  python analyze_audio.py sfx.wav --max-seconds 8 --json\n",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("audio", help="输入音频；wav 原生支持，其余格式需 ffmpeg")
    ap.add_argument("--out", help="输出目录，写入 features.json")
    ap.add_argument("--json", action="store_true", help="把 JSON 打到 stdout")
    ap.add_argument("--summary", action="store_true", help="打印人类可读摘要（默认开启）")
    ap.add_argument("--max-seconds", type=float, default=60.0, help="最多分析多少秒（默认 60）")
    ap.add_argument("--window", choices=("center", "start"), default="center",
                    help="超长时截取中段还是开头（默认 center）")
    ap.add_argument("--sr", type=int, default=TARGET_SR, help="分析采样率（默认 16000）")
    ap.add_argument("--ffmpeg", help="ffmpeg 可执行文件路径（默认自动查找）")
    ap.add_argument("--quiet", action="store_true", help="只输出 JSON，不打印摘要")
    args = ap.parse_args(argv)

    try:
        rep = analyze(args.audio, sr=args.sr, max_seconds=args.max_seconds,
                      window=args.window, ffmpeg=args.ffmpeg,
                      progress=not args.quiet and not args.json)
    except SilentAudio as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 3
    except AudioError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    rep["kind"] = guess_kind(rep["features"], rep["traits"])

    if args.out:
        od = Path(args.out)
        od.mkdir(parents=True, exist_ok=True)
        (od / "features.json").write_text(
            json.dumps(rep, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"已写入 {od / 'features.json'}", file=sys.stderr)

    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    elif not args.quiet:
        print(_summary(rep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
