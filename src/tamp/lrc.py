"""Lyrics: LRC (timed) and plain text, and finding the line to highlight."""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass

_TIME = re.compile(r"\[(\d+):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_TAG = re.compile(r"^\[(ar|ti|al|by|offset|length|re|ve):.*\]$", re.IGNORECASE)
_OFFSET = re.compile(r"^\[offset:\s*([+-]?\d+)\]$", re.IGNORECASE)


@dataclass(frozen=True)
class Line:
    time_ms: int | None
    text: str


@dataclass(frozen=True)
class Lyrics:
    lines: tuple[Line, ...]
    timed: bool

    def index_at(self, position_ms: int) -> int:
        """The line being sung at `position_ms` (-1 before the first one, or if untimed)."""
        if not self.timed:
            return -1
        times = [line.time_ms or 0 for line in self.lines]
        return bisect.bisect_right(times, position_ms) - 1


def _ms(minutes: str, seconds: str, fraction: str | None) -> int:
    frac = (fraction or "0").ljust(3, "0")[:3]
    return (int(minutes) * 60 + int(seconds)) * 1000 + int(frac)


def parse(text: str) -> Lyrics:
    """Parse LRC; anything without timestamps comes back as untimed lines."""
    offset = 0
    timed: list[Line] = []
    plain: list[Line] = []
    for raw in text.splitlines():
        line = raw.strip()
        if m := _OFFSET.match(line):
            offset = int(m.group(1))
            continue
        if _TAG.match(line):
            continue
        stamps = list(_TIME.finditer(line))
        if not stamps:
            plain.append(Line(None, line))
            continue
        words = _TIME.sub("", line).strip()
        for stamp in stamps:  # "[00:12.00][01:30.00]chorus" repeats a line
            timed.append(Line(max(0, _ms(*stamp.groups()) - offset), words))
    if timed:
        return Lyrics(tuple(sorted(timed, key=lambda line: line.time_ms or 0)), timed=True)
    while plain and not plain[-1].text:
        plain.pop()
    return Lyrics(tuple(plain), timed=False)
