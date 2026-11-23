"""Where the music plays.

LocalOutput: mpv on this computer. mpv's playlist only ever holds [current, next]: enough for
gapless playback, while the app's Queue stays the single source of truth.
RemoteOutput: a Plex/Plexamp player elsewhere. What it plays comes from the server's session
list (works for phones too); controls go through Plex's remote-control API when it accepts them.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable, Coroutine
from dataclasses import replace
from typing import Any, Protocol

from tamp.library import Library
from tamp.models import THIS_DEVICE, Device, Playback, Stream, Track
from tamp.mpv import Mpv, MpvError
from tamp.queue import Queue

REMOTE_WINDOW = 100  # tracks handed to a remote player around the current one


class Output(Protocol):
    device: Device
    controllable: bool

    async def start(self) -> None: ...
    async def load(self, queue: Queue) -> None: ...
    async def queue_changed(self, queue: Queue) -> None: ...
    async def toggle(self) -> None: ...
    async def seek(self, position_ms: int) -> None: ...
    async def set_volume(self, volume: int) -> None: ...
    async def set_repeat(self, mode: str) -> None: ...
    async def stop(self) -> None: ...
    async def close(self) -> None: ...


class ReadOnly(RuntimeError):
    """The player shows up in Plex but doesn't accept remote control."""


class TaskSet:
    """Background tasks we keep a reference to (so they aren't garbage-collected mid-flight)
    and whose failures surface as messages instead of vanishing."""

    def __init__(self, on_error: Callable[[str], None]) -> None:
        self.on_error = on_error
        self._tasks: set[asyncio.Task] = set()

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            self.on_error(str(exc) or type(exc).__name__)

    def cancel_all(self) -> None:
        for task in self._tasks:
            task.cancel()


class LocalOutput:
    """mpv. Calls `on_change(playback)` on every state change and `on_advance()` when mpv moves
    on to the prefetched next track by itself (the gapless hand-off)."""

    controllable = True

    def __init__(
        self,
        library: Library,
        mpv: Mpv,
        on_change: Callable[[Playback], None],
        on_advance: Callable[[], None],
        on_finished: Callable[[Track], None],
        loudness: bool = True,
        on_error: Callable[[str], None] = lambda _m: None,
    ) -> None:
        self.device = THIS_DEVICE
        self.library = library
        self.mpv = mpv
        self.on_change = on_change
        self.on_advance = on_advance
        self.on_finished = on_finished
        self.on_error = on_error
        self.loudness = loudness
        self.queue = Queue()
        self.state = Playback()
        self._loaded: list[str] = []  # track ids in mpv's playlist: [current] or [current, next]
        self._streams: dict[str, Stream] = {}
        self._headers: tuple[tuple[str, str], ...] | None = None
        self._lock = asyncio.Lock()  # one playlist edit at a time
        self._tasks = TaskSet(lambda m: self.on_error(f"playback: {m}"))

    async def start(self) -> None:
        await self.mpv.start()
        self._headers = None  # a fresh mpv has none set

    def handle_event(self, msg: dict) -> None:
        """Called by Mpv for every event (wired up by the app)."""
        event = msg.get("event")
        if event == "property-change":
            name, value = msg.get("name"), msg.get("data")
            s = self.state
            if name == "time-pos" and value is not None:
                self.state = replace(s, position_ms=int(value * 1000))
            elif name == "duration" and value:
                self.state = replace(s, duration_ms=int(value * 1000))
            elif name == "pause":
                self.state = replace(s, paused=bool(value))
            elif name == "volume" and value is not None:
                self.state = replace(s, volume=int(value))
            elif name == "playlist-pos" and value == 1 and len(self._loaded) == 2:
                self._loaded.pop(0)
                self.on_advance()  # the Player moves its queue on and hands it back to us
                self._tasks.spawn(self._after_advance())
            elif name == "idle-active" and value:
                self.state = replace(s, paused=True)
            else:
                return
            self.on_change(self.state)
        elif event == "end-file" and msg.get("reason") == "eof" and self.queue.track:
            self.on_finished(self.queue.track)

    async def _after_advance(self) -> None:
        async with self._lock:
            with contextlib.suppress(MpvError):
                await self.mpv.command("playlist-remove", 0)  # the finished track: keep [current, next]
            await self._sync_next()

    async def _stream(self, track: Track) -> Stream:
        if track.id not in self._streams:
            self._streams[track.id] = await asyncio.to_thread(self.library.stream, track)
        return self._streams[track.id]

    async def _append(self, track: Track, mode: str) -> None:
        stream = await self._stream(track)
        if stream.headers != self._headers:
            await self.mpv.set("http-header-fields", [f"{k}: {v}" for k, v in stream.headers])
            self._headers = stream.headers
        options = {}
        if self.loudness and stream.gain_db is not None:
            options["volume-gain"] = f"{stream.gain_db:.2f}"
        await self.mpv.command("loadfile", stream.url, mode, -1, options)

    async def load(self, queue: Queue) -> None:
        """Play queue.track from the start, with the next one prefetched."""
        async with self._lock:
            self.queue = queue
            self._loaded = []
            if queue.track is None:
                await self._stop()
                return
            self.state = Playback(
                track=queue.track,
                duration_ms=queue.track.duration_ms,
                paused=False,
                volume=self.state.volume,
                index=queue.current,
            )
            self.on_change(self.state)
            await self._append(queue.track, "replace")
            self._loaded = [queue.track.id]
            await self.mpv.set("pause", False)
            await self._sync_next()

    async def queue_changed(self, queue: Queue) -> None:
        """The queue changed around the playing track: fix up what mpv plays next."""
        if queue.track is None:
            await self.stop()
            return
        if self._loaded and queue.track.id != self._loaded[0]:
            await self.load(queue)  # the playing track itself changed
            return
        async with self._lock:
            self.queue = queue
            self.state = replace(self.state, track=queue.track, index=queue.current)
            await self._sync_next()

    async def _sync_next(self) -> None:
        """Make mpv's second entry the queue's next track (or nothing). Call with the lock held."""
        if not self._loaded:
            return
        after = self.queue.advanced()
        nxt = after.track if self.queue.repeat != "one" and after.current != self.queue.current else None
        want = nxt.id if nxt else None
        have = self._loaded[1] if len(self._loaded) > 1 else None
        if want == have:
            return
        if have is not None:
            with contextlib.suppress(MpvError):
                await self.mpv.command("playlist-remove", 1)
            self._loaded = self._loaded[:1]
        if nxt is not None:
            try:
                await self._append(nxt, "append")
            except Exception as e:  # can't fetch the next stream: play on, say why it'll stop
                self.on_error(f"couldn't queue {nxt.title}: {e}")
                return
            self._loaded.append(nxt.id)

    async def toggle(self) -> None:
        await self.mpv.command("cycle", "pause")

    async def seek(self, position_ms: int) -> None:
        await self.mpv.command("seek", max(0, position_ms) / 1000, "absolute")

    async def set_volume(self, volume: int) -> None:
        await self.mpv.set("volume", max(0, min(100, volume)))

    async def set_repeat(self, mode: str) -> None:
        await self.mpv.set("loop-file", "inf" if mode == "one" else "no")
        async with self._lock:
            self.queue = replace(self.queue, repeat=mode)
            await self._sync_next()

    async def stop(self) -> None:
        async with self._lock:
            await self._stop()

    async def _stop(self) -> None:
        with contextlib.suppress(MpvError):
            await self.mpv.command("stop")
        self._loaded = []
        self.state = replace(self.state, track=None, paused=True, position_ms=0)
        self.on_change(self.state)

    async def close(self) -> None:
        self._tasks.cancel_all()
        await self.mpv.quit()


class RemoteOutput:
    """A Plex/Plexamp player somewhere else: your phone, a desktop or headless Plexamp, a TV.

    What it's playing comes from the server's session list, which every Plex player reports
    to; between reports the position is extrapolated so lyrics stay in sync. Controls go through
    plexapi's PlexClient when the player accepts remote control; without one it's follow-only."""

    POLL_S = 1.0

    def __init__(
        self,
        device: Device,
        server,
        library,
        client=None,
        on_change: Callable[[Playback], None] | None = None,
    ) -> None:
        self.device = device
        self.server = server
        self.library = library
        self.client = client
        self.on_change = on_change or (lambda _state: None)
        self.queue = Queue()
        self.state = Playback()
        self._poller: asyncio.Task | None = None
        self._play_queue = None
        self._seen: tuple[str, int, bool, float] | None = None  # track id, offset, playing, when

    @property
    def controllable(self) -> bool:
        return self.client is not None

    def _require(self):
        if self.client is None:
            raise ReadOnly(f"{self.device.name} doesn't accept remote control: showing what it plays")
        return self.client

    async def start(self) -> None:
        self._poller = asyncio.create_task(self._poll())

    def _session(self):
        for item in self.server.sessions():
            player = getattr(item, "player", None)
            if player is not None and player.machineIdentifier == self.device.id and item.type == "track":
                return item, player
        return None, None

    async def _poll(self) -> None:
        while True:
            try:
                item, player = await asyncio.to_thread(self._session)
                self.state = self._state_from(item, player, time.monotonic())
                self.on_change(self.state)
            except Exception:  # a flaky network shouldn't take the UI down; try again next second
                pass
            await asyncio.sleep(self.POLL_S)

    def _state_from(self, item, player, now: float) -> Playback:
        if item is None:
            self._seen = None
            return Playback(volume=self.state.volume)
        track = self.library.track_from(item)
        offset = int(item.viewOffset or 0)
        playing = (player.state or "") == "playing"
        if self._seen is None or self._seen[:3] != (track.id, offset, playing):
            self._seen = (track.id, offset, playing, now)  # a new report: restart the clock
        position = offset + (int((now - self._seen[3]) * 1000) if playing else 0)
        position = min(position, track.duration_ms or position)
        index = next((i for i, t in enumerate(self.queue.tracks) if t.id == track.id), -1)
        return Playback(
            track=track,
            position_ms=position,
            duration_ms=track.duration_ms,
            paused=not playing,
            volume=self.state.volume,
            index=index,
        )

    async def load(self, queue: Queue) -> None:
        """Hand the player a new play queue: a window of tracks around the current one."""
        from plexapi.playqueue import PlayQueue

        client = self._require()
        self.queue = queue
        if queue.track is None:
            await self.stop()
            return
        start = max(0, queue.current - REMOTE_WINDOW // 4)
        window = queue.tracks[start : start + REMOTE_WINDOW]

        def begin() -> None:
            items = self.server.fetchItems([int(t.id) for t in window])
            by_id = {str(i.ratingKey): i for i in items}
            ordered = [by_id[t.id] for t in window if t.id in by_id]
            first = by_id.get(queue.track.id, ordered[0])
            self._play_queue = PlayQueue.create(self.server, ordered, startItem=first)
            client.playMedia(self._play_queue)

        await asyncio.to_thread(begin)

    async def queue_changed(self, queue: Queue) -> None:
        """Additions go into the player's own play queue; nothing restarts."""
        old = self.queue
        self.queue = queue
        if self.client is None or self._play_queue is None:
            return  # following only, or we didn't start this queue
        known = {t.id for t in old.tracks}
        added = [t for t in queue.tracks if t.id not in known]
        if not added:
            return
        play_next = tuple(added) == queue.tracks[queue.current + 1 : queue.current + 1 + len(added)]

        def push() -> None:
            for t in reversed(added) if play_next else added:
                self._play_queue.addItem(self.server.fetchItem(int(t.id)), playNext=play_next, refresh=False)
            self.client.refreshPlayQueue(self._play_queue.playQueueID)

        await asyncio.to_thread(push)

    async def skip(self, step: int) -> None:
        client = self._require()
        await asyncio.to_thread(client.skipNext if step > 0 else client.skipPrevious, "music")

    async def toggle(self) -> None:
        client = self._require()
        await asyncio.to_thread(client.play if self.state.paused else client.pause, "music")

    async def seek(self, position_ms: int) -> None:
        await asyncio.to_thread(self._require().seekTo, max(0, position_ms), "music")

    async def set_volume(self, volume: int) -> None:
        volume = max(0, min(100, volume))
        await asyncio.to_thread(self._require().setVolume, volume, "music")
        self.state = replace(self.state, volume=volume)

    async def set_repeat(self, mode: str) -> None:
        if self.client is not None:
            await asyncio.to_thread(self.client.setRepeat, {"off": 0, "one": 1, "all": 2}[mode], "music")

    async def stop(self) -> None:
        if self.client is not None:
            await asyncio.to_thread(self.client.stop, "music")

    async def close(self) -> None:
        if self._poller:
            self._poller.cancel()


FollowOutput = RemoteOutput  # a RemoteOutput without a client only follows
