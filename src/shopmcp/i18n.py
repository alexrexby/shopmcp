"""Model-facing texts in two languages plus per-store overrides.

Every text the model reads (tool and parameter descriptions, default instructions,
error messages) is an `L10n` pair. The store picks the language with `locale` and may
override any text by its key in the `[texts]` table of `store.toml`.
"""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Literal

Locale = Literal["ru", "en"]
LOCALES: tuple[Locale, ...] = ("ru", "en")


@dataclass(frozen=True)
class L10n:
    ru: str
    en: str

    def __post_init__(self) -> None:
        if not self.ru.strip() or not self.en.strip():
            raise ValueError(f"both languages are required: {self!r}")

    def get(self, locale: Locale) -> str:
        return self.ru if locale == "ru" else self.en


_BUILTIN: dict[str, L10n] = {}


def define(key: str, *, ru: str, en: str) -> L10n:
    """Register a built-in text under a stable key; the key is what stores override."""
    if key in _BUILTIN:
        raise ValueError(f"text key defined twice: {key}")
    text = _BUILTIN[key] = L10n(ru=ru, en=en)
    return text


def builtin() -> Iterator[tuple[str, L10n]]:
    yield from _BUILTIN.items()


class Texts:
    """Resolves a text for one store: override from config, else the built-in pair."""

    def __init__(self, locale: Locale, overrides: Mapping[str, str] | None = None) -> None:
        self.locale = locale
        self._overrides = dict(overrides or {})

    def get(self, key: str, default: L10n | None = None) -> str:
        if key in self._overrides:
            return self._overrides[key]
        text = default if default is not None else _BUILTIN.get(key)
        if text is None:
            raise KeyError(f"unknown text key: {key}")
        return text.get(self.locale)
