"""Build the offline interview reader from its Markdown question sources."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
STUDY = ROOT.parent / "study"
LABELS = ("面试问题", "考察意图", "参考答案要点", "进阶追问（Follow-up）")
CATEGORIES = [
    ("业务理解与项目边界", "先讲清用户价值、业务闭环和个人贡献。", ["P1", "N1", "N2", "N3", "D1"], [1, 1, 1, 1, 2]),
    ("架构规划与任务编排", "从入口路由走向规划、分工与依赖执行。", ["N4", "P2", "N5", "N6", "T4"], [2, 2, 2, 3, 3]),
    ("定时调度与任务状态", "从创建提醒走向时间语义、补偿与一致性。", ["T1", "N7", "N8", "P3", "R4"], [2, 2, 3, 3, 3]),
    ("工具调用与邮件处理", "从结构化抽取走向契约演进和执行安全。", ["T2", "N9", "T3", "N10", "T5"], [2, 3, 3, 3, 3]),
    ("长期记忆与上下文", "从记忆分层走向检索、写入、隔离与预算。", ["P4", "P5", "N11", "N12", "R2"], [2, 3, 3, 3, 3]),
    ("运行可靠性与性能", "处理无进展、幻觉、取消、成本和服务故障。", ["R1", "R3", "N13", "R5", "N14"], [3, 3, 3, 4, 4]),
    ("评测、追踪与监控", "从故障定位走向实验、状态一致性与告警。", ["E1", "E2", "E3", "E4", "E5"], [3, 3, 3, 4, 4]),
    ("技术深挖与压力追问", "用生命周期、恢复、事故和规模推演核验深度。", ["D2", "N15", "D3", "D4", "D5"], [4, 4, 4, 4, 4]),
]
DIFFICULTIES = {1: "基础", 2: "进阶", 3: "深入", 4: "压力"}


def inline(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)


def markdown(text: str) -> str:
    """Render the limited, trusted Markdown used by this document collection."""
    output = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.splitlines()
        if not lines:
            continue
        if len(lines) > 1 and lines[0].startswith("|") and re.match(r"\|[- :|]+\|$", lines[1]):
            rows = [[inline(cell.strip()) for cell in line.strip("|").split("|")] for line in lines]
            output.append('<div class="table-scroll"><table><thead><tr>' + "".join(f"<th>{c}</th>" for c in rows[0]) + "</tr></thead><tbody>" + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows[2:]) + "</tbody></table></div>")
        elif re.match(r"^#{1,3} ", lines[0]):
            title = re.sub(r"^#+ ", "", lines[0])
            output.append(f"<h3>{inline(title)}</h3>")
            if len(lines) > 1:
                output.append(f'<p>{inline(" ".join(lines[1:]))}</p>')
        elif all(re.match(r"^\d+\. ", line) for line in lines):
            items = [re.sub(r"^\d+\. ", "", line) for line in lines]
            output.append('<ol class="answer-steps">' + "".join(f"<li>{inline(item)}</li>" for item in items) + "</ol>")
        elif all(line.startswith("- ") for line in lines):
            output.append("<ul>" + "".join(f"<li>{inline(line[2:])}</li>" for line in lines) + "</ul>")
        else:
            output.append(f'<p>{inline(" ".join(lines))}</p>')
    return "\n".join(output)


def load_questions() -> dict:
    questions = {}
    sources = sorted(ROOT.glob("0[1-5]-*.md")) + [ROOT / "07-additional-questions.md"]
    for source in sources:
        for block in re.split(r"^## ", source.read_text(encoding="utf-8"), flags=re.M)[1:]:
            heading, body = block.split("\n", 1)
            key, title = heading.split("｜", 1)
            parts = re.split(r"^### 【([^】]+)】\s*\n", body, flags=re.M)
            assert parts[1::2] == list(LABELS), (source, key, "invalid sections")
            sections = dict(zip(parts[1::2], (part.strip() for part in parts[2::2])))
            assert "简历入口" in sections[LABELS[0]], key
            assert len(re.findall(r"^- ", sections[LABELS[3]], re.M)) == 2, key
            answer = sections[LABELS[2]]
            positions = [answer.index(layer) for layer in ("业务目标与模块定位", "架构拓扑与交互流转", "核心机制")]
            assert positions == sorted(positions), key
            assert key not in questions, key
            questions[key] = {"title": title, "sections": sections}
    ids = [key for _, _, keys, _ in CATEGORIES for key in keys]
    assert len(ids) == len(set(ids)) == len(questions) == 40
    assert set(ids) == set(questions)
    return questions


def load_study_links(questions: dict) -> dict:
    mappings = json.loads((ROOT / "study-links.json").read_text(encoding="utf-8"))
    assert mappings.keys() == questions.keys(), "Each question needs a reading mapping"
    for key, mapping in mappings.items():
        assert 2 <= len(mapping["links"]) <= 3, key
        for href, label in mapping["links"]:
            parsed = urlsplit(href)
            target = (STUDY / parsed.path).resolve()
            assert not parsed.scheme and not parsed.netloc, (key, href)
            assert target.is_relative_to(STUDY.resolve()) and target.is_file(), (key, href)
            assert label.strip(), key
            if parsed.fragment:
                assert f'id="{parsed.fragment}"' in target.read_text(encoding="utf-8"), (key, href)
    return mappings


def reading_links(mapping: dict, number: str) -> str:
    links = []
    for href, label in mapping["links"]:
        path, separator, fragment = href.partition("#")
        target = f'{{{{STUDY_ROOT}}}}/{path}?fromInterview={number.lower()}'
        if separator:
            target += f"#{fragment}"
        links.append(f'<a href="{html.escape(target, quote=True)}">{html.escape(label)} <span aria-hidden="true">↗</span></a>')
    note = f'<p class="reading-note">{html.escape(mapping["note"])}</p>' if mapping.get("note") else ""
    return f'<aside class="related-reading" aria-label="第 {int(number[1:])} 题相关模块讲解"><h4>相关模块讲解</h4><div class="reading-links">{"".join(links)}</div>{note}</aside>'


def build() -> None:
    questions = load_questions()
    mappings = load_study_links(questions)
    nav = []
    groups = []
    sequence = 0
    for index, (name, description, keys, levels) in enumerate(CATEGORIES, 1):
        category = f"category-{index}"
        nav.append(f'<button class="category-button" data-category="{category}" aria-pressed="false"><span class="nav-number">{index:02}</span><span>{name}</span><span class="nav-count">5</span></button>')
        cards = []
        for key, level in zip(keys, levels):
            sequence += 1
            q = questions[key]
            sections = q["sections"]
            question = sections[LABELS[0]]
            match = re.match(r"简历入口 ([^。]+)。\s*", question)
            assert match, key
            basis = match.group(1)
            question = question[match.end():]
            new = '<span class="badge new">新增</span>' if key.startswith("N") else ""
            number = f"Q{sequence:02}"
            cards.append(f'''<article class="question-card" id="{number.lower()}" data-category="{category}" data-level="{level}" data-question-source="{key}">
<div class="question-meta"><a class="question-number" href="#{number.lower()}" aria-label="定位第 {sequence} 题">{number}</a><span class="badge level-{level}">{DIFFICULTIES[level]}</span>{new}<span class="resume-source">简历依据 {html.escape(basis)}</span></div>
<h3>{html.escape(q['title'])}</h3><div class="question-prompt"><span class="eyebrow">面试问题</span><p>{inline(question)}</p></div>
{reading_links(mappings[key], number)}
<details class="answer"><summary><span>参考答案与追问</span><span class="toggle-hint"><span class="closed-text">点击展开</span><span class="open-text">收起答案</span><span class="chevron" aria-hidden="true">⌄</span></span></summary>
<div class="answer-body"><section class="intent"><h4>考察意图</h4>{markdown(sections[LABELS[1]])}</section><section><h4>参考答案要点</h4>{markdown(sections[LABELS[2]])}</section><section class="followups"><h4>进阶追问 <span>Follow-up</span></h4>{markdown(sections[LABELS[3]])}</section></div>
</details></article>''')
        groups.append(f'<section class="question-group" id="{category}"><header class="group-header"><div><span class="eyebrow">CHAPTER {index:02} / 08</span><h2>{name}</h2><p>{description}</p></div><span class="group-count">5 题</span></header>{"".join(cards)}</section>')
    template = (ROOT / "reader.template.html").read_text(encoding="utf-8")
    evidence = (ROOT / "06-evidence-and-gaps.md").read_text(encoding="utf-8")
    rendered = template.replace("{{NAVIGATION}}", "\n".join(nav)).replace("{{QUESTIONS}}", "\n".join(groups)).replace("{{EVIDENCE}}", markdown(evidence)).replace("{{STUDY_ROOT}}", "../study")
    assert "{{" not in rendered
    (ROOT / "index.html").write_text(rendered, encoding="utf-8")
    study_template = (ROOT / "study-reader.template.html").read_text(encoding="utf-8")
    intro = re.search(r'<details class="intro-details">.*?</details>', template, re.S).group(0)
    script = re.search(r"<script>\n(.*?)</script>", template, re.S).group(1)
    # The portable handbook keeps provenance labels, not repository file links.
    study_evidence = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", evidence)
    study_page = study_template.replace("{{NAVIGATION}}", "\n".join(nav)).replace("{{QUESTIONS}}", "\n".join(groups)).replace("{{INTRO}}", intro).replace("{{EVIDENCE}}", markdown(study_evidence)).replace("{{SCRIPT}}", script).replace("{{STUDY_ROOT}}", "../..")
    assert "{{" not in study_page
    destination = STUDY / "chapters/11-interview/index.html"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(study_page, encoding="utf-8")
    import runpy
    runpy.run_path(str(STUDY / "tools/build_reading_navigation.py"), run_name="__main__")
    count = sum(len(mapping["links"]) for mapping in mappings.values())
    print(f"Built standalone reader and study Chapter 11: {sequence} questions, {count} reading links, 80 follow-ups.")


if __name__ == "__main__":
    build()
