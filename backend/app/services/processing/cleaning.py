"""
Text cleaning: making provider text consistent without changing what it says.

**Every step here removes or normalises. None of them adds a word.** That is the rule
the whole module is built around, because the text produced here becomes
`Document.text`, chunk offsets are measured against it, and a claim will eventually be
verified against those chunks. A cleaner that "repaired" a sentence would put words in
a source's mouth.

So the operations are deliberately limited to:

- removing markup (tags) and decoding HTML entities (`&amp;` is `&`);
- Unicode NFC normalisation — composes `e` + combining accent into `é`, and nothing
  more. NFKC is *not* used: it rewrites `m²` as `m2` and `½` as `1⁄2`, which changes
  meaning;
- deleting invisible characters (zero-width spaces, soft hyphens, BOMs) and control
  characters;
- collapsing runs of whitespace, while keeping paragraph breaks;
- removing **whole lines** of recognisable boilerplate, a leading "Abstract" label and
  a trailing copyright notice — see `strip_boilerplate`, which reports everything it
  removed.

Things it deliberately does not do, because each would rewrite text rather than clean
it: re-join words hyphenated across a line break (`self-\\nattention` is ambiguous),
fix spelling or encoding errors (mojibake is left as it is), expand abbreviations, or
remove a "[...]" a provider used to mark a truncated snippet.
"""

import html
import re
import unicodedata

# Tags that sit inside a run of text. Removed with no replacement, so that
# `H<sub>2</sub>O` stays `H2O` rather than becoming `H 2 O`. Every other tag is treated
# as a block boundary and replaced with a line break, so `end.</p><p>Next` does not
# fuse into `end.Next`. The `jats:` names are what Crossref-derived abstracts use.
_INLINE_TAGS = frozenset(
    {
        "a", "abbr", "b", "bdi", "bdo", "cite", "code", "em", "font", "i", "kbd",
        "mark", "q", "s", "small", "span", "strong", "sub", "sup", "u", "var",
        "jats:bold", "jats:italic", "jats:monospace", "jats:sc", "jats:sub",
        "jats:sup", "jats:underline", "mml:math", "mml:mi", "mml:mn", "mml:mo",
        "mml:msub", "mml:msup", "mml:mrow",
    }
)  # fmt: skip

# A tag must start with a letter immediately after `<`, so `a < b` and `x<5` are never
# mistaken for markup. `a <b and c> d` would be — a known limitation, accepted because
# provider text with literal angle brackets around words is far rarer than real tags.
_TAG = re.compile(r"</?([A-Za-z][A-Za-z0-9:_-]*)(?:\s[^<>]*)?/?>")

# Removed outright: they render as nothing, but they break word matching and inflate
# character counts. U+00AD is the soft hyphen.
_INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)

_HORIZONTAL_SPACE = re.compile(r"[^\S\n]+")
_EXCESS_BLANK_LINES = re.compile(r"\n{3,}")

# --------------------------------------------------------------------------- #
# Boilerplate
# --------------------------------------------------------------------------- #

# A line is only removed if the **whole line** matches one of these and it is short.
# Matching whole lines is what makes this safe: a sentence in an article that happens
# to mention cookies or newsletters is part of a longer line and is never touched.
#
# Every pattern is anchored on the boilerplate phrase **at the start of the line**.
# That is not a style choice. Snippets and abstracts are usually a single line, so a
# pattern allowed to match "anything, then 'all rights reserved'" matches an entire
# short abstract that happens to end with its copyright notice — and deletes the
# abstract. A test pins that case down.
_BOILERPLATE_LINE_MAX_CHARS = 120

_BOILERPLATE_LINES = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"skip to (?:the )?(?:main )?(?:content|navigation)\.?",
        r"back to top\.?",
        r"advertisement\.?",
        # Cookie banners, recognised by how they end, not by the word "cookies": a
        # paper may well say "we use cookies as a proxy for returning users".
        r"(?:this (?:web)?site|we) uses? cookies\b.{0,80}"
        r"\b(?:experience|by continuing|cookie policy|privacy policy)\b.{0,40}",
        r"(?:accept|allow|reject|manage) (?:all )?cookies\.?",
        r"share (?:this|on) "
        r"(?:article|story|post|page|facebook|twitter|x|linkedin|reddit|email)\.?",
        r"(?:sign up|subscribe) (?:for|to) (?:our|the) newsletter\b.*",
        r"(?:read more|continue reading)\s*(?:\.{3}|…|»|>>)?",
        r"click here\b.*",
        r"(?:©|copyright\b).{0,100}",
        r"all rights reserved\.?",
    )
]

# "Abstract" or "Abstract:" as a label at the very start of the text. Only when more
# text follows — a document consisting of the single word "Abstract" is left alone.
_LEADING_ABSTRACT_LABEL = re.compile(r"\A\s*abstract\s*[:.\-–—]?\s+(?=\S)", re.IGNORECASE)

# A copyright notice ending the text: "© 2021 Elsevier Ltd. All rights reserved."
# Requires the © sign or the word Copyright, *and* a four-digit year, *and* that it runs
# to the end of the text within 200 characters. "(c)" alone is not accepted — it is
# far more often a list marker, as in "(c) 2005 patients were enrolled".
_TRAILING_COPYRIGHT = re.compile(
    r"\s*(?:copyright\s*)?(?:©|copyright\b)\s*(?:\d{4}\b)[^\n]{0,190}\Z", re.IGNORECASE
)


def _replace_tag(match: re.Match[str]) -> str:
    return "" if match.group(1).lower() in _INLINE_TAGS else "\n"


def _strip_control(text: str) -> str:
    """Drop control characters (Unicode category Cc), keeping newlines and tabs."""
    return "".join(
        ch for ch in text if ch in "\n\t" or unicodedata.category(ch) != "Cc"
    )


def normalise_whitespace(text: str) -> str:
    """Collapse horizontal whitespace, strip each line, keep paragraph breaks.

    Two newlines are a paragraph break and survive; three or more become two. A single
    newline survives too, because in web text it often separates list items or
    headings, and the chunker treats it as a weaker boundary than a paragraph.
    """
    lines = [_HORIZONTAL_SPACE.sub(" ", line).strip() for line in text.split("\n")]
    return _EXCESS_BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def clean_text(raw: str | None) -> str:
    """Normalise provider text. Returns `""` for `None` or anything that cleans to nothing.

    The order matters in one place: tags are removed **before** entities are decoded,
    so a literal `&lt;b&gt;` in the source survives as the text `<b>` rather than being
    decoded into a tag and then deleted.
    """
    if not raw:
        return ""

    text = _TAG.sub(_replace_tag, raw)
    text = html.unescape(text)
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace(" ", "\n").replace(" ", "\n\n")
    text = text.translate(_INVISIBLE)
    text = _strip_control(text)
    return normalise_whitespace(text)


def strip_boilerplate(text: str) -> tuple[str, list[str]]:
    """Remove recognisable boilerplate. Returns the text and **what was removed**.

    Expects text that has already been through `clean_text`. The removed fragments are
    returned rather than discarded so the decision is auditable: if a pattern ever
    removes something it should not have, the processed document says exactly what, and
    the fix can be tested against it.

    Conservative by construction — see the module docstring and the constants above.
    When in doubt, text is kept: a stray "Read more" in a chunk costs a few characters
    of noise, while a wrongly removed sentence is a passage a claim can no longer cite.
    """
    if not text:
        return "", []

    removed: list[str] = []
    kept_lines: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if (
            stripped
            and len(stripped) <= _BOILERPLATE_LINE_MAX_CHARS
            and any(p.fullmatch(stripped) for p in _BOILERPLATE_LINES)
        ):
            removed.append(stripped)
            continue
        kept_lines.append(line)
    text = normalise_whitespace("\n".join(kept_lines))

    label = _LEADING_ABSTRACT_LABEL.match(text)
    if label:
        removed.append(label.group(0).strip())
        text = text[label.end() :]

    notice = _TRAILING_COPYRIGHT.search(text)
    # Never let the notice be the whole text: if all we have is a copyright line, the
    # line-level rule has already decided, and this rule must not empty a document.
    if notice and notice.start() > 0:
        removed.append(notice.group(0).strip())
        text = text[: notice.start()]

    return normalise_whitespace(text), removed
