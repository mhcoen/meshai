"""Bounded in-memory ring buffer of recent channel messages. Nothing is persisted."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class HistoryEntry:
    sender: str
    text: str
    flagged: bool = False  # vordur flagged this line at ingestion; never rendered
    score: float = 0.0
    rules: tuple[str, ...] = ()

    def line(self) -> str:
        return f"{self.sender}: {self.text}"


def render_transcript(lines: list[str], max_chars: int) -> str:
    """Join lines newest-last, dropping from the oldest end until within budget."""
    kept = list(lines)
    while kept and len("\n".join(kept)) > max_chars:
        kept.pop(0)
    return "\n".join(kept)


class History:
    def __init__(self, size: int):
        if size < 1:
            raise ValueError("history size must be at least 1")
        self._buf: deque[HistoryEntry] = deque(maxlen=size)

    def append(self, entry: HistoryEntry) -> None:
        self._buf.append(entry)

    def entries(self) -> list[HistoryEntry]:
        return list(self._buf)

    def render(self, max_chars: int) -> str:
        lines = [e.line() for e in self._buf if not e.flagged]
        return render_transcript(lines, max_chars)

    def clear(self) -> None:
        self._buf.clear()

    def __len__(self) -> int:
        return len(self._buf)
