#!/usr/bin/env python3
"""CLI 层回归测试：把三个脚本当命令跑一遍，验证退出码与产物。

selftest.py 验证的是「函数算得对不对」，这里验证的是「当工具用时好不好使」——
退出码、--help、落盘文件、错误输入的报错是否在 stderr。两者都会漏东西，
所以两个都留着。

用法
----
    python tests/test_scripts.py
    python tests/test_scripts.py -v

退出码：全绿 0，有失败 1。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
PY = sys.executable

FAILS: list[str] = []
N = [0]
VERBOSE = False


def run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([PY] + [str(a) for a in args], cwd=str(cwd or ROOT),
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


def check(cond: bool, msg: str, detail: str = "") -> None:
    N[0] += 1
    if not cond:
        FAILS.append(f"{msg} {detail}")
        print(f"  ✗ {msg}  {detail}")
    elif VERBOSE:
        print(f"  ✓ {msg}")


def main(argv: list[str] | None = None) -> int:
    global VERBOSE
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    VERBOSE = "-v" in (argv or sys.argv[1:])

    tmp = Path(tempfile.mkdtemp(prefix="apr-cli-"))
    probes = tmp / "probes"
    check(PY.lower().endswith(("python.exe", "python", "python3")), "用当前解释器跑子进程", PY)

    print("\n[探针生成]")
    r = run([SCRIPTS / "selftest.py", "--emit", probes])
    check(r.returncode == 0, "selftest 全绿", r.stdout[-200:] + r.stderr[-200:])
    check((probes / "kick-120.wav").is_file(), "探针 wav 已生成")
    check(len(list(probes.glob("*.wav"))) >= 6, "探针数量足够", str(len(list(probes.glob('*.wav')))))

    print("\n[analyze_audio CLI]")
    out = tmp / "out" / "kick120"
    r = run([SCRIPTS / "analyze_audio.py", probes / "kick-120.wav", "--out", out, "--quiet"])
    check(r.returncode == 0, "分析 CLI 退出码 0", r.stderr[-300:])
    fj = out / "features.json"
    check(fj.is_file(), "features.json 落盘")
    if fj.is_file():
        rep = json.loads(fj.read_text(encoding="utf-8"))
        bpm = rep["features"]["tempo_bpm"]
        check(116 <= bpm <= 124, "CLI 路径下 BPM 仍然对", str(bpm))
        check("traits" in rep and "features" in rep, "JSON 含 features / traits")
        check(isinstance(rep.get("warnings"), list), "warnings 是列表")

    r = run([SCRIPTS / "analyze_audio.py", probes / "kick-120.wav", "--json", "--quiet"])
    check(r.returncode == 0 and r.stdout.strip().startswith("{"), "--json 输出合法 JSON")
    try:
        json.loads(r.stdout)
        check(True, "--json 可被解析")
    except json.JSONDecodeError as exc:
        check(False, "--json 可被解析", str(exc))

    r = run([SCRIPTS / "analyze_audio.py", probes / "nope.wav", "--quiet"])
    check(r.returncode == 2, "缺文件退出码 2", str(r.returncode))
    check("找不到" in r.stderr or "错误" in r.stderr, "错误信息走 stderr", r.stderr[:80])

    r = run([SCRIPTS / "analyze_audio.py", probes / "silence.wav", "--quiet"])
    check(r.returncode == 3, "静音退出码 3", str(r.returncode))

    r = run([SCRIPTS / "analyze_audio.py", "--help"])
    check(r.returncode == 0 and "usage" in r.stdout.lower(), "--help 可用")

    print("\n[build_prompt CLI]")
    spec = ROOT / "specs" / "demo-music.json"
    if spec.is_file():
        r = run([SCRIPTS / "build_prompt.py", fj, "--spec", spec, "--outdir", tmp / "out" / "prompt"])
        check(r.returncode == 0, "组装 CLI 退出码 0", r.stderr[-300:])
        od = tmp / "out" / "prompt"
        for name in ("prompt.md", "prompt.json", "negative.txt"):
            check((od / name).is_file(), f"{name} 落盘")
        check((od / "targets" / "suno.txt").is_file(), "targets/suno.txt 落盘")
        check((od / "targets" / "elevenlabs-sfx.json").is_file(), "targets/elevenlabs-sfx.json 落盘")
        if (od / "prompt.json").is_file():
            data = json.loads((od / "prompt.json").read_text(encoding="utf-8"))
            check("追溯" in data.get("prompt_zh", "") or len(data.get("prompt_zh", "")) > 20,
                  "prompt_zh 非空")
            check(len(data.get("negatives_zh", [])) > 0, "负向词非空")
    else:
        check(False, "示例 spec 存在", str(spec))

    bad = tmp / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    r = run([SCRIPTS / "build_prompt.py", bad, "--outdir", tmp / "out" / "x"])
    check(r.returncode == 2, "坏 JSON 退出码 2", str(r.returncode))

    print("\n[compare_features CLI]")
    new120 = tmp / "out" / "kick120-copy"
    r = run([SCRIPTS / "analyze_audio.py", probes / "kick-120.wav", "--out", new120, "--quiet"])
    check(r.returncode == 0, "二次分析成功", r.stderr[-200:])
    r = run([SCRIPTS / "compare_features.py", fj, new120 / "features.json"])
    check(r.returncode == 0 and "100" in r.stdout, "同一素材比分为 100", r.stdout[:120])

    new80 = tmp / "out" / "kick80"
    run([SCRIPTS / "analyze_audio.py", probes / "kick-80-hat.wav", "--out", new80, "--quiet"])
    r = run([SCRIPTS / "compare_features.py", fj, new80 / "features.json"])
    check(r.returncode == 0 and "MISMATCH" in r.stdout, "不同素材判 MISMATCH", r.stdout[:120])

    r = run([SCRIPTS / "compare_features.py", fj, new80 / "features.json", "--gate", "80"])
    check(r.returncode == 1, "--gate 未达标退出码 1", str(r.returncode))

    print("\n[无 ffmpeg 时的行为]")
    fake = tmp / "fake.mp3"
    fake.write_bytes(b"not really an mp3")   # 让它过存在性检查，栽在解码那一层
    r = run([SCRIPTS / "analyze_audio.py", fake, "--quiet"])
    check(r.returncode == 2, "压缩格式缺 ffmpeg 时报 2", str(r.returncode))
    check("ffmpeg" in r.stderr, "报错里给出了 ffmpeg 提示", r.stderr[:120])

    shutil.rmtree(tmp, ignore_errors=True)
    print("\n" + "─" * 46)
    if FAILS:
        print(f"失败 {len(FAILS)} / {N[0]} 项")
        return 1
    print(f"全部通过：{N[0]} 项断言")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
