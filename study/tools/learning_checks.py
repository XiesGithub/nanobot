"""Check offline learning contracts, with optional repository provenance verification."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit


def validate_diagram(svg: str) -> list[str]:
    """Validate static SVG structure; this is not a browser rendering test."""
    try:
        tree = ET.fromstring(svg)
    except ET.ParseError as error:
        return [f"invalid SVG XML: {error}"]
    errors = []
    nodes = list(tree.iter())
    ids = {node.attrib["id"] for node in nodes if "id" in node.attrib}
    tags = {node.tag.rsplit("}", 1)[-1] for node in nodes}
    labels = tree.attrib.get("aria-labelledby", "").split()
    if tree.attrib.get("role") != "img" or not {"title", "desc"} <= tags or not labels:
        errors.append("SVG needs image role, title, description and accessible label")
    for reference in labels + re.findall(r"url\(#([^)]+)\)", svg):
        if reference not in ids:
            errors.append(f"missing SVG reference: {reference}")
    try:
        left, top, width, height = map(float, tree.attrib["viewBox"].split())
        if width <= 0 or height <= 0:
            raise ValueError("nonpositive viewBox")
        for node in nodes:
            tag = node.tag.rsplit("}", 1)[-1]
            if tag not in {"rect", "text"}:
                continue
            x, y = float(node.attrib.get("x", left)), float(node.attrib.get("y", top))
            w = float(node.attrib.get("width", 0)) if tag == "rect" else 0
            h = float(node.attrib.get("height", 0)) if tag == "rect" else 0
            if not (left <= x <= x + w <= left + width and top <= y <= y + h <= top + height):
                errors.append(f"SVG {tag} outside viewBox at {x},{y}")
    except (KeyError, ValueError):
        errors.append("SVG needs valid viewBox and numeric node coordinates")
    return errors


def validate_links(root: Path, page: Path, links: list[str], ids: dict) -> list[str]:
    errors = []
    root = root.resolve()
    for link in links:
        parsed = urlsplit(link)
        if parsed.scheme or parsed.netloc:
            continue
        target = (page.parent / unquote(parsed.path)).resolve() if parsed.path else page.resolve()
        if not target.is_relative_to(root):
            errors.append(f"{page.name}: outside study: {link}")
        elif not target.exists():
            errors.append(f"{page.name}: missing target: {link}")
        elif parsed.fragment and target.suffix == ".html":
            if unquote(parsed.fragment) not in ids.get(target, set()):
                errors.append(f"{page.name}: missing fragment: {link}")
    return errors


def validate_source_excerpt(repo: Path, attrs: dict, code: str) -> list[str]:
    name = attrs.get("data-source", "")
    target = (repo / name).resolve()
    # This validator only reads explicitly labelled source files, never credential files.
    if not target.is_relative_to(repo.resolve()) or target.suffix not in {".py", ".ts", ".md"}:
        return [f"invalid source path: {name}"]
    try:
        start, end = int(attrs["data-start"]), int(attrs["data-end"])
        lines = target.read_text(encoding="utf-8").splitlines()
    except (OSError, KeyError, ValueError):
        return [f"unreadable source or invalid range: {name}"]
    if not 1 <= start <= end <= len(lines):
        return [f"invalid source range: {name}:{start}-{end}"]
    expected = "\n".join(lines[start - 1:end]).strip("\n")
    if code.strip("\n") != expected:
        return [f"source drift: {name}:{start}-{end}"]
    return []


class LearningPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.links = []
        self.remote_assets = []
        self.excerpts = []
        self.body = {}
        self.svg_count = 0
        self._excerpt = None
        self._code = False
        self._text = []

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if attrs.get("id"):
            self.ids.add(attrs["id"])
        if tag == "body":
            self.body = attrs
        for field in ("href", "src"):
            if attrs.get(field):
                self.links.append(attrs[field])
                if tag != "a" and (urlsplit(attrs[field]).scheme or urlsplit(attrs[field]).netloc):
                    self.remote_assets.append(attrs[field])
        if tag == "svg":
            self.svg_count += 1
        if "data-source" in attrs:
            self._excerpt = attrs
        if tag == "code" and self._excerpt and not self._code:
            self._code = True
            self._text = []

    def handle_data(self, text):
        if self._code:
            self._text.append(text)

    def handle_endtag(self, tag):
        if tag == "code" and self._code:
            self.excerpts.append((self._excerpt, "".join(self._text)))
            self._excerpt = None
            self._code = False


def check_learning(root: Path, check_source: bool = False) -> list[str]:
    root = root.resolve()
    errors = []
    parsed = {}
    for page in root.rglob("*.html"):
        parser = LearningPage()
        html = page.read_text(encoding="utf-8")
        parser.feed(html)
        for svg in re.findall(r"<svg\b.*?</svg>", html, re.S):
            errors.extend(f"{page.name}: {e}" for e in validate_diagram(svg))
        parsed[page.resolve()] = parser
    ids = {page: parser.ids for page, parser in parsed.items()}
    catalog = (root / "assets/catalog.js").read_text(encoding="utf-8")
    registered = set(re.findall(r'href:\s*"([^"]+\.html)"', catalog))
    for href in registered:
        if (root / href).resolve() not in parsed:
            errors.append(f"catalog: missing page {href}")
    for page, parser in parsed.items():
        relative = page.relative_to(root).as_posix()
        errors.extend(validate_links(root, page, parser.links, ids))
        if relative != "index.html" and relative not in registered:
            errors.append(f"catalog: unregistered page {relative}")
        if parser.remote_assets:
            errors.append(f"{relative}: remote assets prevent offline reading")
        if page.name == "workshop.html":
            for anchor in ("contract", "dataflow", "source-walkthrough", "build-it"):
                if anchor not in parser.ids:
                    errors.append(f"{relative}: missing learning section {anchor}")
            if len(parser.excerpts) < 3 or not parser.svg_count:
                errors.append(f"{relative}: needs 3 source excerpts and a diagram")
        for attrs, code in parser.excerpts:
            if not all(attrs.get(key) for key in ("id", "data-start", "data-end")):
                errors.append(f"{relative}: incomplete source provenance")
            if not code.strip():
                errors.append(f"{relative}: empty excerpt")
            if check_source:
                errors.extend(f"{relative}: {e}" for e in validate_source_excerpt(root.parent, attrs, code))
    return errors
