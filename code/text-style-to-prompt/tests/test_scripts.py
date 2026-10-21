#!/usr/bin/env python3
"""scripts/ 下三个脚本的回归测试。纯标准库，无外部素材，直接跑：

    python tests/test_scripts.py
    python -m unittest discover -s tests -v

覆盖：
  - style_probe：探针在中文 / 西文 / 空文本上的稳定性
  - build_prompt：空话报错、缺字段报错、合法 spec 通过、三档产物都能生成
  - style_compare：自比满分、不同文本出分、非法输入退出码 2
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_tsp_{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


style_probe = load("style_probe")
build_prompt = load("build_prompt")
style_compare = load("style_compare")

ZH_SAMPLE = (
    "芯片不怕快。怕的是没人用。\n\n"
    "工艺往下走，成本往上抬——这是常识。可常识挡不住叙事。\n\n"
    "我们看到的是 3nm，看不到的是良率。而良率，从来不上发布会。\n\n"
    "有人会说，那就降价。降价能让客户进来，不能让良率上去。"
    "良率是工艺的函数，不是价格的函数。\n\n"
    "一台机器可以买到。一支能调到良率的队伍，买不到。\n"
    "这就是为什么追赶很难。不是差一代设备。是差一代人。\n\n"
    "算力也一样。峰值算力是标称值，实际吞吐取决于内存带宽。\n"
    "一块卡标多少 TFLOPS，写在参数表第一行。内存带宽写在第七行。"
    "真正决定训练时间的是第七行。\n\n"
    "工程师对此的回应很朴素：少搬一点。把数据切块，让它在缓存里多待一会儿。"
    "听起来不像突破，像节约。\n\n所有的性能优化，最后都长得像节约。\n\n"
    "还有一个被忽略的变量：工具。设计一颗先进芯片，要跑完一整套签核流程。"
    "流程不是软件，是几十年累积下来的规则库。\n\n"
    "规则库的特点是，你说不出它哪里对。它只是让芯片能过签核。换一家工具，"
    "同一份设计报告会多出几百条违例。\n\n"
    "所以替代不是把软件换掉。是把几千条没写下来的经验重新踩一遍。\n\n"
    "资本能买设备，买不到规则库。政策能建厂，建不了良率曲线。"
    "这两件事都需要时间，而时间不接受加速。\n\n"
    "我们常说要补短板。短板这个词有误导性，它暗示只要把板子补齐，桶就能装水。"
    "可这里的板子之间是焊死的。动一块，旁边三块跟着变形。\n\n"
    "慢一点，也是一种快。"
)

EN_SAMPLE = (
    "The spec says one thing. The die says another.\n\n"
    "Yield is a function of process. It is not a function of price.\n\n"
    "You can buy the tool. You cannot buy the team that tunes it. "
    "The team takes ten years. Ten years of rejects, of meetings, "
    "of nights where nothing works and nobody can say why.\n\n"
    "That is why catching up is hard. It is not a generation of equipment. "
    "It is a generation of people."
)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def minimal_spec(**overrides) -> dict:
    spec = {
        "meta": {"name": "测试风格", "source": "x", "confidence": "medium"},
        "identity": {"role": "你按规格写作。", "task": "写一段 500 字短文。"},
        "specs": [
            {
                "axis": "syntax",
                "rule": "单句 8–22 字，全文句均不超过 16 字",
                "brief": "句子短",
                "evidence": "3nm 是节点名。不是尺寸。",
                "check": "句均长不超过 16 字",
            },
            {
                "axis": "structure",
                "rule": "每段 2–3 句，单句独立成段不少于 2 处",
                "brief": "段短",
                "evidence": "不是差一代设备。是差一代人。",
                "check": "没有超过 4 句的段；单句成段至少 2 处",
            },
            {
                "axis": "lexicon",
                "rule": "禁用赋能/闭环/生态，每千字虚词不超过 130",
                "brief": "禁黑话",
                "evidence": "良率是工艺的函数，不是价格的函数。",
                "check": "禁用词 0 个",
            },
        ],
        "negative": {"words": ["赋能", "闭环"], "patterns": [], "topics": []},
        "anchors": [],
        "selfcheck": ["句均长不超过 16 字"],
        "metrics": {"sentence_mean": 13.0},
    }
    spec.update(overrides)
    return spec


# ================================================================ probe

class ProbeTest(unittest.TestCase):

    def test_zh_uses_char_unit(self):
        d = style_probe.probe(ZH_SAMPLE)
        self.assertEqual(d["meta"]["unit"], "char")
        self.assertGreater(d["meta"]["zh_ratio"], 0.6)
        self.assertGreater(d["sentence"]["count"], 5)
        self.assertGreater(d["sentence"]["mean"], 0)
        self.assertGreater(d["lexicon"]["de_per_kchar"], 0)

    def test_en_uses_word_unit(self):
        d = style_probe.probe(EN_SAMPLE)
        self.assertEqual(d["meta"]["unit"], "word")
        self.assertLess(d["meta"]["zh_ratio"], 0.4)
        self.assertGreater(d["sentence"]["count"], 3)

    def test_short_sample_flag(self):
        d = style_probe.probe("短句。短句。")
        self.assertTrue(d["meta"]["short_sample"])

    def test_long_sample_no_flag(self):
        # 用项目里实际的 ≥ 800 字样本
        text = (ROOT / "examples" / "sample-input.md").read_text(encoding="utf-8")
        d = style_probe.probe(text)
        self.assertFalse(d["meta"]["short_sample"])

    def test_punct_dash_counts_paired_em_dash(self):
        text = "前一句——后一句。" * 30
        d = style_probe.probe(text)
        self.assertGreater(d["punct"]["dash"], 0)

    def test_dialog_quote_detection(self):
        text = "他说「今天不上班」然后走了。" * 20
        d = style_probe.probe(text)
        self.assertGreater(d["dialog"]["quote_count"], 0)

    def test_summary_runs_without_error(self):
        d = style_probe.probe(ZH_SAMPLE)
        s = style_probe.summary(d)
        self.assertIn("句长", s)
        self.assertIn("标点", s)

    def test_cli_writes_json_and_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "in.txt"
            out = Path(tmp) / "out" / "p.json"
            write(f, ZH_SAMPLE)
            self.assertEqual(
                style_probe.main(["--text", str(f), "--out", str(out), "--quiet"]),
                0)
            self.assertTrue(out.is_file())
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(data["meta"]["source"], str(f))

    def test_cli_empty_file_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "empty.txt"
            write(f, "   \n  ")
            self.assertEqual(style_probe.main(["--text", str(f)]), 2)

    def test_cli_missing_file_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.txt"
            self.assertEqual(style_probe.main(["--text", str(missing)]), 2)


# ================================================================ build_prompt

class BuildPromptTest(unittest.TestCase):

    def test_validate_accepts_minimal(self):
        warnings = build_prompt.validate(minimal_spec())
        self.assertIsInstance(warnings, list)

    def test_rejects_empty_words_no_contrast_no_digit(self):
        spec = minimal_spec()
        spec["specs"][0]["rule"] = "文笔细腻富有张力"
        spec["specs"][0]["evidence"] = "原句照抄以兹佐证"
        spec["specs"][0]["check"] = "读起来有节奏感"
        with self.assertRaises(build_prompt.SpecError):
            build_prompt.validate(spec)

    def test_allows_empty_words_with_contrast(self):
        spec = minimal_spec()
        # 「优美」是空话，但 rule 自带对照「不用 A 用 B」-> 放行
        spec["specs"][0]["rule"] = "段落优美，不用感叹号改用句号"
        spec["specs"][0]["evidence"] = "原句示例证据内容"
        spec["specs"][0]["check"] = "感叹号 0 个"
        warnings = build_prompt.validate(spec)
        self.assertIsInstance(warnings, list)

    def test_rejects_too_few_specs(self):
        spec = minimal_spec(specs=[minimal_spec()["specs"][0]])
        with self.assertRaises(build_prompt.SpecError):
            build_prompt.validate(spec)

    def test_rejects_too_many_specs(self):
        spec = minimal_spec()
        spec["specs"] = spec["specs"] * 5  # 15 条
        with self.assertRaises(build_prompt.SpecError):
            build_prompt.validate(spec)

    def test_rejects_bad_axis(self):
        spec = minimal_spec()
        spec["specs"][0]["axis"] = "fluorish"
        with self.assertRaises(build_prompt.SpecError):
            build_prompt.validate(spec)

    def test_rejects_bad_confidence(self):
        spec = minimal_spec()
        spec["meta"]["confidence"] = "perfect"
        with self.assertRaises(build_prompt.SpecError):
            build_prompt.validate(spec)

    def test_rejects_too_many_anchors(self):
        spec = minimal_spec()
        spec["anchors"] = ["一句。" * 5, "二句。" * 5, "三句。" * 5, "四句。" * 5]
        with self.assertRaises(build_prompt.SpecError):
            build_prompt.validate(spec)

    def test_three_outputs_generated(self):
        outputs = build_prompt.build(minimal_spec())
        self.assertIn("short", outputs)
        self.assertIn("standard", outputs)
        self.assertIn("anchored", outputs)
        self.assertLess(len(outputs["short"]), 250)
        self.assertIn("风格规格", outputs["standard"])
        self.assertIn("语感锚点", outputs["anchored"])
        # standard 不应包含「角色」和「任务」段直接抄原文句子
        # 但 evidence 字段本就是原文照抄作为佐证，所以不检查「3nm」
        # 这里检查 anchored 比 standard 多出 anchors 段
        self.assertIn("## 6 语感锚点", outputs["anchored"])
        self.assertNotIn("## 6 语感锚点", outputs["standard"])
        # standard 不应出现的章节号是 7（自检在 anchored 才是 7）
        self.assertIn("## 6 交稿前自检", outputs["standard"])
        self.assertIn("## 7 交稿前自检", outputs["anchored"])

    def test_cli_check_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "s.json"
            write(f, json.dumps(minimal_spec(), ensure_ascii=False))
            self.assertEqual(build_prompt.main(["--spec", str(f), "--check"]), 0)

    def test_cli_outdir_writes_four_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "s.json"
            outdir = Path(tmp) / "out" / "demo"
            write(f, json.dumps(minimal_spec(), ensure_ascii=False))
            self.assertEqual(
                build_prompt.main(["--spec", str(f), "--outdir", str(outdir)]),
                0)
            for name in ("prompt-short.txt", "prompt-standard.md",
                          "prompt-anchored.md", "prompt.json"):
                self.assertTrue((outdir / name).is_file(),
                                  f"缺产物 {name}")

    def test_cli_missing_spec_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.json"
            self.assertEqual(build_prompt.main(["--spec", str(missing)]), 2)

    def test_cli_broken_json_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "bad.json"
            write(f, '{"name": "x",}')
            self.assertEqual(build_prompt.main(["--spec", str(f), "--check"]), 2)


# ================================================================ compare

class CompareTest(unittest.TestCase):

    def test_self_compare_is_perfect(self):
        d = style_probe.probe(ZH_SAMPLE)
        result = style_compare.compare(d, d)
        self.assertEqual(result["total"], 100.0)
        for axis in result["axes"]:
            self.assertEqual(axis["score"], 100.0)

    def test_different_text_scores_under_100(self):
        ref = style_probe.probe(ZH_SAMPLE)
        cand = style_probe.probe(EN_SAMPLE)
        result = style_compare.compare(ref, cand)
        # 中英文对照，应该有多轴明显偏低
        self.assertLess(result["total"], 90)
        # 中英对照必然触发 unit 不同警告
        self.assertTrue(any("口径不同" in w for w in result["warnings"]))

    def test_verdict_thresholds(self):
        d = style_probe.probe(ZH_SAMPLE)
        result = style_compare.compare(d, d)
        passed, reasons = style_compare.verdict(result, 75.0, 55.0)
        self.assertTrue(passed)
        self.assertEqual(reasons, [])

    def test_load_rejects_non_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "bad.json"
            write(f, '{"foo": 1}')
            with self.assertRaises(style_compare.CompareError):
                style_compare.load(str(f))

    def test_cli_passes_on_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = style_probe.probe(ZH_SAMPLE)
            f = Path(tmp) / "p.json"
            write(f, json.dumps(d, ensure_ascii=False))
            self.assertEqual(
                style_compare.main(["--ref", str(f), "--cand", str(f),
                                    "--min", "75", "--axis-min", "55"]),
                0)

    def test_cli_missing_input_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.json"
            self.assertEqual(
                style_compare.main(["--ref", str(missing), "--cand", str(missing)]),
                2)


# ================================================================ end-to-end

class EndToEndTest(unittest.TestCase):
    """跑一遍完整流程：probe → build → probe → compare，确认数字不掉链。"""

    def test_pipeline_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ref_text = (ROOT / "examples" / "sample-input.md").read_text(encoding="utf-8")
            spec = minimal_spec()
            rev = None
            # 1. probe
            ref_fp = style_probe.probe(ref_text)
            self.assertGreater(ref_fp["meta"]["chars"], 300)
            # 2. build (validate 内部跑过)
            outputs = build_prompt.build(spec)
            self.assertIn("standard", outputs)
            # 3. 用「换主题的复刻文本」当 cand —— 这里直接拿同文本兜底
            spec_path = root / "spec.json"
            write(spec_path, json.dumps(spec, ensure_ascii=False))
            self.assertEqual(
                build_prompt.main(["--spec", str(spec_path), "--check"]), 0)
            # 4. compare 自比应满分
            d1 = root / "ref.json"
            write(d1, json.dumps(ref_fp, ensure_ascii=False))
            self.assertEqual(
                style_compare.main(
                    ["--ref", str(d1), "--cand", str(d1), "--min", "75"]),
                0)


if __name__ == "__main__":
    unittest.main(verbosity=2)