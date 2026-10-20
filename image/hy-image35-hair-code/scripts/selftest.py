#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归自测：数据完整性 + 27 组代码全覆盖 + 质量门该拦的能拦住。

跑法:
    python scripts/selftest.py

任何一项 FAIL 就返回非零退出码。改完 data/hair-matrix.json 或脚本后先跑这个。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import haircode  # noqa: E402
import build_hair_prompt  # noqa: E402

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, msg: str) -> None:
    RESULTS.append((bool(ok), msg))


def fake_args(**kw) -> argparse.Namespace:
    base = dict(
        shape=None, widest=None, fingers=None, hair="fine", forehead=None,
        face_length=None, temple=None, cheek=None, jaw=None, unit="cm",
        face_length_cm=18.5, finger_width=2.2, json_out=None, emit_spec=False,
        style=1, ref=None, out_spec=None, guide=None, out=None,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def t_data(m: dict) -> None:
    cells = m["cells"]
    expect = [a + b for a in "ABC" for b in "ABC"]
    check(sorted(cells) == sorted(expect), f"cells 必须是 9 个（AA..CC），实际 {sorted(cells)}")
    for key, cell in cells.items():
        for ht in "ABC":
            styles = cell["styles"].get(ht, [])
            check(len(styles) >= 2, f"{key}/{ht} 至少 2 款款式，实际 {len(styles)}")
        missing = [k for k in haircode.STRUCT_KEYS if not cell["structure"].get(k)]
        check(not missing, f"{key} 的 structure 缺 {missing}" if missing else f"{key} structure 六项齐全")
        ov = m.get("overrides", {}).get(key, {})
        names = {s for v in cell["styles"].values() for s in v}
        uncovered = sorted(names - set(ov))
        check(not uncovered, f"{key} 有款式没写 overrides：{uncovered}" if uncovered else f"{key} 所有款式都有 overrides")
        # overrides 不能引入不存在的键
        for name, patch in ov.items():
            bad = [k for k in patch if k not in haircode.STRUCT_KEYS]
            check(not bad, f"{key}/{name} overrides 里有未知键 {bad}" if bad else f"{key}/{name} overrides 合法")


def t_all_codes(m: dict) -> None:
    for a in "ABC":
        for b in "ABC":
            for c in "ABC":
                for ht in "ABC":
                    code = a + b + c
                    rep = haircode.build(fake_args(shape=a, widest=b, fingers={"A": "3", "B": "4", "C": "5"}[c], hair=ht))
                    ok = rep["code"] == code and len(rep["recommend"]) >= 2
                    ok = ok and all(all(r["structure"].get(k) for k in haircode.STRUCT_KEYS) for r in rep["recommend"])
                    check(ok, f"{code}/{ht} 能判出代码并给出完整结构" if ok else f"{code}/{ht} 输出不完整")
                    check(rep["alias_graphic"] == m["cells"][a + b]["alias"], f"{code} 原图对应代码取错")


def t_gates(m: dict) -> None:
    good = json.loads((ROOT / "specs" / "example-bba-fine.json").read_text(encoding="utf-8"))
    errs = [i for i in build_hair_prompt.gate(good, m) if i[0] == "error"]
    check(not errs, f"示例 spec 不该有 error，实际 {errs}")

    def mutate(**kw):
        s = json.loads(json.dumps(good))
        for path, val in kw.items():
            node = s
            keys = path.split(".")
            for k in keys[:-1]:
                node = node[k]
            if val is None:
                node.pop(keys[-1], None)
            else:
                node[keys[-1]] = val
        return s

    cases = [
        ("E601", mutate(code="XY")),
        ("E603", mutate(hairtype="Z")),
        ("E605", mutate(**{"style.structure.fringe": "刘海长度适中"})),
        ("E606", mutate(**{"keep.hairline": None})),
        ("E607", mutate(**{"keep.light": None})),
        ("E608", mutate(**{"ref.path": None})),
        ("E604", mutate(**{"style.structure.layer": ""})),
    ]
    for want, spec in cases:
        codes = [i[1].split(" ")[0] for i in build_hair_prompt.gate(spec, m)]
        check(want in codes, f"质量门应报 {want}，实际 {codes}")

    # 正面不该把发质黑名单里的词当建议
    rep = haircode.build(fake_args(shape="C", widest="C", fingers="5", hair="C"))
    blob = " ".join(r["structure"]["nape"] for r in rep["recommend"])
    check("颈根" in blob or "颈中" in blob, "宽脸+下颌宽必须把后颈写在颈中/颈根")


def t_prompt(m: dict) -> None:
    spec = json.loads((ROOT / "specs" / "example-bba-fine.json").read_text(encoding="utf-8"))
    prompt, neg, meta = build_hair_prompt.assemble(spec, m)
    need = ["【任务】", "【身份锁定", "【发际线锁定", "【目标发型", "【发质呈现", "【保持不变", "【发丝质感", "【禁止项】"]
    miss = [n for n in need if n not in prompt]
    check(not miss, f"提示词缺段落 {miss}" if miss else "提示词八段齐全")
    check("**" not in prompt, "提示词里不该有 markdown 加粗标记")
    check(meta["negative_count"] > 10, "负面词太少")
    check(meta["imagegen"]["revise"] is False, "revise 必须是 False")


def t_sheet() -> None:
    try:
        from PIL import Image
    except ImportError:
        check(True, "跳过 contact_sheet（没装 Pillow）")
        return
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        for i, col in enumerate([(90, 90, 96), (120, 100, 90), (80, 105, 120)]):
            Image.new("RGB", (600, 800), col).save(d / f"{i}.png")
        import subprocess
        out = d / "sheet.jpg"
        r = subprocess.run(
            [sys.executable, str(HERE / "contact_sheet.py"), "--ref", str(d / "0.png"),
             "--img", str(d / "1.png"), str(d / "2.png"), "--out", str(out), "--title", "Selftest"],
            capture_output=True,
        )
        ok = out.exists() and ok_size(out)
        check(ok, "contact_sheet 能拼出对比图" if ok else f"contact_sheet 失败：{r.stdout.decode('utf-8', 'ignore')} {r.stderr.decode('utf-8', 'ignore')}")


def ok_size(p: Path) -> bool:
    try:
        from PIL import Image
        with Image.open(p) as im:
            return im.width > 1000 and im.height > 400
    except Exception:
        return False


def main() -> int:
    m = haircode.load_matrix()
    t_data(m)
    t_all_codes(m)
    t_gates(m)
    t_prompt(m)
    t_sheet()

    bad = [msg for ok, msg in RESULTS if not ok]
    for ok, msg in RESULTS:
        if not ok:
            print(f"  [FAIL] {msg}")
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} 通过")
    if bad:
        print(f"{len(bad)} 项失败")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
