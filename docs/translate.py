#!/usr/bin/env python3
"""Build the translated pages from index.html and the files in docs/i18n/.

Each docs/i18n/<code>.json maps English texts from index.html to their translation. Run
`python3 docs/translate.py` after changing index.html or a translation. It stops with an error if
a translation is missing a text, or has one that is no longer in index.html, so a changed sentence
never silently stays in English. It also writes the language menu into every page, index.html too.
"""

import html
import json
import re
from pathlib import Path

HERE = Path(__file__).parent

# code: (html lang, name in its own language)
LANGS = {
    "en": ("en", "English"),
    "de": ("de", "Deutsch"),
    "pt": ("pt-BR", "Português (Brasil)"),
    "sk": ("sk", "Slovenčina"),
    "uk": ("uk", "Українська"),
    "bg": ("bg", "Български"),
    "ca": ("ca", "Català"),
    "es": ("es", "Español"),
    "it": ("it", "Italiano"),
    "fr": ("fr", "Français"),
    "el": ("el", "Ελληνικά"),
    "tlh": ("tlh", "tlhIngan Hol (Klingon)"),
}


def page_name(code: str) -> str:
    return "index.html" if code == "en" else f"{code}.html"


def language_menu(current: str) -> str:
    options = "".join(
        f'<option value="{page_name(c)}"{" selected" if c == current else ""} lang="{lang}">{html.escape(name)}</option>'
        for c, (lang, name) in LANGS.items())
    return ('<!-- lang -->\n  <label class="lang">🌍 <select aria-label="Language" onchange="location.href=this.value">'
            f'{options}</select></label>\n  <!-- /lang -->')


def footer_links(current: str) -> str:
    links = " · ".join(
        f'<a href="{page_name(c)}" lang="{lang}"{" aria-current=" + chr(34) + "page" + chr(34) if c == current else ""}>'
        f'{html.escape(name)}</a>'
        for c, (lang, name) in LANGS.items())
    return f'<!-- langs -->\n  <p class="langs">{links}</p>\n  <!-- /langs -->'


def with_menus(page: str, code: str) -> str:
    page = re.sub(r"<!-- lang -->.*?<!-- /lang -->", lambda _: language_menu(code), page, flags=re.S)
    return re.sub(r"<!-- langs -->.*?<!-- /langs -->", lambda _: footer_links(code), page, flags=re.S)


def build() -> None:
    index = HERE / "index.html"
    source = with_menus(index.read_text(encoding="utf-8"), "en")
    index.write_text(source, encoding="utf-8")
    reference = set(json.loads((HERE / "i18n" / "de.json").read_text(encoding="utf-8")))
    problems = [f"index.html no longer contains {en!r}" for en in sorted(reference) if en not in source]
    for code, (lang, _) in LANGS.items():
        if code == "en":
            continue
        texts = json.loads((HERE / "i18n" / f"{code}.json").read_text(encoding="utf-8"))
        problems += [f"{code}: missing translation for {en!r}" for en in sorted(reference - set(texts))]
        problems += [f"{code}: unknown text {en!r}" for en in sorted(set(texts) - reference)]
        page = with_menus(source, code).replace('<html lang="en">', f'<html lang="{lang}">')
        for en, translated in sorted(texts.items(), key=lambda kv: -len(kv[0])):  # longest first
            page = page.replace(en, translated)
        (HERE / page_name(code)).write_text(page, encoding="utf-8")
    if problems:
        raise SystemExit("\n".join(problems))
    print(f"built {len(LANGS) - 1} translated pages")


if __name__ == "__main__":
    build()
