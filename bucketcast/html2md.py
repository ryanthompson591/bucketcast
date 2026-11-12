"""Convert show-notes HTML to Markdown.

Other podcast hosts store episode notes as HTML; Bucketcast stores Markdown.
The importer uses this to bring notes across. It handles what show notes
actually contain (paragraphs, line breaks, links, images, bold, italic,
headings, lists, quotes) and keeps the text of anything else.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from html.parser import HTMLParser

_ESCAPE_RE = re.compile(r"([\\`*_\[\]<])")
_LINE_START_RE = re.compile(r"^(\s*)([#>+-]|\d+[.)])(?=\s|$)")
_SPACE_RE = re.compile(r"[ \t\r\n\f\v]+")
_SKIP = {"script", "style", "head", "title", "iframe", "noscript", "template"}
_BLOCKS = {"p", "div", "section", "article", "blockquote", "ul", "ol", "li", "table", "tr",
           "h1", "h2", "h3", "h4", "h5", "h6", "pre", "figure", "figcaption", "hr"}
_HEADINGS = {f"h{n}": "#" * n for n in range(1, 7)}
_BOLD, _ITALIC = {"strong", "b"}, {"em", "i"}


def _escape(text: str) -> str:
    return _ESCAPE_RE.sub(r"\\\1", text)


_LIST_ITEM_RE = re.compile(r"^(?:> )*\s*(?:\d+\.|-) ")


def _escape_line_start(line: str) -> str:
    """Escape a leading ``#``, ``>``, ``-``, ``+`` or ``1.`` so it stays plain text."""
    m = _LINE_START_RE.match(line)
    if not m:
        return line
    marker = m.group(2)
    if marker[0].isdigit():
        escaped = marker[:-1] + "\\" + marker[-1]  # 1. -> 1\.
    else:
        escaped = "\\" + marker
    return m.group(1) + escaped + line[m.end():]


def _clean_url(url: str) -> str:
    return (url or "").strip().replace(" ", "%20").replace("(", "%28").replace(")", "%29")


class _Converter(HTMLParser):
    def __init__(self, image_url: Callable[[str], str] | None):
        super().__init__(convert_charrefs=True)
        self.image_url = image_url
        self.blocks: list[str] = []
        self.buf: list[str] = []
        self.skip = 0
        self.lists: list[list] = []  # [kind, counter] for each open list
        self.quote = 0
        self.prefix = ""  # set when a heading or list item opens
        self.links: list[str] = []
        self.pre = 0

    # --- block handling --------------------------------------------------------

    def _flush(self) -> None:
        text = "".join(self.buf)
        self.buf = []
        if not self.pre:
            text = "\n".join(line.strip() for line in text.split("\n"))
        text = text.strip("\n ")
        prefix, self.prefix = self.prefix, ""
        if not text.strip():
            return
        if not prefix:
            # Stop ordinary text from being read as a heading, quote or list.
            text = "\n".join(_escape_line_start(line) for line in text.split("\n"))
        indent = " " * len(prefix)
        lines = text.split("\n")
        block = "\n".join([prefix + lines[0]] + [indent + line for line in lines[1:]])
        if self.quote:
            block = "\n".join("> " * self.quote + line for line in block.split("\n"))
        is_item = bool(prefix) and not prefix.startswith("#")
        if is_item and self.blocks and _LIST_ITEM_RE.match(self.blocks[-1]):
            self.blocks[-1] += "\n" + block  # keep list items together
        else:
            self.blocks.append(block)

    # --- parser callbacks ------------------------------------------------------

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in _SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        if tag in _BLOCKS:
            self._flush()
        if tag in _HEADINGS:
            self.prefix = _HEADINGS[tag] + " "
        elif tag in ("ul", "ol"):
            self.lists.append([tag, 0])
        elif tag == "li":
            depth = max(len(self.lists) - 1, 0)
            if self.lists and self.lists[-1][0] == "ol":
                self.lists[-1][1] += 1
                self.prefix = "   " * depth + f"{self.lists[-1][1]}. "
            else:
                self.prefix = "  " * depth + "- "
        elif tag == "blockquote":
            self.quote += 1
        elif tag == "pre":
            self.pre += 1
        elif tag == "hr":
            self.blocks.append("---")
        elif tag == "br":
            self.buf.append("\n")
        elif tag in _BOLD:
            self.buf.append("**")
        elif tag in _ITALIC:
            self.buf.append("*")
        elif tag == "a":
            href = _clean_url(a.get("href") or "")
            self.links.append(href)
            if href:
                self.buf.append("[")
        elif tag == "img":
            src = _clean_url(a.get("src") or "")
            if src:
                if self.image_url:
                    src = self.image_url(src) or src
                alt = _escape(_SPACE_RE.sub(" ", a.get("alt") or "").strip())
                self.buf.append(f"![{alt}]({src})")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in _BOLD:
            self._close_inline("**")
        elif tag in _ITALIC:
            self._close_inline("*")
        elif tag == "a":
            href = self.links.pop() if self.links else ""
            if href:
                self._close_link(href)
        if tag in _BLOCKS:
            self._flush()
        if tag in ("ul", "ol") and self.lists:
            self.lists.pop()
        elif tag == "blockquote":
            self.quote = max(0, self.quote - 1)
        elif tag == "pre":
            self.pre = max(0, self.pre - 1)

    def handle_data(self, data):
        if self.skip:
            return
        if self.pre:
            self.buf.append(data)
            return
        text = _SPACE_RE.sub(" ", data.replace("\xa0", " "))
        if text == " " and (not self.buf or self.buf[-1].endswith((" ", "\n"))):
            return
        self.buf.append(_escape(text))

    # --- inline helpers --------------------------------------------------------

    def _close_inline(self, marker: str) -> None:
        """Close ``**`` or ``*``, dropping empty emphasis and moving spaces outside."""
        for i in range(len(self.buf) - 1, -1, -1):
            if self.buf[i] == marker:
                inner = "".join(self.buf[i + 1:])
                del self.buf[i:]
                if inner.strip():
                    lead = " " if inner[:1] in (" ", "\n") else ""
                    trail = " " if inner[-1:] in (" ", "\n") else ""
                    self.buf.append(f"{lead}{marker}{inner.strip()}{marker}{trail}")
                else:
                    self.buf.append(inner)
                return

    def _close_link(self, href: str) -> None:
        for i in range(len(self.buf) - 1, -1, -1):
            if self.buf[i] == "[":
                label = "".join(self.buf[i + 1:]).strip()
                del self.buf[i:]
                plain = label.replace("\\", "")
                if not label:
                    return
                if plain == href:
                    self.buf.append(f"<{href}>")  # the link text is just the address
                else:
                    self.buf.append(f"[{label}]({href})")
                return


def html_to_markdown(html: str, image_url: Callable[[str], str] | None = None) -> str:
    """Convert notes HTML to Markdown.

    ``image_url`` can rewrite each image address, for example to an
    ``asset:name.jpg`` reference once the image has been copied in.
    """
    converter = _Converter(image_url)
    converter.feed(html or "")
    converter.close()
    converter._flush()
    return "\n\n".join(b for b in converter.blocks if b.strip()).strip()
