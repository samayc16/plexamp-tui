"""The play queue as an immutable value: every operation returns a new Queue.

Like Plexamp's, it has three parts after the playing track:
  up next   tracks you added ("play next" goes to its front, "add to up next" to its end)
  rest      what's left of the album / playlist / radio you started
and history before it. `queued` counts the up-next tracks right after `current`."""

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
    queued: int = 0  # up-next tracks you added, right after the current one

    @property
    def track(self) -> Track | None:
        return self.tracks[self.current] if 0 <= self.current < len(self.tracks) else None

    @property
    def upcoming(self) -> tuple[Track, ...]:
        return self.tracks[self.current + 1 :]

    @property
    def up_next(self) -> tuple[Track, ...]:
        return self.tracks[self.current + 1 : self.current + 1 + self.queued]

    @property
    def rest(self) -> tuple[Track, ...]:
        return self.tracks[self.current + 1 + self.queued :]

    @property
    def history(self) -> tuple[Track, ...]:
        return self.tracks[: max(0, self.current)]

    def play(self, tracks: list[Track] | tuple[Track, ...], start: int = 0, source: str = "") -> Queue:
        return replace(self, tracks=tuple(tracks), current=start if tracks else -1, source=source, queued=0)

    def _insert(self, at: int, tracks: tuple[Track, ...]) -> tuple[Track, ...]:
        return self.tracks[:at] + tracks + self.tracks[at:]

    def play_next(self, tracks: list[Track] | tuple[Track, ...]) -> Queue:
        """Straight after the playing track, ahead of anything else you added."""
        if self.current < 0:
            return self.play(tracks)
        added = tuple(tracks)
        return replace(self, tracks=self._insert(self.current + 1, added), queued=self.queued + len(added))

    def add_up_next(self, tracks: list[Track] | tuple[Track, ...]) -> Queue:
        """After what you've already added, before the rest of the album / playlist."""
        if self.current < 0:
            return self.play(tracks)
        added = tuple(tracks)
        at = self.current + 1 + self.queued
        return replace(self, tracks=self._insert(at, added), queued=self.queued + len(added))

    def append(self, tracks: list[Track] | tuple[Track, ...]) -> Queue:
        """At the very end ("play last")."""
        if self.current < 0:
            return self.play(tracks)
        return replace(self, tracks=self.tracks + tuple(tracks))

    def _in_up_next(self, index: int) -> bool:
        return self.current < index <= self.current + self.queued

    def remove(self, index: int) -> Queue:
        if not 0 <= index < len(self.tracks):
            return self
        tracks = self.tracks[:index] + self.tracks[index + 1 :]
        queued = self.queued - (1 if self._in_up_next(index) else 0)
        current = self.current - (1 if index < self.current else 0)
        if index == self.current:
            current = min(self.current, len(tracks) - 1)
        return replace(self, tracks=tracks, current=current, queued=max(0, queued))

    def move(self, index: int, to: int) -> Queue:
        """Move a track (dragging in the queue). A track lands in up next if it's dropped inside
        it or right at its end; dropped elsewhere it becomes part of the rest."""
        if not (0 <= index < len(self.tracks) and 0 <= to < len(self.tracks)) or index == to:
            return self
        items = list(self.tracks)
        items.insert(to, items.pop(index))
        if index == self.current:
            return replace(self, tracks=tuple(items), current=to)
        was_queued = self._in_up_next(index)
        current = self.current - (1 if index < self.current else 0)  # after taking the track out
        queued = self.queued - (1 if was_queued else 0)
        joins = current < to <= current + queued + 1
        if to <= current:
            current += 1  # dropped above the playing track
        return replace(self, tracks=tuple(items), current=current, queued=queued + (1 if joins else 0))

    def to_up_next(self, index: int, front: bool = True) -> Queue:
        """Move a queued track into Up Next: to its front ("play next") or its end."""
        if not 0 <= index < len(self.tracks) or index == self.current:
            return self
        current = self.current - (1 if index < self.current else 0)  # once the track is taken out
        queued = self.queued - (1 if self._in_up_next(index) else 0)
        return self.move(index, current + 1 if front else current + queued + 1)

    def jump(self, index: int) -> Queue:
        if not 0 <= index < len(self.tracks):
            return self
        # jumping forward uses up the up-next tracks passed over; jumping back leaves them as the rest
        queued = max(0, self.current + self.queued - index) if index > self.current else 0
        return replace(self, current=index, queued=queued)

    def advanced(self, step: int = 1) -> Queue:
        """Where the queue goes after `step` tracks, honouring repeat."""
        if not self.tracks:
            return self
        if self.repeat == "one" and step == 1:
            return self
        nxt = self.current + step
        if self.repeat == "all":
            nxt %= len(self.tracks)
        if not 0 <= nxt < len(self.tracks):
            return self
        queued = max(0, self.queued - step) if step > 0 and nxt > self.current else 0
        return replace(self, current=nxt, queued=queued)

    def shuffled(self, rng: random.Random | None = None) -> Queue:
        """Shuffle the rest; what you added to up next keeps its order."""
        rest = list(self.rest)
        (rng or random).shuffle(rest)
        head = self.tracks[: self.current + 1 + self.queued]
        return replace(self, tracks=head + tuple(rest))

    def cycle_repeat(self) -> Queue:
        return replace(self, repeat=REPEAT_MODES[(REPEAT_MODES.index(self.repeat) + 1) % len(REPEAT_MODES)])

    def clear_upcoming(self) -> Queue:
        return replace(self, tracks=self.tracks[: self.current + 1], queued=0)
