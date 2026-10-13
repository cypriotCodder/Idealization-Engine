"""Turn the HTML bodies that Stack Exchange and Hacker News return into plain text."""

from html.parser import HTMLParser

_BLOCK_TAGS = frozenset(
    {"p", "br", "div", "li", "ul", "ol", "blockquote", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}
)
CODE_PLACEHOLDER = "[code]"


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._pre_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "pre":
            # code blocks are pages of VBA and formulas: noise for the filter and wasted tokens,
            # so keep a marker that code was there and drop the rest
            if self._pre_depth == 0:
                self.parts.append(f" {CODE_PLACEHOLDER} ")
            self._pre_depth += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "pre":
            self._pre_depth = max(0, self._pre_depth - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._pre_depth == 0:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(html)
    extractor.close()
    return " ".join("".join(extractor.parts).split())
