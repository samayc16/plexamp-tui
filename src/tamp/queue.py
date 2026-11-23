"""The play queue as an immutable value: every operation returns a new Queue."""

from __future__ import annotations

import random
from dataclasses import dataclass, replace

from tamp.models import Track

REPEAT_MODES = ("off", "all", "one")


@dataclass(frozen=True)
class Queue:
    tracks: tuple[Track, ...] = ()
    current: int = -1
    repeat: str = "off"
    source: str = ""  # what it was started from ("Album · Kid A", "Radio · Björk")

    @property
    def track(self) -> Track | None:
        return self.tracks[self.current] if 0 <= self.current < len(self.tracks) else None

    @property
    def upcoming(self) -> tuple[Track, ...]:
        return self.tracks[self.current + 1 :]

    def play(self, tracks: list[Track] | tuple[Track, ...], start: int = 0, source: str = "") -> Queue:
        return replace(self, tracks=tuple(tracks), current=start if tracks else -1, source=source)

    def play_next(self, tracks: list[Track] | tuple[Track, ...]) -> Queue:
        if self.current < 0:
            return self.play(tracks)
        at = self.current + 1
        return replace(self, tracks=self.tracks[:at] + tuple(tracks) + self.tracks[at:])

    def append(self, tracks: list[Track] | tuple[Track, ...]) -> Queue:
        if self.current < 0:
            return self.play(tracks)
        return replace(self, tracks=self.tracks + tuple(tracks))

    def remove(self, index: int) -> Queue:
        if not 0 <= index < len(self.tracks):
            return self
        tracks = self.tracks[:index] + self.tracks[index + 1 :]
        current = self.current - (1 if index < self.current else 0)
        if index == self.current:
            current = min(self.current, len(tracks) - 1)
        return replace(self, tracks=tracks, current=current)

    def move(self, index: int, to: int) -> Queue:
        if not (0 <= index < len(self.tracks) and 0 <= to < len(self.tracks)) or index == to:
            return self
        items = list(self.tracks)
        items.insert(to, items.pop(index))
        current = self.current
        if index == self.current:
            current = to
        elif index < self.current <= to:
            current -= 1
        elif to <= self.current < index:
            current += 1
        return replace(self, tracks=tuple(items), current=current)

    def jump(self, index: int) -> Queue:
        return replace(self, current=index) if 0 <= index < len(self.tracks) else self

    def advanced(self, step: int = 1) -> Queue:
        """Where the queue goes after `step` tracks, honouring repeat."""
        if not self.tracks:
            return self
        if self.repeat == "one" and step == 1:
            return self
        nxt = self.current + step
        if self.repeat == "all":
            nxt %= len(self.tracks)
        return replace(self, current=nxt) if 0 <= nxt < len(self.tracks) else self

    def shuffled(self, rng: random.Random | None = None) -> Queue:
        """Shuffle what's left, keeping the playing track first."""
        rest = list(self.upcoming)
        (rng or random).shuffle(rest)
        return replace(self, tracks=self.tracks[: self.current + 1] + tuple(rest))

    def cycle_repeat(self) -> Queue:
        return replace(self, repeat=REPEAT_MODES[(REPEAT_MODES.index(self.repeat) + 1) % len(REPEAT_MODES)])

    def clear_upcoming(self) -> Queue:
        return replace(self, tracks=self.tracks[: self.current + 1])
