"""Small, offline, keyword-selected radio references; no embeddings or chat storage."""

import re
import tomllib
from dataclasses import dataclass
from datetime import date
from importlib.resources import files

from bot.guard import InjectionGate
from bot.prompt import build_user_message

REFERENCE_MAX_CHARS = 1200
MAX_PASSAGES = 2


def _words(text: str) -> str:
    return " " + " ".join(re.findall(r"[a-z0-9]+", text.lower())) + " "


@dataclass(frozen=True)
class Reference:
    title: str
    keywords: tuple[str, ...]
    text: str
    source: str
    reviewed: str

    def render(self) -> str:
        return f"{self.title} (reviewed {self.reviewed})\n{self.text}\nSource: {self.source}"


def load_references() -> tuple[Reference, ...]:
    """Read only the bundled corpus, with fixed resource and passage bounds.

    Invalid operator edits fail at startup rather than silently losing grounding.
    Source URLs are provenance only: the bot never fetches them.
    """
    with files("bot").joinpath("radio_reference.toml").open("rb") as stream:
        raw = stream.read(65_537)
    if len(raw) > 65_536:
        raise ValueError("radio reference file exceeds 64 KiB")
    data = tomllib.loads(raw.decode("ascii"))
    rows = data.get("reference", [])
    if not isinstance(rows, list) or not 1 <= len(rows) <= 32:
        raise ValueError("radio reference file needs 1 to 32 passages")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("radio reference passages must be TOML tables")  # noqa: TRY004 - invalid file data
        fields = [row.get(k) for k in ("title", "text", "source", "reviewed")]
        if any(not isinstance(v, str) or not v.strip() or not v.isascii()
               or any(ord(c) < 32 or ord(c) > 126 for c in v) for v in fields):
            raise ValueError("radio reference fields must be nonempty printable ASCII")
        title, body, source, reviewed = fields
        keywords = row.get("keywords")
        if (not isinstance(keywords, list) or not 1 <= len(keywords) <= 32
                or any(not isinstance(k, str) or not k.isascii() or not _words(k).strip()
                       or len(k) > 64 for k in keywords)):
            raise ValueError("radio reference needs 1 to 32 ASCII keywords")
        if not source.startswith("https://"):
            raise ValueError("radio reference needs an HTTPS source link")
        date.fromisoformat(reviewed)
        ref = Reference(title, tuple(keywords), body, source, reviewed)
        if len(ref.render()) > REFERENCE_MAX_CHARS:
            raise ValueError("radio reference passage exceeds context budget")
        result.append(ref)
    return tuple(result)


def checked_references(gate: InjectionGate) -> tuple[Reference, ...]:
    """Validate the corpus against the configured detector before opening a radio.

    Check standalone passages and their normal context framing. Runtime checks
    remain necessary for combinations with user questions and conversation.
    """
    references = load_references()
    for ref in references:
        for text in (ref.render(), build_user_message("", "", reference=ref.render())):
            verdict = gate.check(text)
            if verdict.blocked or verdict.error:
                reason = verdict.error or f"score {verdict.score:g}, rules {', '.join(verdict.rules)}"
                raise ValueError(f"radio reference {ref.title!r} blocked by injection gate: {reason}")
    return references


def select_references(prompt: str, references: tuple[Reference, ...]) -> str:
    """Rank whole-word/phrase matches; include at most two complete passages.

    Only the current question selects topics, never another sender's chatter.
    No match means no reference block. Ties retain the curated file's order.
    """
    query = _words(prompt)
    ranked = [(sum(_words(k) in query for k in ref.keywords), ref) for ref in references]
    ranked.sort(key=lambda item: item[0], reverse=True)
    kept = []
    size = 0
    for score, ref in ranked:
        if score == 0 or len(kept) == MAX_PASSAGES:
            break
        passage = ref.render()
        added = len(passage) + (2 if kept else 0)
        if size + added <= REFERENCE_MAX_CHARS:
            kept.append(passage)
            size += added
    return "\n\n".join(kept)
