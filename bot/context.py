"""Bounded context assembly without replaying personal exchanges twice."""

from collections import Counter

from bot.history import HistoryEntry, render_transcript
from bot.memory import Round, fitting_rounds, render_rounds
from bot.reply import reply_prefix


def conversation_context(
    entries: list[HistoryEntry], rounds: list[Round], sender: str, bot_name: str,
    transcript_max_chars: int, memory_max_chars: int,
) -> tuple[str, str]:
    """Prefer complete personal rounds; omit their matching channel lines.

    Work on an ingestion snapshot, not the live history. Match sender as well as
    text, remove only as many occurrences as memory contains, and trim AFTER
    deduplication. Stored history and memory are never changed.
    """
    kept = fitting_rounds(rounds, memory_max_chars)
    duplicates: Counter[tuple[str, str]] = Counter()
    for r in kept:
        duplicates[sender, r.source_prompt if r.source_prompt is not None else r.prompt] += 1
        duplicates[bot_name, reply_prefix(sender) + r.reply] += 1
    lines = []
    for entry in reversed(entries):
        if entry.flagged:
            continue
        key = (entry.sender, entry.text)
        if duplicates[key]:
            duplicates[key] -= 1
        else:
            lines.append(entry.line())
    return (
        render_transcript(list(reversed(lines)), transcript_max_chars),
        render_rounds(kept, memory_max_chars),
    )
