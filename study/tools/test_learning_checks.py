"""Regression tests for promises made by a portable source-based handbook."""

import tempfile
import unittest
from pathlib import Path

from learning_checks import validate_diagram, validate_links, validate_source_excerpt


class LearningChecksTests(unittest.TestCase):
    def test_diagram_requires_resolvable_arrow_and_accessible_name(self):
        svg = '<svg viewBox="0 0 900 300" role="img" aria-labelledby="title"><title id="title">Flow</title><desc>Data flow</desc><path marker-end="url(#missing)"/></svg>'
        self.assertTrue(any("missing SVG reference" in e for e in validate_diagram(svg)))
        self.assertTrue(validate_diagram('<svg viewBox="0 0 900 300"/>'))

    def test_diagram_detects_nodes_outside_viewbox(self):
        svg = '<svg viewBox="0 0 100 100" role="img" aria-labelledby="title"><title id="title">Flow</title><desc>Data flow</desc><rect x="90" y="10" width="40" height="20"/></svg>'
        self.assertTrue(any("outside viewBox" in e for e in validate_diagram(svg)))

    def test_local_source_link_is_rejected_even_when_source_exists(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "study"
            root.mkdir()
            (base / "runner.py").write_text("pass", encoding="utf-8")
            errors = validate_links(root, root / "index.html", ["../runner.py"], {})
            self.assertTrue(any("outside study" in error for error in errors))

    def test_cross_page_fragment_must_exist(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            page = root / "lesson.html"
            page.write_text('<h2 id="loop">Loop</h2>', encoding="utf-8")
            ids = {page.resolve(): {"loop"}}
            self.assertEqual(validate_links(root, root / "index.html", ["lesson.html#loop"], ids), [])
            self.assertTrue(validate_links(root, root / "index.html", ["lesson.html#missing"], ids))

    def test_same_page_fragment_is_checked(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            page = root / "index.html"
            page.write_text("", encoding="utf-8")
            self.assertTrue(validate_links(root, page, ["#missing"], {page.resolve(): set()}))

    def test_excerpt_detects_source_drift(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "module.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
            attrs = {"data-source": "module.py", "data-start": "1", "data-end": "2"}
            self.assertEqual(validate_source_excerpt(root, attrs, "def answer():\n    return 42"), [])
            self.assertTrue(validate_source_excerpt(root, attrs, "def answer():\n    return 43"))

    def test_excerpt_cannot_read_outside_repository_or_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("../secret.py", "api.txt"):
                attrs = {"data-source": name, "data-start": "1", "data-end": "1"}
                self.assertTrue(validate_source_excerpt(root, attrs, ""))


if __name__ == "__main__":
    unittest.main()
