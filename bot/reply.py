"""Shape the model's output into a single channel-safe line and enforce the cap.

Everything that leaves the bot is plain ASCII with ordinary punctuation. Dashes become
commas, ellipses become periods, curly quotes become straight ones, accented letters
lose their accents, and anything else outside ASCII (emoji, symbols) is dropped. One
byte per character on the air, and readable on any screen.
"""

from __future__ import annotations

import re
import unicodedata

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_SENTENCE_RE = re.compile(r"(.+?[.!?])(?=\s|$)")
_QUOTE_PAIRS = (('"', '"'), ("'", "'"), ("\u201c", "\u201d"), ("\u2018", "\u2019"))

# Dashes used as separators read as commas; a hyphen inside a word stays a hyphen.
_DASH_RE = re.compile(r"\s*[\u2014\u2013\u2015]\s*|\s+-\s+")
_ELLIPSIS_RE = re.compile(r"\u2026|\.{3,}")
_QUOTE_MAP = str.maketrans({"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'", "\u00b4": "'", "\u2032": "'"})
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([,.!?;:])")
_DOUBLE_COMMA_RE = re.compile(r",(\s*,)+")
_COMMA_BEFORE_STOP_RE = re.compile(r",\s*([.!?])")


def strip_think(text: str) -> str:
    """Remove any <think>...</think> block a reasoning model may have leaked."""
    return _THINK_RE.sub("", text)


def collapse_whitespace(text: str) -> str:
    """Collapse all runs of whitespace, including newlines, to single spaces."""
    return " ".join(text.split())


def strip_wrapping_quotes(text: str) -> str:
    for left, right in _QUOTE_PAIRS:
        if len(text) >= 2 and text.startswith(left) and text.endswith(right):
            return text[1:-1].strip()
    return text


def plain_ascii(text: str) -> str:
    """Reduce to ASCII with ordinary punctuation. See the module docstring."""
    text = text.translate(_QUOTE_MAP)
    text = _DASH_RE.sub(", ", text)
    text = _ELLIPSIS_RE.sub(".", text)
    text = text.replace(";", ",")
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = collapse_whitespace(text)
    text = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", text)
    text = _DOUBLE_COMMA_RE.sub(",", text)
    text = _COMMA_BEFORE_STOP_RE.sub(r"\1", text)
    return text.strip(" ,")


def first_sentence(text: str) -> str:
    """Keep only the first sentence, plus the next one if the first is a question.

    A terminator followed by a non-space (3.5, e.g.) does not split. A question on its
    own is never a complete reply (riddle-style jokes), so its answer is kept too.
    """
    match = _SENTENCE_RE.match(text)
    if not match:
        return text
    first = match.group(1)
    if first.endswith("?"):
        rest = text[match.end():].lstrip()
        follow = _SENTENCE_RE.match(rest)
        if follow:
            return f"{first} {follow.group(1)}"
        if rest:
            return f"{first} {rest}"
    return first


def shape_reply(raw: str) -> str:
    """The full outbound normalisation: strip think blocks, collapse, unquote, ASCII, first sentence."""
    text = collapse_whitespace(strip_think(raw))
    text = plain_ascii(strip_wrapping_quotes(text))
    return first_sentence(text).strip()


def compose_reply(sender: str, text: str, max_chars: int) -> str | None:
    """Return ``"@[sender] text"`` cut to ``max_chars`` total, or None if nothing fits.

    The prefix is never truncated. When the body must be cut, it is cut at the last
    space if one exists in the second half of the available room, so we avoid ending
    on a fragment; otherwise it is a hard cut.
    """
    prefix = f"@[{sender}] "
    available = max_chars - len(prefix)
    body = text.strip()
    if available <= 0 or not body:
        return None
    if len(body) > available:
        cut = body[:available]
        if body[available] != " ":  # the cut landed inside a word
            space = cut.rfind(" ")
            if space >= available // 2:
                cut = cut[:space]
        body = cut.rstrip(" ,")
        if not body:
            return None
    reply = prefix + body
    assert len(reply) <= max_chars
    return reply
