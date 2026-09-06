"""Per-person memory: the last few answered exchanges with each sender name.

In memory only; a restart clears it. Sender names are unauthenticated, so this is
continuity for a conversation, not identity. Garbage collection has three parts:
a cap on rounds per person, an age limit on rounds, and a cap on the number of
people, least recently seen out first.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Round:
    at: float
    prompt: str
    reply: str


class PersonMemory:
    def __init__(
        self,
        rounds: int = 20,
        max_age_s: float = 7 * 24 * 3600.0,
        max_people: int = 500,
        clock: Callable[[], float] = time.monotonic,
    ):
        if rounds < 1 or max_people < 1 or max_age_s <= 0:
            raise ValueError("rounds and max_people must be at least 1, max_age_s positive")
        self.rounds = rounds
        self.max_age_s = max_age_s
        self.max_people = max_people
        self._clock = clock
        self._people: OrderedDict[str, deque[Round]] = OrderedDict()

    def record(self, sender: str, prompt: str, reply: str) -> None:
        """Remember one answered exchange. Evicts the least recently seen person past the cap."""
        self.sweep()
        rounds = self._people.get(sender)
        if rounds is None:
            rounds = deque(maxlen=self.rounds)
            self._people[sender] = rounds
        else:
            self._people.move_to_end(sender)
        rounds.append(Round(self._clock(), prompt, reply))
        while len(self._people) > self.max_people:
            self._people.popitem(last=False)

    def rounds_for(self, sender: str) -> list[Round]:
        """Fresh rounds for one sender, oldest first. Stale rounds are dropped on the way."""
        self.sweep()
        rounds = self._people.get(sender)
        if rounds is None:
            return []
        self._people.move_to_end(sender)
        return list(rounds)

    def forget(self, sender: str) -> bool:
        return self._people.pop(sender, None) is not None

    def sweep(self) -> int:
        """Drop stale rounds everywhere and people with none left. Returns people removed."""
        removed = 0
        for sender in list(self._people):
            self._expire(sender, self._people[sender])
            if not self._people[sender]:
                del self._people[sender]
                removed += 1
        return removed

    def _expire(self, sender: str, rounds: deque[Round]) -> None:
        cutoff = self._clock() - self.max_age_s
        while rounds and rounds[0].at < cutoff:
            rounds.popleft()

    @property
    def people(self) -> int:
        return len(self._people)

    @property
    def total_rounds(self) -> int:
        return sum(len(r) for r in self._people.values())


def render_rounds(rounds: list[Round], max_chars: int) -> str:
    """'asked: ...' / 'replied: ...' pairs, oldest first, trimmed from the oldest end to fit."""
    lines = [f"asked: {r.prompt}\nreplied: {r.reply}" for r in rounds]
    while lines and len("\n".join(lines)) > max_chars:
        lines.pop(0)
    return "\n".join(lines)
