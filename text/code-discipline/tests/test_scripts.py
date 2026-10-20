#!/usr/bin/env python3
"""scripts/ 下两个脚本的回归测试。纯标准库，直接跑：

    python tests/test_scripts.py
    python -m unittest discover -s tests -v
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
    spec = importlib.util.spec_from_file_location(f"_cd_{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


build_report = load("build_report")
survey = load("survey")


def minimal(**overrides) -> dict:
    spec = {
        "title": "t",
        "tabs": [{"label": "A", "blocks": [{"type": "text", "text": "x"}]}],
    }
    spec.update(overrides)
    return spec


class ReportValidationTest(unittest.TestCase):
    """非法 spec 必须报错。静默丢内容比报错危险得多：报告少一段没人会发现。"""

    def test_rejects_bad_specs(self):
        cases = {
            "空 tabs": minimal(tabs=[]),
            "tabs 不是数组": minimal(tabs={"a": 1}),
            "缺 tabs": {"title": "t"},
            "空 title": minimal(title=""),
            "verdict 乱写": minimal(verdict="danger"),
            "block type 未知": minimal(tabs=[{"label": "A", "blocks": [{"type": "wat"}]}]),
            "block 为空": minimal(tabs=[{"label": "A", "blocks": []}]),
            "缺 label": minimal(tabs=[{"blocks": [{"type": "text", "text": "x"}]}]),
            "table 列数不匹配": minimal(tabs=[{"label": "A", "blocks": [
                {"type": "table", "head": ["a", "b"], "rows": [["1"]]}]}]),
            "flow 只有一个节点": minimal(tabs=[{"label": "A", "blocks": [
                {"type": "flow", "nodes": [{"title": "x"}]}]}]),
            "cards 缺 title": minimal(tabs=[{"label": "A", "blocks": [
                {"type": "cards", "items": [{"tone": "bad"}]}]}]),
            "tone 非法": minimal(tabs=[{"label": "A", "blocks": [
                {"type": "callout", "tone": "danger", "title": "x", "text": "y"}]}]),
            "重复 tab id": minimal(tabs=[
                {"id": "a", "label": "A", "blocks": [{"type": "text", "text": "x"}]},
                {"id": "a", "label": "B", "blocks": [{"type": "text", "text": "x"}]}]),
            "顶层是数组": [1, 2],
        }
        for name, spec in cases.items():
            with self.subTest(name):
                with self.assertRaises(build_report.SpecError):
                    build_report.validate(spec)

    def test_accepts_all_block_types(self):
        spec = minimal(tabs=[{"label": "A", "count": "3", "blocks": [
            {"type": "heading", "text": "h"},
            {"type": "text", "text": "p"},
            {"type": "callout", "tone": "ok", "title": "c", "text": "d"},
            {"type": "cards", "items": [{"tone": "bad", "title": "c1", "file": "f.py:1",
                                         "trigger": "t", "body": "b", "fix": "x"}]},
            {"type": "table", "head": ["a", "b"], "rows": [["1", "2"]]},
            {"type": "code", "text": "print(1)"},
            {"type": "list", "items": ["i1", "i2"]},
            {"type": "flow", "nodes": [{"title": "n1"}, {"title": "n2", "via": "v"}]},
        ]}])
        html = build_report.render(spec)
        for marker in ("<h2>h</h2>", "<pre><code>", "fvia", "pill", "<table>"):
            self.assertIn(marker, html)

    def test_escapes_user_content(self):
        """spec 里的任何字符串都可能带尖括号，一律转义。"""
        spec = minimal(
            title="<script>alert(1)</script>",
            summary="a & b < c",
            tabs=[{"label": "<i>x</i>", "blocks": [
                {"type": "cards", "items": [{"title": "<img src=x onerror=alert(1)>",
                                             "fix": "\"><script>bad()</script>"}]},
                {"type": "flow", "nodes": [{"title": "a"}, {"title": "b", "via": "<svg/onload=1>"}]},
                {"type": "code", "text": "</code><script>evil()</script>"},
            ]}],
        )
        html = build_report.render(spec)
        self.assertNotIn("<script>alert(1)", html)
        self.assertNotIn("<img src=x", html)
        self.assertNotIn("<svg/onload", html)
        self.assertNotIn("<script>evil()", html)
        self.assertIn("&lt;script&gt;alert", html)

    def test_write_and_check_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec_file = Path(tmp) / "s.json"
            spec_file.write_text(json.dumps(minimal(), ensure_ascii=False), encoding="utf-8")
            self.assertEqual(build_report.main(["--spec", str(spec_file), "--check"]), 0)
            out = Path(tmp) / "nested" / "r.html"
            self.assertEqual(
                build_report.main(["--spec", str(spec_file), "--out", str(out)]), 0)
            self.assertTrue(out.is_file())
            self.assertIn("<!DOCTYPE html>", out.read_text(encoding="utf-8"))

    def test_missing_and_broken_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.json"
            self.assertEqual(build_report.main(["--spec", str(missing)]), 2)
            broken = Path(tmp) / "bad.json"
            broken.write_text('{"title": "x",}', encoding="utf-8")
            self.assertEqual(build_report.main(["--spec", str(broken)]), 2)


class SurveyTest(unittest.TestCase):

    def make_project(self, root: Path) -> None:
        (root / "package.json").write_text(json.dumps({
            "name": "demo", "scripts": {"test": "vitest run"}, "dependencies": {"vue": "^3"},
        }), encoding="utf-8")
        (root / "src").mkdir()
        (root / "src" / "index.ts").write_text("export const a = 1;\n", encoding="utf-8")
        (root / "src" / "app.test.ts").write_text("test('a', () => {});\n", encoding="utf-8")
        (root / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = ["pytest"]\n', encoding="utf-8")
        (root / "node_modules").mkdir()
        (root / "node_modules" / "junk.js").write_text("x\n" * 100, encoding="utf-8")

    def test_detects_stack_entry_and_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_project(root)
            data = survey.survey(root, set(), 5)
            self.assertEqual(len(data["manifests"]), 2)
            self.assertIn("src/index.ts", data["entries"])
            self.assertTrue(data["tests"]["has_test_files"])
            joined = " ".join(data["tests"]["commands"])
            self.assertIn("vitest", joined)
            self.assertIn("pytest", joined)
            # node_modules 里的文件不能进统计
            self.assertNotIn("node_modules", json.dumps(data["tree"]["dirs"]))
            md = survey.to_markdown(data)
            self.assertIn("TypeScript", md)

    def test_skips_binary_and_reports_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "blob.py").write_bytes(b"\x00\x01\x02" * 200)
            (root / "ok.py").write_text("import os\n", encoding="utf-8")
            data = survey.survey(root, set(), 5)
            self.assertEqual(data["tree"]["lang_files"].get("Python"), 1)

            empty = root / "empty"
            empty.mkdir()
            md = survey.to_markdown(survey.survey(empty, set(), 5))
            self.assertIn("没有可统计的代码文件", md)

    def test_bad_root_exits_2(self):
        self.assertEqual(survey.main(["--root", str(ROOT / "__nope__")]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
