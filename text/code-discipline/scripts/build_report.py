#!/usr/bin/env python3
"""把检查结果渲染成单文件 HTML 报告，并可直接在浏览器打开。

输入一份 JSON（schema 见 references/07-report.md），输出一个自包含的 .html：
CSS / JS 全部内联，零外链、零 CDN、零第三方包，断网也能看，也能直接发给别人。

用法：
    python build_report.py --spec examples/review-demo.json --out out/review.html
    python build_report.py --spec examples/review-demo.json --open      # 生成后打开浏览器
    python build_report.py --spec examples/review-demo.json --check     # 只校验 spec
"""

from __future__ import annotations

import argparse
import html
import json
import os
import subprocess
import sys
import webbrowser
from pathlib import Path

# ---------------------------------------------------------------- 词表

TONES = ("bad", "warn", "note", "ok", "info")

# tone -> (中文语义, 主色, 底色)
TONE_META = {
    "bad": ("阻断 / 安全", "#b42318", "#fef3f2"),
    "warn": ("逻辑 / 正确性", "#b54708", "#fffaeb"),
    "note": ("性能 / 资源", "#a16207", "#fefce8"),
    "ok": ("通过", "#067647", "#ecfdf3"),
    "info": ("背景 / 说明", "#175cd3", "#eff8ff"),
}

VERDICT_META = {
    "block": ("不可合入", "#b42318", "#fef3f2"),
    "warn": ("有条件合入", "#b54708", "#fffaeb"),
    "pass": ("通过", "#067647", "#ecfdf3"),
}

BLOCK_TYPES = ("heading", "callout", "cards", "table", "code", "list", "flow", "text")


class SpecError(Exception):
    """spec 结构不合法。宁可报错，也不要静默丢内容。"""


# ---------------------------------------------------------------- 校验


def _need(obj: dict, key: str, where: str):
    if key not in obj or obj[key] in (None, "", [], {}):
        raise SpecError(f"{where} 缺少必填字段 {key!r}")
    return obj[key]


def _tone(value: str | None, where: str) -> str:
    if value is None:
        return "info"
    if value not in TONES:
        raise SpecError(f"{where} 的 tone={value!r} 不合法，只能是 {TONES}")
    return value


def validate(spec: dict) -> dict:
    if not isinstance(spec, dict):
        raise SpecError("spec 顶层必须是对象")
    _need(spec, "title", "spec")
    lang = spec.get("lang", "zh-CN")
    if not isinstance(lang, str):
        raise SpecError("spec.lang 必须是字符串")

    verdict = spec.get("verdict", "warn")
    if verdict not in VERDICT_META:
        raise SpecError(f"spec.verdict={verdict!r} 不合法，只能是 {tuple(VERDICT_META)}")

    for i, m in enumerate(spec.get("metrics") or []):
        _need(m, "label", f"metrics[{i}]")
        _tone(m.get("tone"), f"metrics[{i}]")

    tabs = spec.get("tabs")
    if tabs is None:
        raise SpecError("spec 缺少必填字段 'tabs'")
    if not isinstance(tabs, list):
        raise SpecError("spec.tabs 必须是数组")
    if not tabs:
        raise SpecError("spec.tabs 至少需要一个标签页")
    seen_ids = set()
    for ti, tab in enumerate(tabs):
        where = f"tabs[{ti}]"
        _need(tab, "label", where)
        tid = tab.get("id") or f"tab{ti}"
        if tid in seen_ids:
            raise SpecError(f"{where} 的 id={tid!r} 与前面的标签页重复")
        seen_ids.add(tid)
        blocks = tab.get("blocks")
        if not isinstance(blocks, list) or not blocks:
            raise SpecError(f"{where} 至少需要一个 block")

        for bi, blk in enumerate(blocks):
            bw = f"{where}.blocks[{bi}]"
            if not isinstance(blk, dict):
                raise SpecError(f"{bw} 必须是对象")
            btype = _need(blk, "type", bw)
            if btype not in BLOCK_TYPES:
                raise SpecError(f"{bw}.type={btype!r} 不支持，只能是 {BLOCK_TYPES}")
            _tone(blk.get("tone"), bw)

            if btype == "heading":
                _need(blk, "text", bw)
            elif btype == "callout":
                _need(blk, "title", bw)
                _need(blk, "text", bw)
            elif btype == "cards":
                items = _need(blk, "items", bw)
                if not isinstance(items, list):
                    raise SpecError(f"{bw}.items 必须是数组")
                for ci, item in enumerate(items):
                    _need(item, "title", f"{bw}.items[{ci}]")
                    _tone(item.get("tone"), f"{bw}.items[{ci}]")
            elif btype == "table":
                head = _need(blk, "head", bw)
                rows = _need(blk, "rows", bw)
                if not isinstance(head, list) or not isinstance(rows, list):
                    raise SpecError(f"{bw} 的 head / rows 必须是数组")
                for ri, row in enumerate(rows):
                    if not isinstance(row, list) or len(row) != len(head):
                        raise SpecError(
                            f"{bw}.rows[{ri}] 有 {len(row) if isinstance(row, list) else '?'} 列，"
                            f"表头是 {len(head)} 列，对不上"
                        )
            elif btype == "code":
                _need(blk, "text", bw)
            elif btype == "list":
                items = _need(blk, "items", bw)
                if not isinstance(items, list):
                    raise SpecError(f"{bw}.items 必须是数组")
            elif btype == "flow":
                nodes = _need(blk, "nodes", bw)
                if not isinstance(nodes, list) or len(nodes) < 2:
                    raise SpecError(f"{bw}.nodes 至少要有 2 个节点")
                for ni, node in enumerate(nodes):
                    _need(node, "title", f"{bw}.nodes[{ni}]")
                    _tone(node.get("tone"), f"{bw}.nodes[{ni}]")
            elif btype == "text":
                _need(blk, "text", bw)
    return spec


# ---------------------------------------------------------------- 渲染


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def render_flow(blk: dict) -> str:
    out = ['<div class="flow">']
    for i, node in enumerate(blk["nodes"]):
        tone = _tone(node.get("tone"), "flow node")
        label, color, bg = TONE_META[tone]
        via = node.get("via")
        if i and via:
            out.append(f'<div class="fvia"><span>{esc(via)}</span></div>')
        elif i:
            out.append('<div class="fvia fvia-bare"></div>')
        note = node.get("note")
        note_html = f'<span class="fnote">{esc(note)}</span>' if note else ""
        out.append(
            f'<div class="fnode" style="--tc:{color};--tb:{bg}">'
            f'<span class="fidx">{i + 1}</span>'
            f'<span class="ftitle">{esc(node["title"])}</span>'
            f"{note_html}"
            f'<span class="ftag">{esc(label)}</span>'
            f"</div>"
        )
    out.append("</div>")
    return "".join(out)


def render_cards(blk: dict) -> str:
    parts = ['<div class="cards">']
    for item in blk["items"]:
        tone = _tone(item.get("tone"), "card")
        label, color, bg = TONE_META[tone]
        meta = []
        if item.get("file"):
            meta.append(f'<code class="loc">{esc(item["file"])}</code>')
        if item.get("trigger"):
            meta.append(f'<span class="trig">触发：{esc(item["trigger"])}</span>')
        body = f'<p class="cbody">{esc(item["body"])}</p>' if item.get("body") else ""
        fix = f'<p class="cfix"><b>修法</b>{esc(item["fix"])}</p>' if item.get("fix") else ""
        parts.append(
            f'<article class="card" style="--tc:{color};--tb:{bg}">'
            f'<header><span class="pill">{esc(label)}</span>'
            f'<h3>{esc(item["title"])}</h3></header>'
            f'<div class="cmeta">{"".join(meta)}</div>'
            f"{body}{fix}"
            f"</article>"
        )
    parts.append("</div>")
    return "".join(parts)


def render_block(blk: dict) -> str:
    btype = blk["type"]
    tone = _tone(blk.get("tone"), "block")
    _, color, bg = TONE_META[tone]

    if btype == "text":
        return f'<p class="para">{esc(blk["text"])}</p>'

    if btype == "heading":
        return f'<h2>{esc(blk["text"])}</h2>'

    if btype == "callout":
        title = f'<h3>{esc(blk["title"])}</h3>'
        return (
            f'<aside class="callout" style="--tc:{color};--tb:{bg}">'
            f"<div>{title}<p>{esc(blk['text'])}</p></div></aside>"
        )

    if btype == "cards":
        return render_cards(blk)

    if btype == "table":
        head = "".join(f"<th>{esc(h)}</th>" for h in blk["head"])
        rows = "".join(
            "<tr>" + "".join(f"<td>{esc(c)}</td>" for c in row) + "</tr>" for row in blk["rows"]
        )
        caption = f'<caption>{esc(blk["caption"])}</caption>' if blk.get("caption") else ""
        return f'<div class="tw"><table>{caption}<thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table></div>'

    if btype == "code":
        caption = f'<div class="codecap">{esc(blk["caption"])}</div>' if blk.get("caption") else ""
        return f'<div class="codeblock">{caption}<pre><code>{esc(blk["text"])}</code></pre></div>'

    if btype == "list":
        tag = "ol" if blk.get("ordered") else "ul"
        items = "".join(f"<li>{esc(i)}</li>" for i in blk["items"])
        return f'<{tag} class="bullet">{items}</{tag}>'

    if btype == "flow":
        return render_flow(blk)

    raise SpecError(f"未知 block type {btype!r}")


CSS = """
*{box-sizing:border-box}
:root{
  --ink:#101828;--ink2:#475467;--ink3:#667085;--line:#e4e7ec;--line2:#d0d5dd;
  --bg:#f9fafb;--surface:#fff;--mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
}
html{-webkit-text-size-adjust:100%}
body{
  margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  padding:0 20px 72px;
}
.wrap{max-width:1040px;margin:0 auto}

/* ---- 头部 ---- */
header.top{padding:40px 0 28px;border-bottom:1px solid var(--line2);margin-bottom:0}
.eyebrow{font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:var(--ink3);font-weight:600;margin-bottom:10px}
h1{font-size:29px;line-height:1.25;margin:0 0 10px;letter-spacing:-.01em;font-weight:700}
.sub{color:var(--ink2);font-size:14px;margin:0}
.verdict{
  display:inline-flex;align-items:center;gap:8px;margin-top:18px;
  padding:7px 15px 7px 12px;border-radius:999px;font-weight:600;font-size:14px;
  color:var(--vc);background:var(--vb);border:1px solid color-mix(in srgb,var(--vc) 26%,transparent);
}
.verdict::before{content:"";width:8px;height:8px;border-radius:50%;background:var(--vc)}
.summary{
  margin:24px 0 0;padding:16px 20px;background:var(--surface);border:1px solid var(--line);
  border-left:3px solid var(--ink);border-radius:3px;font-size:15.5px;color:var(--ink);
}

/* ---- 指标 ---- */
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));gap:12px;margin:26px 0 0}
.metric{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:13px 15px}
.metric b{display:block;font-size:25px;line-height:1.15;color:var(--mc);font-variant-numeric:tabular-nums}
.metric span{display:block;font-size:12px;color:var(--ink3);margin-top:3px}

/* ---- 标签页 ---- */
nav.tabs{
  position:sticky;top:0;z-index:9;display:flex;gap:2px;flex-wrap:wrap;
  margin:32px 0 0;padding:10px 0 0;background:var(--bg);border-bottom:1px solid var(--line2);
}
nav.tabs button{
  appearance:none;border:0;background:none;font:inherit;font-size:14px;font-weight:500;
  color:var(--ink2);padding:9px 15px;border-radius:7px 7px 0 0;cursor:pointer;position:relative;
  border-bottom:2px solid transparent;margin-bottom:-1px;
}
nav.tabs button:hover{color:var(--ink);background:#f2f4f7}
nav.tabs button[aria-selected=true]{color:var(--ink);font-weight:650;border-bottom-color:var(--ink);background:var(--surface)}
nav.tabs button em{font-style:normal;font-size:11px;color:var(--ink3);margin-left:6px}
nav.tabs button[aria-selected=true] em{color:var(--ink2)}

.panel{display:none;padding:26px 0 0}
.panel[data-active=true]{display:block;animation:fade .18s ease-out}
@keyframes fade{from{opacity:0;transform:translateY(3px)}to{opacity:1;transform:none}}

/* ---- 通用块 ---- */
.panel>*+*{margin-top:18px}
h2{font-size:13px;letter-spacing:.1em;text-transform:uppercase;color:var(--ink3);font-weight:650;margin:30px 0 0}
.panel>h2:first-child{margin-top:0}
.para{margin:0;color:var(--ink)}
.bullet{margin:0;padding-left:22px;color:var(--ink)}
.bullet li{margin:5px 0}
.bullet li::marker{color:var(--ink3)}

.callout{display:flex;gap:13px;padding:15px 18px;border-radius:8px;background:var(--tb);border:1px solid color-mix(in srgb,var(--tc) 22%,transparent);border-left:3px solid var(--tc)}
.callout h3{margin:0 0 5px;font-size:14.5px;color:var(--tc)}
.callout p{margin:0;font-size:14px;color:var(--ink2)}

.cards{display:grid;gap:12px}
.card{background:var(--surface);border:1px solid var(--line);border-left:3px solid var(--tc);border-radius:8px;padding:15px 18px}
.card header{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.card h3{margin:0;font-size:15.5px;font-weight:650;letter-spacing:-.005em}
.pill{font-size:11px;font-weight:650;color:var(--tc);background:var(--tb);border:1px solid color-mix(in srgb,var(--tc) 22%,transparent);padding:2px 8px;border-radius:999px;white-space:nowrap}
.cmeta{display:flex;gap:10px;flex-wrap:wrap;margin:9px 0 0;align-items:center}
.loc{font-family:var(--mono);font-size:12.5px;background:#f2f4f7;border:1px solid var(--line);padding:1.5px 7px;border-radius:5px;color:var(--ink)}
.trig{font-size:12.5px;color:var(--ink2)}
.cbody{margin:10px 0 0;font-size:14px;color:var(--ink2)}
.cfix{margin:9px 0 0;font-size:13.5px;color:var(--ink);background:#f9fafb;border:1px dashed var(--line2);border-radius:6px;padding:8px 11px}
.cfix b{color:var(--ink3);font-weight:650;font-size:11px;letter-spacing:.08em;text-transform:uppercase;margin-right:9px}

.tw{overflow-x:auto;border:1px solid var(--line);border-radius:8px;background:var(--surface)}
table{border-collapse:collapse;width:100%;font-size:13.5px}
caption{caption-side:top;text-align:left;padding:11px 14px;font-size:12.5px;color:var(--ink3);border-bottom:1px solid var(--line)}
th{text-align:left;font-size:11.5px;letter-spacing:.07em;text-transform:uppercase;color:var(--ink3);font-weight:650;background:#f9fafb;padding:9px 14px;border-bottom:1px solid var(--line)}
td{padding:9px 14px;border-bottom:1px solid #f2f4f7;color:var(--ink2);vertical-align:top}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover td{background:#fcfcfd}

.codeblock{border:1px solid var(--line);border-radius:8px;overflow:hidden;background:var(--surface)}
.codecap{font-family:var(--mono);font-size:12px;color:var(--ink3);padding:8px 14px;border-bottom:1px solid var(--line);background:#f9fafb}
pre{margin:0;padding:14px 16px;overflow-x:auto}
pre code{font-family:var(--mono);font-size:12.8px;line-height:1.6;color:#1d2939}

/* ---- 流程图 ---- */
.flow{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:18px 20px}
.fnode{
  display:flex;align-items:center;gap:11px;flex-wrap:wrap;
  background:var(--tb);background:color-mix(in srgb,var(--tc) 7%,#fff);
  border:1px solid color-mix(in srgb,var(--tc) 26%,transparent);
  border-left:4px solid var(--tc);border-radius:7px;padding:11px 14px;
}
.fidx{
  flex:0 0 auto;width:21px;height:21px;border-radius:50%;background:var(--tc);color:#fff;
  font-size:11.5px;font-weight:700;display:grid;place-items:center;font-variant-numeric:tabular-nums;
}
.ftitle{font-size:14.5px;font-weight:600;color:var(--ink)}
.fnote{font-size:12.5px;color:var(--ink2);font-family:var(--mono)}
.ftag{
  font-size:10.5px;font-weight:650;color:var(--tc);letter-spacing:.06em;text-transform:uppercase;
  border:1px solid color-mix(in srgb,var(--tc) 24%,transparent);border-radius:4px;padding:1px 6px;
}
.fvia{height:26px;margin-left:21px;border-left:2px dashed var(--line2);position:relative}
.fvia::after{
  content:"";position:absolute;left:-5.5px;bottom:0;
  border-left:6px solid transparent;border-right:6px solid transparent;
  border-top:7px solid #98a2b3;
}
.fvia span{
  position:absolute;left:9px;top:50%;transform:translateY(-50%);
  font-size:11.5px;color:var(--ink3);background:var(--surface);padding:0 6px;white-space:nowrap;
}
.fvia-bare span{display:none}

footer{margin:52px 0 0;padding-top:18px;border-top:1px solid var(--line2);font-size:12px;color:var(--ink3);display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap}

@media (max-width:640px){
  body{padding:0 14px 48px}
  h1{font-size:23px}
  .ftag{margin-left:0;width:100%}
}
@media print{
  body{background:#fff;padding:0}
  nav.tabs{display:none}
  .panel{display:block!important;break-inside:avoid;page-break-inside:avoid}
  .panel::before{content:attr(data-label);display:block;font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:#667085;margin:26px 0 8px;font-weight:650}
  .card,.callout,.flow,table{break-inside:avoid}
}
"""

JS = """
(function(){
  var nav=document.querySelector('nav.tabs');
  var btns=[].slice.call(nav.querySelectorAll('button'));
  var panels=[].slice.call(document.querySelectorAll('.panel'));
  function show(id){
    btns.forEach(function(b){b.setAttribute('aria-selected', b.dataset.tab===id?'true':'false');});
    panels.forEach(function(p){p.dataset.active = p.id===('p-'+id) ? 'true':'false';});
    if(history.replaceState){history.replaceState(null,'','#'+id);}
  }
  btns.forEach(function(b){
    b.addEventListener('click',function(){show(b.dataset.tab);});
  });
  var want=location.hash.slice(1);
  var ok=btns.some(function(b){return b.dataset.tab===want;});
  show(ok?want:btns[0].dataset.tab);
})();
"""


def render(spec: dict) -> str:
    spec = validate(spec)
    title = esc(spec["title"])
    subtitle = esc(spec.get("subtitle", ""))
    eyebrow = esc(spec.get("eyebrow", "CODE DISCIPLINE REPORT"))
    verdict = spec.get("verdict", "warn")
    vlabel, vcolor, vbg = VERDICT_META[verdict]
    summary = spec.get("summary")

    metrics_html = ""
    if spec.get("metrics"):
        cards = []
        for m in spec["metrics"]:
            _, color, _ = TONE_META[_tone(m.get("tone"), "metric")]
            cards.append(
                f'<div class="metric" style="--mc:{color}">'
                f'<b>{esc(m["value"])}</b><span>{esc(m["label"])}</span></div>'
            )
        metrics_html = f'<div class="metrics">{"".join(cards)}</div>'

    nav_btns = []
    panels = []
    for ti, tab in enumerate(spec["tabs"]):
        tid = tab.get("id") or f"tab{ti}"
        count = tab.get("count")
        em = f"<em>{esc(count)}</em>" if count not in (None, "") else ""
        nav_btns.append(
            f'<button role="tab" data-tab="{esc(tid)}" aria-controls="p-{esc(tid)}">'
            f'{esc(tab["label"])}{em}</button>'
        )
        body = "".join(render_block(b) for b in tab["blocks"])
        panels.append(
            f'<section class="panel" id="p-{esc(tid)}" role="tabpanel" '
            f'data-label="{esc(tab["label"])}">{body}</section>'
        )

    summary_html = f'<p class="summary">{esc(summary)}</p>' if summary else ""
    footer_note = esc(spec.get("footer", ""))
    generated = esc(spec.get("generated", ""))

    return f"""<!DOCTYPE html>
<html lang="{esc(spec.get("lang", "zh-CN"))}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div class="eyebrow">{eyebrow}</div>
    <h1>{title}</h1>
    <p class="sub">{subtitle}</p>
    <div class="verdict" style="--vc:{vcolor};--vb:{vbg}">{esc(vlabel)}</div>
    {summary_html}
    {metrics_html}
  </header>

  <nav class="tabs" role="tablist">{"".join(nav_btns)}</nav>
  {"".join(panels)}

  <footer><span>{footer_note}</span><span>{generated}</span></footer>
</div>
<script>{JS}</script>
</body>
</html>
"""


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="把检查结果渲染成单文件 HTML 报告")
    p.add_argument("--spec", required=True, help="JSON spec 路径")
    p.add_argument("--out", help="输出 HTML 路径；默认与 spec 同名 .html，放在 spec 旁边")
    p.add_argument("--open", dest="open_it", action="store_true", help="生成后直接在浏览器打开")
    p.add_argument("--check", action="store_true", help="只校验 spec，不生成文件")
    args = p.parse_args(argv)

    spec_path = Path(args.spec).expanduser().resolve()
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"找不到 spec：{spec_path}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"spec 不是合法 JSON：{exc}", file=sys.stderr)
        return 2

    try:
        validate(spec)
    except SpecError as exc:
        print(f"spec 不合法：{exc}", file=sys.stderr)
        return 2

    if args.check:
        tabs = len(spec["tabs"])
        blocks = sum(len(t["blocks"]) for t in spec["tabs"])
        print(f"OK  {tabs} 个标签页 / {blocks} 个区块  {spec_path}")
        return 0

    out_path = Path(args.out).expanduser().resolve() if args.out else spec_path.with_suffix(".html")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render(spec), encoding="utf-8")
    size_kb = out_path.stat().st_size / 1024
    print(f"已生成 {out_path}  ({size_kb:.1f} KB)")

    if args.open_it:
        open_in_browser(out_path)
    return 0


def open_in_browser(path: Path) -> None:
    """优先交给系统默认浏览器；失败时退回 webbrowser 模块。"""
    url = path.as_uri()
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # noqa: S606 - 打开本地 HTML 是预期行为
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return
    except Exception:
        pass
    try:
        webbrowser.open(url)
    except Exception as exc:
        print(f"自动打开失败，请手动打开：{path}  ({exc})", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
