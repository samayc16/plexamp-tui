"""The controller between the UI and the outputs: owns the Queue, routes commands to the
current output, reports plays to Plex, and keeps the status file for tmux up to date."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import asdict, replace

from tamp import config
from tamp.errors import describe
from tamp.library import Library
from tamp.models import Device, Playback, Track
from tamp.output import LocalOutput, Output, ReadOnly, RemoteOutput, TaskSet
from tamp.queue import REPEAT_MODES, Queue

REPORT_EVERY_S = 10
SCROBBLE_AT = 0.9  # Plex counts a play at 90%


class Player:
    def __init__(
        self,
        library: Library,
        on_update: Callable[[Playback, Queue], None],
        on_error: Callable[[str], None] = lambda _m: None,
        report: bool = True,
        persist: bool = True,
    ) -> None:
        self.name_of: Callable[[Device], str] = lambda d: d.name  # the app hides names on request
        self.report = report  # False: a private session, nothing goes to Plex's history
        # False: the queue and status files belong to someone else (another tamp, or the demo)
        self.persist = persist
        self.library = library
        self.on_update = on_update
        self.on_error = on_error
        self._status_at = 0.0
        self._status_key: tuple = ()
        self.queue = Queue()
        self.state = Playback()
        self.output: Output | None = None
        self._reported_at = 0.0
        self._scrobbled: str | None = None
        self._sleep_task: asyncio.Task | None = None
        self.sleep_at: float | None = None
        self._tasks = TaskSet(lambda _m: None)  # reporting is best-effort: failures stay quiet

    # ── outputs ──
    async def use(self, output: Output, resume: bool = False) -> None:
        """Switch where music plays. With `resume`, the queue carries over and keeps playing."""
        old = self.output
        self.output = output
        if old is not None:
            if resume:
                with contextlib.suppress(Exception):
                    await old.stop()
            if not isinstance(old, LocalOutput):  # tamp's own mpv stays up for switching back
                await old.close()
        await output.start()
        # show the new output's own state at once, not the last one from where you were
        self.changed(getattr(output, "state", None) or Playback(volume=self.state.volume))
        if resume and self.queue.track and getattr(output, "controllable", True):
            await self._guard(output.load(self.queue))

    def adopt_queue(self, queue: Queue) -> None:
        """A remote player's own queue (started on that device): show it, don't push it back."""
        if self.remote:
            self.queue = replace(queue, repeat=self.queue.repeat)

    @property
    def remote(self) -> bool:
        return isinstance(self.output, RemoteOutput)

    async def _guard(self, coro) -> bool:
        """Run an output command; a player that refuses control becomes a message, not a crash.
        True if it went through."""
        try:
            await coro
        except ReadOnly as e:
            self.on_error(describe(e))
        except Exception as e:  # network errors talking to a remote player
            self.on_error(f"{self.name_of(self.output.device) if self.output else 'player'}: {describe(e)}")
        else:
            return True
        return False

    def _refused_remotely(self, what: str) -> bool:
        """Edits a remote player's queue can't take from tamp yet: say so, change nothing.
        (Sending them would mean rewriting the phone's own queue on the server.)"""
        if not self.remote or self.output is None:
            return False
        self.on_error(f"{what} isn't available when playing on {self.name_of(self.output.device)} yet")
        return True

    def replace_track(self, track: Track) -> None:
        """A track's details changed (liked): update every copy in the queue."""
        tracks = tuple(track if t.id == track.id else t for t in self.queue.tracks)
        self.queue = replace(self.queue, tracks=tracks)
        if self.state.track and self.state.track.id == track.id:
            self.state = replace(self.state, track=track)
        self.on_update(self.state, self.queue)

    # ── events from the output ──
    def changed(self, state: Playback) -> None:
        if self.remote and 0 <= state.index != self.queue.current:
            self.queue = self.queue.jump(state.index)  # the remote player moved through our queue
        self.state = state
        self._maybe_report(state)
        key = (state.track.id if state.track else None, state.paused)
        if key != self._status_key or time.monotonic() - self._status_at >= 1:
            self._status_key, self._status_at = key, time.monotonic()
            self._write_status()
        self.on_update(state, self.queue)

    def advanced(self) -> None:
        """Local mpv moved to the prefetched next track."""
        self.queue = self.queue.advanced()
        self._scrobbled = None
        if isinstance(self.output, LocalOutput):  # it fixes up its own playlist afterwards
            self.output.queue = self.queue
            self.output.state = replace(
                self.output.state, track=self.queue.track, index=self.queue.current, position_ms=0
            )
        self._save_queue()
        self.changed(self.output.state if self.output else self.state)

    def finished(self, track: Track) -> None:
        self._scrobble(track)

    # ── reporting ──
    def _maybe_report(self, state: Playback) -> None:
        track = state.track
        if track is None or self.remote or not self.report:  # a remote player reports for itself
            return
        if state.duration_ms and state.position_ms >= SCROBBLE_AT * state.duration_ms:
            self._scrobble(track)
        now = time.monotonic()
        if now - self._reported_at >= REPORT_EVERY_S:
            self._reported_at = now
            status = "paused" if state.paused else "playing"
            self._tasks.spawn(self._background(self.library.report, track, status, state.position_ms))

    def _scrobble(self, track: Track) -> None:
        if self._scrobbled == track.id or self.remote or not self.report:
            return
        self._scrobbled = track.id
        self._tasks.spawn(self._background(self.library.scrobble, track))

    @staticmethod
    async def _background(fn, *args) -> None:
        with contextlib.suppress(Exception):  # reporting is best-effort; never interrupt the music
            await asyncio.to_thread(fn, *args)

    # ── queue commands ──
    async def play(self, tracks: list[Track], start: int = 0, source: str = "") -> None:
        if not tracks:
            return
        self.queue = self.queue.play(tracks, start, source)
        self._scrobbled = None
        await self._load()

    async def play_next(self, tracks: list[Track]) -> bool:
        return await self._add(self.queue.play_next(tracks), tracks, play_next=True)

    async def add_up_next(self, tracks: list[Track]) -> bool:
        return await self._add(self.queue.add_up_next(tracks), tracks, play_next=False)

    async def append(self, tracks: list[Track]) -> bool:
        if self.queue.track is not None and self._refused_remotely("Play last"):
            return False  # a server play queue only takes additions into Up Next
        await self._mutate(self.queue.append(tracks))
        return True

    async def _add(self, queue: Queue, tracks: list[Track], play_next: bool) -> bool:
        """True if the tracks were added (and, on a remote player, reached it)."""
        if not (self.remote and self.queue.track is not None):
            await self._mutate(queue)
            return True
        if not await self._guard(self.output.add(queue, list(tracks), play_next)):  # type: ignore[union-attr]
            return False
        self.queue = queue
        self._save_queue()
        self.on_update(self.state, self.queue)
        return True

    async def remove(self, index: int) -> None:
        if self._refused_remotely("Removing tracks"):
            return
        if index == self.queue.current and len(self.queue.tracks) > 1:
            await self.jump(index + 1 if index + 1 < len(self.queue.tracks) else index - 1)
        await self._mutate(self.queue.remove(index))  # an empty queue stops the output

    async def move(self, index: int, to: int) -> bool:
        if self._refused_remotely("Moving tracks"):
            return False
        await self._mutate(self.queue.move(index, to))
        return True

    async def to_up_next(self, index: int, front: bool = True) -> int:
        """Move a queued track into Up Next; returns where it ended up."""
        if not 0 <= index < len(self.queue.tracks) or index == self.queue.current:
            return index
        if self._refused_remotely("Moving tracks"):
            return index
        await self._mutate(self.queue.to_up_next(index, front))
        return self.queue.current + 1 if front else self.queue.current + self.queue.queued

    async def shuffle(self) -> bool:
        if self._refused_remotely("Shuffling the queue"):
            return False
        await self._mutate(self.queue.shuffled())
        return True

    async def clear_upcoming(self) -> None:
        if self._refused_remotely("Clearing the queue"):
            return
        await self._mutate(self.queue.clear_upcoming())

    async def cycle_repeat(self) -> None:
        self.queue = self.queue.cycle_repeat()
        if self.output:
            await self._guard(self.output.set_repeat(self.queue.repeat))
        self._save_queue()
        self.on_update(self.state, self.queue)

    async def jump(self, index: int) -> None:
        if not 0 <= index < len(self.queue.tracks):
            return
        queue = self.queue.jump(index)
        if self.remote and self.output:  # skip within the player's queue; never replace it
            if await self._guard(self.output.jump(queue)):  # type: ignore[attr-defined]
                self.queue = queue
                self._save_queue()
                self.on_update(self.state, self.queue)
            return
        self.queue = queue
        self._scrobbled = None
        await self._load()

    async def next(self) -> None:
        """Skip to the next track (repeat-one only repeats when a track ends by itself)."""
        if self.remote and self.output:
            await self._guard(self.output.skip(1))  # type: ignore[attr-defined]
            return
        nxt = self.queue.current + 1
        if nxt >= len(self.queue.tracks):
            if self.queue.repeat != "all" or not self.queue.tracks:
                return
            nxt = 0
        await self.jump(nxt)

    async def prev(self) -> None:
        """Restart the track if we're more than 3 s in, else go back one."""
        if self.remote and self.output:
            await self._guard(self.output.skip(-1))
            return
        if self.state.position_ms > 3000 or self.queue.current <= 0:
            await self.seek(0)
        else:
            await self.jump(self.queue.current - 1)

    async def _load(self) -> None:
        self._save_queue()
        if self.output:
            await self._guard(self.output.load(self.queue))
        self.on_update(self.state, self.queue)

    async def _mutate(self, queue: Queue) -> None:
        was_empty = self.queue.track is None
        self.queue = queue
        self._save_queue()
        if self.output:
            if was_empty and queue.track:
                await self._guard(self.output.load(queue))
            else:
                await self._guard(self.output.queue_changed(queue))
        self.on_update(self.state, self.queue)

    # ── transport ──
    async def toggle(self) -> None:
        if self.output and self.state.track is None and self.queue.track and not self.remote:
            await self._guard(self.output.load(self.queue))  # resume a restored queue
        elif self.output:
            await self._guard(self.output.toggle())

    async def seek(self, position_ms: int) -> None:
        if self.output and self.state.track:
            await self._guard(self.output.seek(position_ms))

    async def seek_by(self, delta_ms: int) -> None:
        await self.seek(self.state.position_ms + delta_ms)

    async def volume_by(self, delta: int) -> None:
        if self.output:
            await self._guard(self.output.set_volume(self.state.volume + delta))

    async def stop(self) -> None:
        if self.output:
            await self._guard(self.output.stop())

    # ── sleep timer ──
    def sleep_in(self, minutes: int | None) -> None:
        if self._sleep_task:
            self._sleep_task.cancel()
            self._sleep_task = None
        self.sleep_at = None
        if minutes:
            self.sleep_at = time.time() + minutes * 60
            self._sleep_task = asyncio.get_running_loop().create_task(self._sleep(minutes * 60))

    async def _sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        if self.output and not self.state.paused:
            await self.output.toggle()
        self.sleep_at = None

    # ── persistence ──
    def _save_queue(self) -> None:
        if not self.persist:
            return
        data = {
            "current": self.queue.current,
            "repeat": self.queue.repeat,
            "queued": self.queue.queued,
            "source": self.queue.source,
            "tracks": [asdict(t) for t in self.queue.tracks[:500]],
        }
        with contextlib.suppress(OSError):
            config.write_state(config.QUEUE_FILE, data)

    def restore_queue(self) -> None:
        """The queue from last time. A damaged file (hand-edited, half-written, another
        version's) never stops tamp: bad tracks are skipped and positions kept in range."""
        data = config.read_state(config.QUEUE_FILE)
        saved = data.get("tracks")
        tracks = tuple(t for t in map(_saved_track, saved) if t) if isinstance(saved, list) else ()
        if not tracks:
            return
        current, queued = _int(data.get("current")), _int(data.get("queued"))
        current = min(max(current, 0), len(tracks) - 1)
        repeat, source = data.get("repeat"), data.get("source")
        self.queue = Queue(
            tracks=tracks,
            current=current,
            repeat=repeat if isinstance(repeat, str) and repeat in REPEAT_MODES else "off",
            queued=min(max(queued, 0), len(tracks) - current - 1),
            source=source if isinstance(source, str) else "",
        )

    def _write_status(self) -> None:
        if not self.persist:
            return
        s, t = self.state, self.state.track
        data = {
            "pid": __import__("os").getpid(),
            "updated": time.time(),
            "state": "stopped" if t is None else ("paused" if s.paused else "playing"),
            "title": t.title if t else "",
            "artist": t.artist if t else "",
            "album": t.album if t else "",
            "position_ms": s.position_ms,
            "duration_ms": s.duration_ms,
            "device": self.output.device.name if self.output else "",
        }
        with contextlib.suppress(OSError):
            config.write_state(config.STATUS_FILE, data)

    def clear_status(self) -> None:
        if not self.persist:
            return
        with contextlib.suppress(OSError):
            config.STATUS_FILE.unlink()


def _int(value: object) -> int:
    return value if type(value) is int else 0


_TEXT_FIELDS = ("id", "title", "artist", "album", "album_id", "artist_id")


def _saved_track(data: object) -> Track | None:
    """A track from queue.json, or None if it isn't one."""
    if not isinstance(data, dict):
        return None
    try:
        track = Track(**{k: v for k, v in data.items() if k in Track.__dataclass_fields__})
    except TypeError:  # a required field is missing
        return None
    sane = all(isinstance(getattr(track, f), str) for f in _TEXT_FIELDS) and type(track.duration_ms) is int
    return track if sane else None
