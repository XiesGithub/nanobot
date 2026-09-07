"""Report the handbook's learning-material baseline (no source or credentials read)."""

import json
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


class Audit(HTMLParser):
    def __init__(self):
        super().__init__()
        self.counts = Counter()
        self.links = []

    def handle_starttag(self, tag, attrs):
        self.counts[tag] += 1
        if dict(attrs).get("data-source"):
            self.counts["source_excerpt"] += 1
        if tag == "a" and dict(attrs).get("href"):
            self.links.append(dict(attrs)["href"])


def main():
    result = Counter(pages=0, code_blocks=0, svg_diagrams=0, repository_links=0,
                     source_excerpts=0)
    targets = set()
    for page in ROOT.rglob("*.html"):
        parser = Audit()
        parser.feed(page.read_text(encoding="utf-8"))
        result["pages"] += 1
        result["code_blocks"] += parser.counts["pre"]
        result["svg_diagrams"] += parser.counts["svg"]
        result["source_excerpts"] += parser.counts["source_excerpt"]
        for link in parser.links:
            parsed = urlsplit(link)
            if parsed.scheme or parsed.netloc:
                continue
            target = (page.parent / parsed.path).resolve()
            if not target.is_relative_to(ROOT):
                result["repository_links"] += 1
                targets.add(str(target))
    result["repository_targets"] = len(targets)
    print(json.dumps(dict(result), indent=2))


if __name__ == "__main__":
    main()
