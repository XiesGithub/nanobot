"""Rebuild internal reading references after editing workshop source excerpts.

Only modifies handbook HTML/catalog. Original source links are retained as metadata,
so running again resolves to updated excerpt anchors without reading raw source files.
"""

from __future__ import annotations

import re
from html import escape, unescape
from pathlib import Path
from urllib.parse import urlsplit

from learning_checks import LearningPage

ROOT = Path(__file__).resolve().parents[1]


def relative_href(page: Path, target: Path) -> str:
    import os

    return Path(os.path.relpath(target, page.parent)).as_posix()


def main():
    workshops = sorted(ROOT.glob("chapters/0*/workshop.html"))
    if len(workshops) != 9 or not (ROOT / "chapters/10-build-your-agent/index.html").exists():
        raise SystemExit("Create all 9 workshops and the capstone before integrating")
    excerpts = []
    for page in workshops:
        parser = LearningPage()
        parser.feed(page.read_text(encoding="utf-8"))
        for attrs, _ in parser.excerpts:
            guide = attrs["id"].replace("excerpt-", "source-")
            if guide in parser.ids:
                attrs.setdefault("data-guide", guide)
            excerpts.append((page, attrs))

    count = 0
    exact = 0
    for page in sorted(ROOT.rglob("*.html")):
        html = page.read_text(encoding="utf-8")

        def rewrite(match):
            nonlocal count, exact
            attrs, label = match.group(1), match.group(2)
            href_match = re.search(r'\bhref="([^"]+)"', attrs)
            if not href_match:
                return match.group(0)
            original_match = re.search(r'\bdata-original-href="([^"]+)"', attrs)
            href = unescape((original_match or href_match).group(1))
            url = urlsplit(href)
            if url.scheme or url.netloc or not url.path:
                return match.group(0)
            target = (page.parent / url.path).resolve()
            if target.is_relative_to(ROOT):
                return match.group(0)
            if not target.is_relative_to(ROOT.parent):
                raise ValueError(f"Unexpected external local target: {href}")
            source = target.relative_to(ROOT.parent).as_posix()
            original_label = re.search(r'\bdata-source-label="([^"]*)"', attrs)
            clean_label = unescape(original_label.group(1)) if original_label else re.sub("<[^>]+>", "", label)
            lines = re.search(r":(\d+)", clean_label)
            line = int(lines.group(1)) if lines else 0
            candidates = [(candidate, data) for candidate, data in excerpts if data["data-source"] == source]
            if candidates:
                def rank(candidate):
                    location, data = candidate
                    start, end = int(data["data-start"]), int(data["data-end"])
                    return (location.parent != page.parent, 0 if start <= line <= end else abs(start - line))

                destination, data = min(candidates, key=rank)
                fragment = data.get("data-guide", data["id"])
                kind = "站内摘录"
                title = f"阅读 {source}:{data['data-start']}–{data['data-end']} 的代表性摘录与讲解"
                exact += 1
            elif page.parent.name.startswith("0"):
                destination, fragment = page.parent / "workshop.html", "source-walkthrough"
                kind = "模块讲解"
                title = f"{source} 的所属模块讲解；该文件未单独逐行收录"
            else:
                destination, fragment = ROOT / "index.html", "learning-path"
                kind, title = "阅读路线", "阅读本手册的能力路线"
            new_href = f"{relative_href(page, destination)}#{fragment}"
            attrs = re.sub(r'\bhref="[^"]+"', f'href="{escape(new_href, quote=True)}"', attrs, count=1)
            for field in ("data-original-href", "data-source-label", "title"):
                attrs = re.sub(rf'\s*{field}="[^"]*"', "", attrs)
            attrs += f' data-original-href="{escape(href, quote=True)}" data-source-label="{escape(clean_label, quote=True)}" title="{escape(title, quote=True)}"'
            if source.endswith("README.md"):
                display = "模块阅读指南" if kind == "模块讲解" else "学习路线"
            else:
                display = re.sub(r":\d+(?:[-–]\d+)?", "", clean_label) + f" · {kind}"
            if 'class="button"' in attrs:
                display = "源码精讲" if kind == "站内摘录" else "模块指南"
            count += 1
            return f"<a{attrs}>{escape(display)}</a>"

        html = re.sub(r"<a\b([^>]*)>(.*?)</a>", rewrite, html, flags=re.S)
        if page.parent.name.startswith("0") and page.name != "workshop.html" and 'id="learning-bridge"' not in html:
            bridge = '''<aside class="learning-bridge" id="learning-bridge" aria-label="本章学习闭环"><p><strong>把本页概念落实为实现：</strong>对照本章真实源码与数据流，再完成设计任务。本文骨架中的省略号和简化调用用于说明流程；可运行完整项目见第10章。</p><div class="learning-links"><a class="button" href="workshop.html#contract">输入输出契约</a><a class="button" href="workshop.html#dataflow">模块数据流图</a><a class="button primary" href="workshop.html#source-walkthrough">核心源码精讲</a><a class="button" href="workshop.html#build-it">动手设计与验收</a></div></aside>'''
            # Hero is always the first section: retain the original content and insert a learning bridge.
            html = html.replace("</section>", "</section>\n" + bridge, 1)
        html = html.replace("核心源码地图</h2>", "核心源码学习地图（站内阅读）</h2>")
        html = html.replace("源码索引</h2>", "源码学习索引（站内阅读）</h2>")
        if page.parent.name[:2].isdigit():
            html = re.sub(r"(<title>)\d{2}( ·)", rf"\g<1>{page.parent.name[:2]}\g<2>", html)
        if page == ROOT / "index.html":
            def page_count(match):
                folder = ROOT / match.group(1)
                return match.group(0).replace(match.group(2), f"{len(list(folder.glob('*.html')))} 页 · 含精讲")

            html = re.sub(r'href="(chapters/0[^/]+)/index.html".*?class="chapter-state">已完成 · ([^<]+)', page_count, html)
            if 'class="chapter-number">10</div>' not in html:
                entry = '<a class="chapter-entry ready" href="chapters/10-build-your-agent/index.html"><div class="chapter-number">10</div><div><h2>从零设计并实现你的 Agent</h2><p>独立可运行项目 → 完整模块源码 → 事件轨迹 → 行为测试 → 分阶段扩展与验收。</p></div><span class="chapter-state">实作 · 1 页</span></a>'
                html = html.replace('      </div>\n\n      <h2>全书代码地图</h2>', entry + '\n      </div>\n\n      <h2>全书代码地图</h2>')
            folder_names = [p.parent.name for p in workshops]
            def map_link(match):
                label = match.group(1)
                if label.startswith(("api/", "bus/", "nanobot.py")):
                    number = 0
                elif label.startswith(("config/", "providers/")):
                    number = 1
                elif label.startswith(("session/", "agent/memory", "agent/autocompact")):
                    number = 2
                elif "context" in label:
                    number = 3
                elif label.startswith(("agent/loop", "agent/runner", "AgentLoop")):
                    number = 4
                elif "subagent" in label or "spawn" in label:
                    number = 6
                elif label.startswith(("cron/", "agent/tools/cron", "heartbeat/")):
                    number = 7
                elif label.startswith("command/"):
                    number = 8
                else:
                    number = 5
                return f'<a class="arch-item" href="chapters/{folder_names[number]}/workshop.html#source-walkthrough">{label} · 精讲</a>'
            html = re.sub(r'<span class="arch-item">([^<]+)</span>', map_link, html)
            html = html.replace("全书代码地图</h2>", '全书源码学习地图</h2><p>点击模块进入手册内的源码摘录与设计解释。文件名用于说明来源；不要求打开仓库文件。</p>')
        page.write_text(html, encoding="utf-8")

    path = ROOT / "assets/catalog.js"
    catalog = path.read_text(encoding="utf-8")

    def add_workshop(match):
        pages = match.group(2)
        first = re.search(r'href: "(chapters/0[^/]+)/', pages)
        if not first or 'id: "workshop"' in pages:
            return match.group(0)
        return match.group(1) + pages.rstrip() + ',\n        { id: "workshop", title: "源码精讲与设计实作", href: "' + first.group(1) + '/workshop.html" }' + match.group(3)

    catalog = re.sub(r'(      pages: \[\n)(.*?)(\n      \])', add_workshop, catalog, flags=re.S)
    if 'id: "build-your-agent"' not in catalog:
        addition = '''    },
    {
      id: "build-your-agent",
      number: "10",
      title: "从零设计并实现你的 Agent",
      shortTitle: "从零实现 Agent",
      description: "运行标准库教学项目，推演完整轨迹，按里程碑实现生产能力。",
      href: "chapters/10-build-your-agent/index.html",
      status: "ready",
      pages: [
        { id: "overview", title: "独立实现与扩展验收", href: "chapters/10-build-your-agent/index.html" }
      ]
    }
  ]'''
        catalog = catalog.replace("    }\n  ]", addition)
    path.write_text(catalog, encoding="utf-8")
    from build_reading_navigation import main as build_navigation
    build_navigation()
    print(f"Integrated {len(workshops)} workshops; {count} internal references, {exact} point to source excerpts")


if __name__ == "__main__":
    main()
