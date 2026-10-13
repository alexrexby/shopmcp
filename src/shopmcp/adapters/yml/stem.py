"""Search normalization: lower case, ё -> е, word tokens, snowball stems.

The same function feeds the index and the query, so a word form in a query meets
its stem in the index (молока -> молок <- молоко). Stemmers keep internal state,
so each thread gets its own pair.
"""

import re
import threading

import snowballstemmer

_WORD = re.compile(r"\w+")
_CYRILLIC = re.compile("[а-я]")
_local = threading.local()


def _stemmers() -> tuple[snowballstemmer.RussianStemmer, snowballstemmer.EnglishStemmer]:
    pair = getattr(_local, "pair", None)
    if pair is None:
        pair = _local.pair = (snowballstemmer.RussianStemmer(), snowballstemmer.EnglishStemmer())
    return pair


def normalize(text: str) -> str:
    return text.lower().replace("ё", "е")


def stems(text: str | None) -> list[str]:
    """Stems of every word in `text`, in order, duplicates kept."""
    if not text:
        return []
    ru, en = _stemmers()
    return [
        (ru if _CYRILLIC.search(tok) else en).stemWord(tok)
        for tok in _WORD.findall(normalize(text))
    ]


def stem_text(text: str | None) -> str:
    return " ".join(stems(text))
