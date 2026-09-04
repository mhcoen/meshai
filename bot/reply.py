"""Shape the model's output into a single channel-safe line and enforce the cap."""

from __future__ import annotations

import re

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_SENTENCE_RE = re.compile(r"(.+?[.!?])(?=\s|$)")
_QUOTE_PAIRS = (('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"))
# Typographic punctuation costs 3 UTF-8 bytes on the air and renders badly on small screens.
_ASCII_PUNCT = str.maketrans({"—": " - ", "–": "-", "―": " - ", "“": '"', "”": '"', "‘": "'", "’": "'", "…": "..."})


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


def ascii_punctuation(text: str) -> str:
    """Fold dashes, curly quotes and ellipses to ASCII, then re-collapse spaces."""
    return collapse_whitespace(text.translate(_ASCII_PUNCT))


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
    text = ascii_punctuation(strip_wrapping_quotes(text))
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
        body = cut.rstrip()
        if not body:
            return None
    reply = prefix + body
    assert len(reply) <= max_chars
    return reply
