"""Small text helpers shared by the mirrors: HTML to text, capping, cleanup."""

from __future__ import annotations

import re
from html.parser import HTMLParser

_BLOCK_TAGS = {
    "p", "div", "br", "tr", "li", "ul", "ol", "table", "blockquote", "pre",
    "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "header", "footer",
}
_SKIP_TAGS = {"script", "style", "head", "title", "noscript", "template"}

_BLANK_LINES = re.compile(r"\n{3,}")
_SPACES = re.compile(r"[ \t ​‌‍﻿]+")
# Google Docs' markdown export inlines images as base64 reference definitions.
_DATA_URI_DEFINITION = re.compile(r"^\[[^\]\n]+\]:\s*<?data:[^\n]*$", re.MULTILINE)
_DATA_URI_INLINE = re.compile(r"data:[\w.+-]+/[\w.+-]+;base64,[A-Za-z0-9+/=]+")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.chunks.append(data)


def html_to_text(html: str) -> str:
    """Strip tags from ``html`` and return readable plain text."""
    if not html:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # malformed markup: fall back to a crude tag strip
        return tidy(re.sub(r"<[^>]+>", " ", html))
    return tidy("".join(parser.chunks))


def tidy(text: str) -> str:
    """Normalise whitespace: no trailing spaces, at most one blank line."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_SPACES.sub(" ", line).strip() for line in text.split("\n")]
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def cap(text: str, limit: int) -> tuple[str, bool]:
    """Return ``text`` cut to ``limit`` characters and whether it was cut."""
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def clip(text: str, limit: int) -> str:
    """The start of ``text`` as one line, cut at a word and marked with ... when it was longer than ``limit``."""
    line = " ".join(text.split())
    if len(line) <= limit:
        return line
    return line[:limit].rsplit(" ", 1)[0].rstrip(",.;:") + "..."


def strip_data_uris(markdown: str) -> str:
    """Remove embedded base64 images from exported markdown."""
    markdown = _DATA_URI_DEFINITION.sub("", markdown)
    return _DATA_URI_INLINE.sub("", markdown)
