#!/usr/bin/env python3
"""项目勘察：一次给出「要读懂这个项目」需要的大部分事实。

给模式 1（看清再动手）用。输出不是结论，是**提问的材料**——技术栈、测试命令、
入口、规模、最近的提交风格。剩下的（约定、可复用逻辑、风格）还得自己读文件。

纯标准库，零依赖。

用法：
    python survey.py                          # 扫当前目录，打印 Markdown
    python survey.py --root ../proj           # 指定目录
    python survey.py --json out/survey.json   # 同时落一份 JSON
    python survey.py --top 15                 # 目录规模榜单长度
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

# ---------------------------------------------------------------- 常量

IGNORE_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "bower_components", "vendor",
    "__pycache__", ".venv", "venv", "env", ".tox", ".mypy_cache", ".ruff_cache",
    ".pytest_cache", "dist", "build", "target", "out", "coverage", "htmlcov",
    ".next", ".nuxt", ".svelte-kit", ".turbo", ".parcel-cache", ".idea", ".vscode",
    ".gradle", ".cargo", "Pods", "DerivedData", ".terraform", "site-packages", "obj",
}

LANG_BY_EXT = {
    ".py": "Python", ".pyx": "Cython", ".pyi": "Python",
    ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".jsx": "JavaScript",
    ".vue": "Vue", ".svelte": "Svelte",
    ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin",
    ".rb": "Ruby", ".php": "PHP", ".cs": "C#", ".fs": "F#",
    ".c": "C", ".h": "C", ".cc": "C++", ".cpp": "C++", ".hpp": "C++",
    ".swift": "Swift", ".m": "Objective-C", ".scala": "Scala", ".ex": "Elixir",
    ".sh": "Shell", ".ps1": "PowerShell", ".lua": "Lua", ".dart": "Dart",
    ".sql": "SQL", ".html": "HTML", ".css": "CSS", ".scss": "SCSS",
    ".md": "Markdown", ".json": "JSON", ".yaml": "YAML", ".yml": "YAML",
    ".toml": "TOML", ".xml": "XML",
}

# 这些语言参与「代码占比」，其余的（md/json/yaml）只统计不排名
CODE_LANGS = {
    "Python", "Cython", "JavaScript", "TypeScript", "Vue", "Svelte", "Go", "Rust",
    "Java", "Kotlin", "Ruby", "PHP", "C#", "F#", "C", "C++", "Swift",
    "Objective-C", "Scala", "Elixir", "Shell", "PowerShell", "Lua", "Dart", "SQL",
}

MANIFESTS = [
    "package.json", "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
    "Pipfile", "poetry.lock", "uv.lock", "go.mod", "Cargo.toml", "pom.xml",
    "build.gradle", "build.gradle.kts", "composer.json", "Gemfile", "mix.exs",
    "pubspec.yaml", "CMakeLists.txt", "Makefile", "justfile", "Taskfile.yml",
]

CONFIG_HINTS = [
    ("CI", [".github/workflows", ".gitlab-ci.yml", "Jenkinsfile", ".circleci", "azure-pipelines.yml"]),
    ("容器", ["Dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yaml", ".dockerignore"]),
    ("环境变量", [".env.example", ".env.sample", ".env.template", "env.example"]),
    ("lint/format", [".eslintrc", ".eslintrc.js", ".eslintrc.json", "eslint.config.js",
                     ".prettierrc", "ruff.toml", ".ruff.toml", ".flake8", ".pylintrc",
                     "rustfmt.toml", ".golangci.yml", ".editorconfig"]),
    ("测试配置", ["pytest.ini", "tox.ini", "noxfile.py", "jest.config.js", "jest.config.ts",
                  "vitest.config.ts", "vitest.config.js", "phpunit.xml", ".mocharc.yml",
                  "conftest.py", "tests", "test"]),
    ("类型/构建", ["tsconfig.json", "vite.config.ts", "webpack.config.js", "rollup.config.js",
                   "next.config.js", "web.py", "manage.py"]),
    ("文档约定", ["CONTRIBUTING.md", "AGENTS.md", "CLAUDE.md", ".cursorrules", "docs", "ARCHITECTURE.md"]),
]

TEST_FILE_RE = re.compile(
    r"(^test_.*\.py$)|(.*_test\.py$)|(^tests?$)|(.*\.(test|spec)\.(js|jsx|ts|tsx)$)"
    r"|(.*_test\.go$)|(.*Test\.(java|kt)$)|(.*_spec\.rb$)"
)

ENTRY_CANDIDATES = [
    "main.py", "__main__.py", "app.py", "manage.py", "wsgi.py", "asgi.py",
    "src/main.ts", "src/main.js", "src/index.ts", "src/index.js",
    "index.js", "server.js", "app.js", "main.go", "src/main.rs",
    "Program.cs", "Application.java",
]

MAX_FILES = 40000          # 超过就停止遍历，避免在大仓库里卡死
MAX_BYTES_PER_FILE = 2 * 1024 * 1024   # 单文件最多读 2MB 数行
BINARY_SNIFF = 4096

# 锁文件必然最大，列出来没有信息量
LOCKFILES_SKIP = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock",
    "Cargo.lock", "go.sum", "Pipfile.lock", "composer.lock", "Gemfile.lock",
}


# ---------------------------------------------------------------- 工具


def run_git(root: Path, *args: str, timeout: int = 10) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def count_lines(path: Path) -> int:
    try:
        size = path.stat().st_size
        if size == 0:
            return 0
        with path.open("rb") as fh:
            head = fh.read(BINARY_SNIFF)
            if b"\x00" in head:
                return -1            # 二进制，-1 表示跳过
            fh.seek(0)
            data = fh.read(MAX_BYTES_PER_FILE)
        return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)
    except OSError:
        return -1


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


# ---------------------------------------------------------------- 勘察


def scan_tree(root: Path, excludes: set[str], top: int) -> dict:
    files = 0
    truncated = False
    lang_files = Counter()
    lang_lines = Counter()
    dir_sizes: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # [files, lines]
    tests: list[str] = []
    biggest: list[tuple[int, str]] = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORE_DIRS and d not in excludes)
        rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
        if rel_dir == ".":
            rel_dir = ""

        for name in filenames:
            files += 1
            if files > MAX_FILES:
                truncated = True
                break
            path = Path(dirpath) / name
            ext = path.suffix.lower()
            lang = LANG_BY_EXT.get(ext)
            if not lang:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue

            rel = f"{rel_dir}/{name}" if rel_dir else name
            lines = count_lines(path)
            if lines < 0:
                continue

            lang_files[lang] += 1
            lang_lines[lang] += lines
            if lang in CODE_LANGS:
                bucket = rel_dir.split("/")[0] if rel_dir else "(根目录)"
                dir_sizes[bucket][0] += 1
                dir_sizes[bucket][1] += lines

            is_test_dir = bool(rel_dir) and bool(TEST_FILE_RE.match(rel_dir.split("/")[-1]))
            if TEST_FILE_RE.match(name) or is_test_dir:
                if len(tests) < 60:
                    tests.append(rel)

            if name not in LOCKFILES_SKIP:
                biggest.append((size, rel))
        if truncated:
            break

    biggest.sort(reverse=True)
    return {
        "files_scanned": files,
        "truncated": truncated,
        "lang_files": dict(lang_files.most_common()),
        "lang_lines": dict(lang_lines.most_common()),
        "dirs": {
            k: {"files": v[0], "lines": v[1]}
            for k, v in sorted(dir_sizes.items(), key=lambda kv: -kv[1][1])[:top]
        },
        "tests": tests,
        "biggest": [(human(s), p) for s, p in biggest[:top]],
    }


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def parse_manifest(root: Path) -> list[dict]:
    found = []

    pkg = root / "package.json"
    if pkg.is_file():
        data = read_json(pkg)
        deps = data.get("dependencies") or {}
        dev = data.get("devDependencies") or {}
        found.append({
            "file": "package.json",
            "name": data.get("name", "?"),
            "scripts": data.get("scripts") or {},
            "deps": list(deps)[:12],
            "deps_count": len(deps),
            "dev_deps": list(dev)[:12],
            "dev_deps_count": len(dev),
            "package_manager": "pnpm" if (root / "pnpm-lock.yaml").exists()
            else "yarn" if (root / "yarn.lock").exists()
            else "npm",
        })

    pp = root / "pyproject.toml"
    if pp.is_file():
        info = {"file": "pyproject.toml", "name": "?", "deps": [], "scripts": {}}
        try:
            import tomllib
            data = tomllib.loads(pp.read_text(encoding="utf-8"))
            proj = data.get("project") or {}
            poetry = (data.get("tool") or {}).get("poetry") or {}
            info["name"] = proj.get("name") or poetry.get("name") or "?"
            info["deps"] = [d for d in (proj.get("dependencies") or [])][:12] or list(
                (poetry.get("dependencies") or {}).keys())[:12]
            info["scripts"] = {k: v for k, v in (proj.get("scripts") or {}).items()}
            build = ((data.get("build-system") or {}).get("requires") or [])
            info["build_backend"] = (data.get("build-system") or {}).get("build-backend", "")
            info["build_requires"] = build
        except Exception:
            text = pp.read_text(encoding="utf-8", errors="replace")
            m = re.search(r'^\s*name\s*=\s*"([^"]+)"', text, re.M)
            info["name"] = m.group(1) if m else "?"
            info["note"] = "tomllib 解析失败，仅取到名字"
        found.append(info)

    req = root / "requirements.txt"
    if req.is_file():
        lines = [l.strip() for l in req.read_text(encoding="utf-8", errors="replace").splitlines()]
        pkgs = [l for l in lines if l and not l.startswith("#")]
        found.append({"file": "requirements.txt", "deps_count": len(pkgs), "deps": pkgs[:12]})

    gm = root / "go.mod"
    if gm.is_file():
        head = gm.read_text(encoding="utf-8", errors="replace").splitlines()[:12]
        found.append({"file": "go.mod", "head": head})

    ct = root / "Cargo.toml"
    if ct.is_file():
        text = ct.read_text(encoding="utf-8", errors="replace")
        m = re.search(r'^\s*name\s*=\s*"([^"]+)"', text, re.M)
        found.append({"file": "Cargo.toml", "name": m.group(1) if m else "?", "size": len(text)})

    mk = root / "Makefile"
    if mk.is_file():
        text = mk.read_text(encoding="utf-8", errors="replace")
        targets = re.findall(r"^([a-zA-Z0-9_\-]+):", text, re.M)
        found.append({"file": "Makefile", "targets": [t for t in targets if not t.startswith(".")][:20]})

    return found


def detect_test_command(root: Path, manifests: list[dict]) -> dict:
    """猜测试命令。只给候选，不替人决定。"""
    cmds = []
    names = {m["file"] for m in manifests}
    text_bits = " ".join(
        " ".join(m.get("deps") or []) + " ".join((m.get("scripts") or {}).values())
        for m in manifests
    ).lower()

    has_pytest_cfg = any((root / f).exists() for f in
                         ("pytest.ini", "conftest.py", "tox.ini", "noxfile.py"))
    if "pyproject.toml" in names and re.search(r"\[tool\.pytest", (root / "pyproject.toml").read_text(encoding="utf-8", errors="replace")):
        has_pytest_cfg = True

    if "pytest" in text_bits or has_pytest_cfg:
        cmds.append("python -m pytest -q            # 全量")
        cmds.append("python -m pytest path::test -q  # 单个")
    if (root / "tox.ini").exists():
        cmds.append("tox")
    if "jest" in text_bits:
        cmds.append("npx jest")
    if "vitest" in text_bits:
        cmds.append("npx vitest run")
    if "mocha" in text_bits:
        cmds.append("npx mocha")
    if "go.mod" in names:
        cmds.append("go test ./...")
    if "Cargo.toml" in names:
        cmds.append("cargo test")
    if "Makefile" in names:
        mk = [m for m in manifests if m["file"] == "Makefile"][0]
        for t in mk.get("targets", []):
            if t in ("test", "tests", "check", "lint", "fmt"):
                cmds.append(f"make {t}")
    if "package.json" in names:
        pkg = [m for m in manifests if m["file"] == "package.json"][0]
        for k in ("test", "lint", "typecheck", "build"):
            if k in (pkg.get("scripts") or {}):
                cmds.append(f"{pkg.get('package_manager', 'npm')} run {k}")

    seen, uniq = set(), []
    for c in cmds:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return {"commands": uniq, "has_test_files": None}  # has_test_files 由 scan 填


def find_entries(root: Path) -> list[str]:
    out = []
    for rel in ENTRY_CANDIDATES:
        if (root / rel).is_file():
            out.append(rel)
    cmd_dir = root / "cmd"
    if cmd_dir.is_dir():
        for child in sorted(cmd_dir.iterdir()):
            if (child / "main.go").is_file():
                out.append(f"cmd/{child.name}/main.go")
    src = root / "src"
    if src.is_dir():
        for child in sorted(src.iterdir()):
            if child.name in ("main.rs", "main.go", "index.ts", "index.js"):
                out.append(f"src/{child.name}")
    return out[:8]


def check_configs(root: Path) -> list[dict]:
    groups = []
    for label, candidates in CONFIG_HINTS:
        hits = [c for c in candidates if (root / c).exists()]
        if hits:
            groups.append({"label": label, "hits": hits})
    return groups


def survey(root: Path, excludes: set[str], top: int) -> dict:
    manifests = parse_manifest(root)
    tree = scan_tree(root, excludes, top)
    test_cmd = detect_test_command(root, manifests)
    test_cmd["has_test_files"] = bool(tree["tests"])

    git = {
        "is_repo": bool(run_git(root, "rev-parse", "--git-dir")),
        "branch": run_git(root, "rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": len([l for l in run_git(root, "status", "--porcelain").splitlines() if l]),
        "last_commits": run_git(root, "log", "--oneline", "-12").splitlines(),
        "recent_files": run_git(root, "log", "-1", "--name-only", "--format=").splitlines()[:20],
        "contributors": [
            re.sub(r"\s+", " ", l).strip()
            for l in run_git(root, "shortlog", "-sn", "HEAD").splitlines()[:5]
        ],
    }

    return {
        "root": str(root),
        "manifests": manifests,
        "tree": tree,
        "tests": test_cmd,
        "entries": find_entries(root),
        "configs": check_configs(root),
        "git": git,
    }


# ---------------------------------------------------------------- 输出


def to_markdown(d: dict) -> str:
    L = []
    add = L.append
    add(f"# 项目勘察 · {Path(d['root']).name}")
    add(f"\n`{d['root']}`\n")

    # 技术栈
    add("## 技术栈")
    if d["manifests"]:
        for m in d["manifests"]:
            if m["file"] == "package.json":
                add(f"- **package.json**：`{m['name']}` · {m.get('package_manager')} · "
                    f"{m['deps_count']} 依赖 + {m['dev_deps_count']} dev")
                if m["scripts"]:
                    add(f"  - scripts: {', '.join(f'`{k}`' for k in list(m['scripts'])[:8])}")
                if m["deps"]:
                    add(f"  - 主要依赖: {', '.join(m['deps'][:8])}")
            elif m["file"] == "pyproject.toml":
                add(f"- **pyproject.toml**：`{m['name']}`" + (f" · {m.get('build_backend')}" if m.get("build_backend") else ""))
                if m.get("deps"):
                    add(f"  - 依赖: {', '.join(m['deps'][:8])}")
            elif m["file"] == "requirements.txt":
                add(f"- **requirements.txt**：{m['deps_count']} 个包（{', '.join(m['deps'][:6])}…）")
            elif m["file"] == "go.mod":
                add(f"- **go.mod**：{m['head'][0] if m['head'] else '?'}")
            elif m["file"] == "Cargo.toml":
                add(f"- **Cargo.toml**：`{m['name']}`")
            elif m["file"] == "Makefile":
                add(f"- **Makefile**：{', '.join(m['targets'][:10])}")
    else:
        add("- 没找到任何构建清单。可能是纯脚本目录、或代码在子目录里。**这一点要向用户确认。**")

    # 语言
    add("\n## 语言构成（按行数）")
    langs = [(k, v) for k, v in d["tree"]["lang_lines"].items() if k in CODE_LANGS]
    total = sum(v for _, v in langs) or 1
    if langs:
        for name, lines in langs[:8]:
            pct = lines * 100 / total
            bar = "█" * max(1, round(pct / 4))
            add(f"- {name:<12} {lines:>8,} 行  {pct:>5.1f}%  {bar}")
        other = sum(v for k, v in d["tree"]["lang_lines"].items() if k not in CODE_LANGS)
        add(f"\n非代码（md/json/yaml 等）另有 {other:,} 行；共扫描 {d['tree']['files_scanned']:,} 个文件"
            + ("（**已截断**）" if d["tree"]["truncated"] else ""))
    else:
        add("- 没扫到已知语言的源码文件。")

    # 目录
    add("\n## 目录规模 Top")
    if d["tree"]["dirs"]:
        for name, info in d["tree"]["dirs"].items():
            add(f"- `{name}` — {info['files']} 文件 / {info['lines']:,} 行")
    else:
        add("- （没有可统计的代码文件）")

    # 入口
    add("\n## 入口候选")
    add("- " + (", ".join(f"`{e}`" for e in d["entries"]) if d["entries"] else "没找到典型入口。找 `if __name__`、框架的 app 实例、或 package.json 的 main/bin"))

    # 测试
    add("\n## 测试")
    if d["tests"]["has_test_files"]:
        add(f"- 找到 {len(d['tree']['tests'])} 个测试文件（示例）：")
        for t in d["tree"]["tests"][:8]:
            add(f"  - `{t}`")
    else:
        add("- **没有测试文件**。改动前要先跟用户确认怎么验证，不要自己发明一套框架")
    if d["tests"]["commands"]:
        add("- 可能的命令（按清单推测，**以实际跑通为准**）：")
        for c in d["tests"]["commands"]:
            add(f"  - `{c}`")

    # 配置
    add("\n## 工程配置")
    if d["configs"]:
        for g in d["configs"]:
            add(f"- {g['label']}：{', '.join(g['hits'])}")
    else:
        add("- 没找到 CI / 容器 / lint / env 配置")

    # git
    add("\n## Git")
    g = d["git"]
    if g["is_repo"]:
        add(f"- 分支 `{g['branch']}`，工作区{'**有未提交改动**（' + str(g['dirty']) + ' 个文件）' if g['dirty'] else '干净'}")
        if g["last_commits"]:
            add("- 最近提交（**这是团队真实风格的最佳证据**）：")
            for c in g["last_commits"][:8]:
                add(f"  - `{c}`")
        if g["contributors"]:
            add(f"- 主要作者：{', '.join(g['contributors'])}")
    else:
        add("- 不是 git 仓库")

    # 大文件
    if d["tree"]["biggest"]:
        add("\n## 最大的文件")
        for size, rel in d["tree"]["biggest"][:6]:
            add(f"- {size}  `{rel}`")

    add("\n---")
    add("**下一步**：目标文件全文 + 同目录 2–3 个同类文件 + 调用方，然后是复述和假设清单。"
        "这份勘察只回答「是什么」，不回答「该怎么做」。")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="项目勘察：技术栈、测试命令、入口、规模、git 状态")
    p.add_argument("--root", default=".", help="项目根目录，默认当前目录")
    p.add_argument("--top", type=int, default=10, help="目录/大文件榜单长度，默认 10")
    p.add_argument("--exclude", action="append", default=[], help="额外跳过的目录名，可重复")
    p.add_argument("--json", dest="json_out", help="同时把结果写成 JSON")
    args = p.parse_args(argv)

    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"不是目录：{root}", file=sys.stderr)
        return 2

    data = survey(root, set(args.exclude), args.top)

    if args.json_out:
        out = Path(args.json_out).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON 已写入 {out}", file=sys.stderr)

    print(to_markdown(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
