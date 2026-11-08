"""HTML 正文抽取与文本归一化。"""

from __future__ import annotations

import re
import unicodedata
from html import unescape
from html.parser import HTMLParser

# 这些标签里的内容不是正文，直接跳过
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "title"}

# 这些标签用于分隔文本，避免相邻段落粘在一起
_BLOCK_TAGS = {
    "p", "div", "section", "article", "header", "footer", "main", "aside",
    "nav", "h1", "h2", "h3", "h4", "h5", "h6", "li", "ul", "ol", "br",
    "hr", "tr", "td", "th", "table", "blockquote", "pre", "form",
}

# 页面和用户输入里的引号、破折号、零宽字符写法经常不一致，比对前统一掉
_QUOTE_MAP = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-", "\u00a0": " ", "\u00ad": "",
    "\u200b": "", "\u200c": "", "\u200d": "", "\ufeff": "",
})


class _TextExtractor(HTMLParser):
    """把 HTML 转成纯文本，并优先保留正文区域。

    优先级：<main> > [role=main] > 最长的 <article> > 全部正文。
    太短的 main 会被跳过，避免页面只渲染出 “Loading…” 时丢掉真正的正文。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._body: list[str] = []
        self._main: list[str] = []
        self._role_main: list[str] = []
        self._article_buffers: list[list[str]] = []
        self._articles: list[list[str]] = []
        self._in_main = 0
        self._in_role_main = 0
        self._role_stack: list[str] = []

    def _push(self, text: str) -> None:
        self._body.append(text)
        if self._in_main:
            self._main.append(text)
        for buffer in self._article_buffers:
            buffer.append(text)
        if self._in_role_main:
            self._role_main.append(text)

    def handle_starttag(self, tag: str, attrs) -> None:
        attrs_dict = dict(attrs)
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "main":
            self._in_main += 1
        if tag == "article":
            self._article_buffers.append([])
        if (attrs_dict.get("role") or "").lower() == "main":
            self._in_role_main += 1
            self._role_stack.append(tag)
        if tag in _BLOCK_TAGS:
            self._push(" ")

    def handle_startendtag(self, tag: str, attrs) -> None:
        if self._skip_depth == 0 and tag in _BLOCK_TAGS:
            self._push(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag in _BLOCK_TAGS:
            self._push(" ")
        if tag == "main" and self._in_main:
            self._in_main -= 1
        if tag == "article" and self._article_buffers:
            self._articles.append(self._article_buffers.pop())
        if self._role_stack and self._role_stack[-1] == tag:
            self._role_stack.pop()
            if self._in_role_main:
                self._in_role_main -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._push(data)

    def text(self) -> str:
        main_text = normalize_text("".join(self._main))
        role_text = normalize_text("".join(self._role_main))
        article_text = ""
        if self._articles:
            article_text = max(
                (normalize_text("".join(chunks)) for chunks in self._articles),
                key=len,
            )
        body_text = normalize_text("".join(self._body))
        body_length = len(body_text)

        def substantial(candidate: str) -> bool:
            if not candidate:
                return False
            if len(candidate) >= 100:
                return True
            return bool(body_length) and len(candidate) >= body_length * 0.2

        for candidate in (main_text, role_text, article_text):
            if substantial(candidate):
                return candidate

        non_empty = [text for text in (main_text, role_text, article_text, body_text) if text]
        return max(non_empty, key=len) if non_empty else ""


def normalize_text(text: str) -> str:
    """统一全角字符、引号、零宽字符和空白。"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_QUOTE_MAP)
    return re.sub(r"\s+", " ", text).strip()


def normalize_quote(quote: str) -> str:
    """归一化引用片段，规则与正文保持一致。"""
    return normalize_text(quote)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return parser.text()


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def extract_title(html: str) -> str:
    match = _TITLE_RE.search(html)
    if not match:
        return ""
    return normalize_text(unescape(match.group(1)))


def contains_quote(text: str, quote: str) -> bool:
    target = normalize_quote(quote)
    if not target:
        return False
    return target in normalize_text(text)
