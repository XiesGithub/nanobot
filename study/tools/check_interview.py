"""Validate interview cards and their in-handbook reading links, offline."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit


class InterviewParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.cards = []
        self.groups = 0
        self.current = None
        self.in_reading = False

    def handle_starttag(self, tag, pairs):
        attrs = dict(pairs)
        classes = set(attrs.get("class", "").split())
        self.groups += int("question-group" in classes)
        if tag == "article" and "question-card" in classes:
            self.current = {"id": attrs.get("id"), "answers": 0, "open": False, "links": [], "parts": set()}
            self.cards.append(self.current)
        if self.current is None:
            return
        self.current["parts"].update(classes & {"question-prompt", "intent", "answer-steps", "followups"})
        if tag == "details" and "answer" in classes:
            self.current["answers"] += 1
            self.current["open"] = "open" in attrs
        if tag == "aside" and "related-reading" in classes:
            self.in_reading = True
        if tag == "a" and self.in_reading:
            self.current["links"].append(attrs.get("href", ""))

    def handle_endtag(self, tag):
        if tag == "aside":
            self.in_reading = False
        if tag == "article":
            self.current = None


class AnchorParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()

    def handle_starttag(self, tag, pairs):
        attrs = dict(pairs)
        if attrs.get("id"):
            self.ids.add(attrs["id"])


def check_interview(root: Path) -> list[str]:
    root = root.resolve()
    page = root / "chapters/11-interview/index.html"
    if not page.is_file():
        return ["interview: Chapter 11 page missing"]
    parser = InterviewParser()
    parser.feed(page.read_text(encoding="utf-8"))
    errors = []
    expected = [f"q{i:02}" for i in range(1, 41)]
    if [card["id"] for card in parser.cards] != expected:
        errors.append("interview: expected 40 unique cards ordered q01 through q40")
    if parser.groups != 8:
        errors.append("interview: expected 8 question groups")
    anchors = {}
    for card in parser.cards:
        key = card["id"]
        if card["answers"] != 1 or card["open"]:
            errors.append(f"interview {key}: expected one initially collapsed answer")
        if card["parts"] != {"question-prompt", "intent", "answer-steps", "followups"}:
            errors.append(f"interview {key}: missing required question or answer sections")
        if not 2 <= len(card["links"]) <= 3:
            errors.append(f"interview {key}: expected 2-3 related module links")
        if len(card["links"]) != len(set(card["links"])):
            errors.append(f"interview {key}: duplicate reading link")
        for href in card["links"]:
            parsed = urlsplit(href)
            target = (page.parent / unquote(parsed.path)).resolve()
            if parsed.scheme or parsed.netloc or not target.is_relative_to(root):
                errors.append(f"interview {key}: reading link leaves handbook: {href}")
                continue
            if not target.is_file() or target.suffix != ".html" or target == page:
                errors.append(f"interview {key}: reading target is not a module lesson: {href}")
                continue
            if parse_qs(parsed.query).get("fromInterview") != [key]:
                errors.append(f"interview {key}: wrong return-to-question parameter: {href}")
            if parsed.fragment:
                if target not in anchors:
                    anchor_parser = AnchorParser()
                    anchor_parser.feed(target.read_text(encoding="utf-8"))
                    anchors[target] = anchor_parser.ids
                if unquote(parsed.fragment) not in anchors[target]:
                    errors.append(f"interview {key}: missing lesson anchor: {href}")
    return errors


if __name__ == "__main__":
    issues = check_interview(Path(__file__).resolve().parents[1])
    if issues:
        print("\n".join(issues))
        raise SystemExit(1)
    print("Interview passed: 40 cards, 8 groups, collapsed answers, 2-3 local reading links per card, valid return IDs and anchors.")
