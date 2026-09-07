"""Validate the static nanobot study handbook without third-party dependencies."""

from __future__ import annotations

import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from learning_checks import check_learning
from check_interview import check_interview

STUDY_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_REVIEW_BLOCKS = 61
EXPECTED_REVIEW_QUESTIONS = 211
REVIEW_HEADING_MARKERS = ("自测", "能回答", "能解释", "本章要解决")
SPECIAL_REVIEW_PAGES = {
    Path("chapters/09-slash-command-system/07-channels-extension.html"),
}


class PageParser(HTMLParser):
    def __init__(self, relative: Path) -> None:
        super().__init__()
        self.links: list[str] = []
        self.ids: list[str] = []
        self.body_attrs: dict[str, str | None] = {}
        self.review_blocks = 0
        self.review_questions: list[tuple[int, str]] = []
        self.open_answer_reveals = 0
        self._relative = relative
        self._in_h2 = False
        self._h2_text: list[str] = []
        self._last_h2 = ""
        self._ul_depth = 0
        self._review_ul_depth: int | None = None
        self._question_answer_count: int | None = None
        self._answer_content_depth = 0
        self._answer_text: list[str] = []

    def _is_review_list(self, classes: set[str]) -> bool:
        if "check-list" not in classes:
            return False
        if any(marker in self._last_h2 for marker in REVIEW_HEADING_MARKERS):
            return True
        return self._relative in SPECIAL_REVIEW_PAGES and self._last_h2 == "测试与源码地图"

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if tag in {"a", "link"} and values.get("href"):
            self.links.append(values["href"] or "")
        if tag == "script" and values.get("src"):
            self.links.append(values["src"] or "")
        if values.get("id"):
            self.ids.append(values["id"] or "")
        if tag == "body":
            self.body_attrs = values
        if tag == "h2":
            self._in_h2 = True
            self._h2_text = []
        if tag == "ul":
            self._ul_depth += 1
            if self._review_ul_depth is None and self._is_review_list(classes):
                self._review_ul_depth = self._ul_depth
                self.review_blocks += 1
        if (
            tag == "li"
            and self._review_ul_depth is not None
            and self._ul_depth == self._review_ul_depth
        ):
            self._question_answer_count = 0
            self._answer_text = []
        if self._question_answer_count is not None and tag == "details" and "answer-reveal" in classes:
            self._question_answer_count += 1
            if "open" in values:
                self.open_answer_reveals += 1
        if self._answer_content_depth:
            self._answer_content_depth += 1
        elif self._question_answer_count is not None and tag == "div" and "answer-content" in classes:
            self._answer_content_depth = 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "h2" and self._in_h2:
            self._in_h2 = False
            self._last_h2 = "".join(self._h2_text).strip()
        if self._answer_content_depth:
            self._answer_content_depth -= 1
        if (
            tag == "li"
            and self._question_answer_count is not None
            and self._review_ul_depth is not None
            and self._ul_depth == self._review_ul_depth
        ):
            self.review_questions.append(
                (self._question_answer_count, " ".join(self._answer_text).strip())
            )
            self._question_answer_count = None
            self._answer_text = []
            self._answer_content_depth = 0
        if tag == "ul":
            if self._review_ul_depth == self._ul_depth:
                self._review_ul_depth = None
            self._ul_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_h2:
            self._h2_text.append(data)
        if self._answer_content_depth:
            self._answer_text.append(data)


def local_target(page: Path, link: str) -> Path | None:
    parsed = urlsplit(link)
    if parsed.scheme or parsed.netloc or link.startswith("#"):
        return None
    return (page.parent / unquote(parsed.path)).resolve()


def main() -> int:
    errors: list[str] = check_learning(STUDY_ROOT, check_source="--check-source" in sys.argv)
    errors.extend(check_interview(STUDY_ROOT))
    review_blocks = 0
    review_questions = 0
    pages = sorted(STUDY_ROOT.rglob("*.html"))
    if not pages:
        errors.append("No HTML pages found")

    for page in pages:
        relative = page.relative_to(STUDY_ROOT)
        parser = PageParser(relative)
        parser.feed(page.read_text(encoding="utf-8"))
        review_blocks += parser.review_blocks
        review_questions += len(parser.review_questions)

        duplicates = sorted({item for item in parser.ids if parser.ids.count(item) > 1})
        if duplicates:
            errors.append(f"{relative}: duplicate id(s): {', '.join(duplicates)}")

        if relative != Path("index.html"):
            for required in ("data-study-root", "data-chapter", "data-page"):
                if required not in parser.body_attrs:
                    errors.append(f"{relative}: missing body attribute {required}")

        for link in parser.links:
            target = local_target(page, link)
            if target is not None and not target.exists():
                errors.append(f"{relative}: broken link {link}")

        for number, (answer_count, answer_text) in enumerate(parser.review_questions, start=1):
            if answer_count != 1:
                errors.append(
                    f"{relative}: review question {number} has {answer_count} answer reveal(s)"
                )
            elif not answer_text:
                errors.append(f"{relative}: review question {number} has an empty answer")
        if parser.open_answer_reveals:
            errors.append(
                f"{relative}: {parser.open_answer_reveals} answer reveal(s) are open by default"
            )

    if review_blocks != EXPECTED_REVIEW_BLOCKS:
        errors.append(
            f"review block count: expected {EXPECTED_REVIEW_BLOCKS}, found {review_blocks}"
        )
    if review_questions != EXPECTED_REVIEW_QUESTIONS:
        errors.append(
            f"review question count: expected {EXPECTED_REVIEW_QUESTIONS}, found {review_questions}"
        )

    if errors:
        print("Study handbook validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    print(
        "Study handbook validation passed: "
        f"{len(pages)} HTML pages, {review_blocks} review blocks, "
        f"{review_questions} original answered questions + 40 interview cards checked"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
