import html
import re
from html.parser import HTMLParser

_SPACES = re.compile(r"\s+")
# Tags that break a line: they separate words; inline tags (<b>, <span>) do not.
_BLOCK = frozenset(
    [
        "br",
        "p",
        "div",
        "li",
        "ul",
        "ol",
        "tr",
        "td",
        "th",
        "table",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "section",
        "article",
        "dd",
        "dt",
        "dl",
    ]
)
_SKIP = frozenset({"script", "style"})


class _TextOnly(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0  # inside <script>/<style>

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)

    def unknown_decl(self, data: str) -> None:
        if data.startswith("CDATA[") and not self._skip:  # literal CDATA keeps its text
            self.parts.append(clean_text(data.removeprefix("CDATA[")) or "")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self._skip += 1
        elif tag in _BLOCK:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK:
            self.parts.append(" ")


def clean_text(value: str | None) -> str | None:
    """Unescape entities, drop markup, collapse whitespace (incl. nbsp). Empty -> None."""
    if value is None:
        return None
    parser = _TextOnly()
    parser.feed(value)
    parser.close()
    # Double-escaped sources (&amp;nbsp;) survive one pass, hence the second unescape.
    text = html.unescape("".join(parser.parts))
    text = _SPACES.sub(" ", text).strip()
    return text or None
