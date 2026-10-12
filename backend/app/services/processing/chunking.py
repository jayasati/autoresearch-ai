"""
Chunking: dividing a document into overlapping spans, with exact offsets.

The output is **spans, not strings**: `(start, end)` pairs into the document text.
A chunk's text is always `document_text[start:end]`, never a reconstruction, which is
what lets the `document_chunk` row's `span_is_non_empty` constraint and
`DocumentChunkCreate`'s span check hold by construction. A claim verified against a
chunk can then be followed to the exact characters in the exact document version.

Strategy (sizes are in **characters**, not tokens — see README.md for why):

1. A chunk starts at `start` and may run to at most `start + chunk_size`.
2. If that reaches the end of the text, the chunk ends there.
3. Otherwise the end moves **back** to the best boundary in the second half of that
   window, in order of preference: a paragraph break, a sentence end, a line break,
   any whitespace. Only text with no whitespace at all is cut mid-word.
4. Trailing whitespace is trimmed off the chunk, so no chunk starts or ends with it.
5. The next chunk starts `chunk_overlap` characters before this one ended, moved
   **forward** to the start of the next word. So the real overlap is at most the
   configured overlap, and short of it by less than one word.

Searching only the second half of the window is what bounds chunk size from below:
a boundary near the start would produce a tiny chunk followed by a full one, and the
tiny one would be retrieved on almost no context.
"""

import re

# A sentence end: terminal punctuation, optionally a closing quote or bracket, then
# whitespace. Abbreviations ("e.g. ", "Fig. 3") also match. That is harmless here: a
# false sentence end only moves a chunk boundary, it never changes or loses text.
_SENTENCE_END = re.compile(r"[.!?][\"'”’)\]]?(?=\s)")


def _find_break(text: str, start: int, hard_end: int) -> int:
    """The end offset for a chunk starting at `start` that may not pass `hard_end`."""
    floor = start + max(1, (hard_end - start) // 2)
    # The window includes the character at hard_end, so that a sentence ending exactly
    # at the limit (period at hard_end - 1, space at hard_end) is still recognised.
    window = text[floor : hard_end + 1]

    paragraph = window.rfind("\n\n")
    if paragraph != -1:
        return floor + paragraph

    last_sentence = None
    for match in _SENTENCE_END.finditer(window):
        last_sentence = match
    if last_sentence is not None:
        return floor + last_sentence.end()

    newline = window.rfind("\n")
    if newline != -1:
        return floor + newline

    for offset in range(len(window) - 1, -1, -1):
        if window[offset].isspace():
            return floor + offset

    return hard_end


def _skip_whitespace(text: str, position: int) -> int:
    while position < len(text) and text[position].isspace():
        position += 1
    return position


def _next_start(text: str, previous_start: int, previous_end: int, overlap: int) -> int:
    """Where the chunk after `[previous_start, previous_end)` begins."""
    if overlap <= 0:
        return _skip_whitespace(text, previous_end)

    candidate = max(previous_end - overlap, 0)
    # Mid-word? Move forward to the next word, so no chunk opens on half a word.
    if 0 < candidate < len(text) and not text[candidate - 1].isspace():
        boundary = candidate
        while boundary < previous_end and not text[boundary].isspace():
            boundary += 1
        # If the word runs all the way to the previous chunk's end (text with no
        # whitespace at all), moving forward would discard the overlap or, worse, skip
        # text. Keep the exact character offset instead.
        if boundary < previous_end:
            candidate = boundary
    candidate = _skip_whitespace(text, candidate)

    # Every chunk must start after the last one, or the loop would never end. This
    # only triggers when the overlap is close to the chunk size and the boundary search
    # shortened the chunk; the next chunk then simply starts without overlap.
    if candidate <= previous_start or candidate >= previous_end:
        return _skip_whitespace(text, previous_end)
    return candidate


def chunk_spans(text: str, *, chunk_size: int, chunk_overlap: int) -> list[tuple[int, int]]:
    """Split `text` into `(start, end)` spans. Empty text gives no spans.

    Guarantees, each covered by a test:

    - every span is non-empty and at most `chunk_size` characters;
    - spans neither start nor end with whitespace;
    - spans are in order, and each starts after the previous one starts;
    - consecutive spans overlap by at most `chunk_overlap` characters;
    - every non-whitespace character of `text` falls inside at least one span;
    - the same input always gives the same spans.
    """
    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")
    if not 0 <= chunk_overlap < chunk_size:
        raise ValueError("chunk_overlap must be at least 0 and smaller than chunk_size")

    length = len(text)
    spans: list[tuple[int, int]] = []
    start = _skip_whitespace(text, 0)

    while start < length:
        hard_end = start + chunk_size
        end = length if hard_end >= length else _find_break(text, start, hard_end)

        while end > start and text[end - 1].isspace():
            end -= 1
        spans.append((start, end))

        if _skip_whitespace(text, end) >= length:
            break
        start = _next_start(text, start, end, chunk_overlap)

    return spans
