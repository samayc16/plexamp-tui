"""The controller between the UI and the outputs: owns the Queue, routes commands to the
current output, reports plays to Plex, and keeps the status file for tmux up to date."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import asdict, replace

from tamp import config
from tamp.library import Library
from tamp.models import Playback, Track
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
    ) -> None:
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
        if resume and self.queue.track and getattr(output, "controllable", True):
            await self._guard(output.load(self.queue))

    @property
    def remote(self) -> bool:
        return isinstance(self.output, RemoteOutput)

    async def _guard(self, coro) -> None:
        """Run an output command; a player that refuses control becomes a message, not a crash."""
        try:
            await coro
        except ReadOnly as e:
            self.on_error(str(e))
        except Exception as e:  # network errors talking to a remote player
            self.on_error(f"{self.output.device.name if self.output else 'player'}: {e}")

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
        if track is None or self.remote:  # a remote player reports for itself
            return
        if state.duration_ms and state.position_ms >= SCROBBLE_AT * state.duration_ms:
            self._scrobble(track)
        now = time.monotonic()
        if now - self._reported_at >= REPORT_EVERY_S:
            self._reported_at = now
            status = "paused" if state.paused else "playing"
            self._tasks.spawn(self._background(self.library.report, track, status, state.position_ms))

    def _scrobble(self, track: Track) -> None:
        if self._scrobbled == track.id or self.remote:
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

    async def play_next(self, tracks: list[Track]) -> None:
        await self._mutate(self.queue.play_next(tracks))

    async def append(self, tracks: list[Track]) -> None:
        await self._mutate(self.queue.append(tracks))

    async def remove(self, index: int) -> None:
        if index == self.queue.current and len(self.queue.tracks) > 1:
            await self.jump(index + 1 if index + 1 < len(self.queue.tracks) else index - 1)
        await self._mutate(self.queue.remove(index))  # an empty queue stops the output

    async def move(self, index: int, to: int) -> None:
        await self._mutate(self.queue.move(index, to))

    async def shuffle(self) -> None:
        await self._mutate(self.queue.shuffled())

    async def clear_upcoming(self) -> None:
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
        self.queue = self.queue.jump(index)
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
        data = {
            "current": self.queue.current,
            "repeat": self.queue.repeat,
            "source": self.queue.source,
            "tracks": [asdict(t) for t in self.queue.tracks[:500]],
        }
        with contextlib.suppress(OSError):
            config.write_state(config.QUEUE_FILE, data)

    def restore_queue(self) -> None:
        data = config.read_state(config.QUEUE_FILE)
        try:
            tracks = tuple(Track(**t) for t in data.get("tracks", []))
        except TypeError:  # saved by an older version
            return
        if tracks:
            repeat = data.get("repeat", "off")
            self.queue = Queue(
                tracks=tracks,
                current=min(int(data.get("current", 0)), len(tracks) - 1),
                repeat=repeat if repeat in REPEAT_MODES else "off",
                source=data.get("source", ""),
            )

    def _write_status(self) -> None:
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
        with contextlib.suppress(OSError):
            config.STATUS_FILE.unlink()
