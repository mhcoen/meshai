"""Bounded arguments for the local dice command."""

import re


def parse_dice(arguments: str) -> tuple[int, int]:
    if len(arguments) > 32:
        raise ValueError("dice arguments too long")
    arguments = arguments.strip()
    if not arguments:
        return 2, 6
    match = re.fullmatch(r"([0-9]{1,2})(?:\s*,\s*|\s+)([0-9]{1,4})", arguments)
    if match is None:
        raise ValueError("expected dice count and sides")
    count, sides = map(int, match.groups())
    if not 1 <= count <= 20 or not 1 <= sides <= 1000:
        raise ValueError("dice count or sides out of range")
    return count, sides
