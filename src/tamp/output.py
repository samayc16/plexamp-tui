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

from tamp.companion import Timeline
from tamp.errors import describe
from tamp.library import Library
from tamp.models import THIS_DEVICE, Device, Playback, Stream, Track
from tamp.mpv import Mpv, MpvError
from tamp.queue import Queue

REMOTE_WINDOW = 100  # tracks handed to a remote player around the current one
PER_FILE_GAIN = (0, 38)  # the first mpv with volume-gain, set per track through loadfile


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
            self.on_error(describe(exc))

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
        self._said_no_gain = False

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
        if not self.loudness or stream.gain_db is None:
            await self.mpv.command("loadfile", stream.url, mode)
        elif self.mpv.version is not None and self.mpv.version < PER_FILE_GAIN:
            # volume-gain, and loadfile's index argument before the options, came in mpv 0.38
            if not self._said_no_gain:
                self._said_no_gain = True
                have = ".".join(map(str, self.mpv.version))
                self.on_error(f"loudness leveling needs mpv 0.38 or newer (this is {have})")
            await self.mpv.command("loadfile", stream.url, mode)
        else:
            gain = {"volume-gain": f"{stream.gain_db:.2f}"}
            await self.mpv.command("loadfile", stream.url, mode, -1, gain)

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
                self.on_error(f"couldn't queue {nxt.title}: {describe(e)}")
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

    With a Companion client (the player is on this network and accepts remote control) its own
    timeline says what it's playing and every control works. Without one, or when it stops
    answering (your phone left the Wi-Fi), the server's session list still says what it plays:
    every Plex player reports there. Between reports the position is extrapolated so synced
    lyrics stay in time."""

    POLL_S = 1.0
    HANDOFF_S = 15.0  # how long a player may take to move to a queue tamp handed it

    def __init__(
        self,
        device: Device,
        server,
        library,
        companion=None,
        on_change: Callable[[Playback], None] | None = None,
    ) -> None:
        self.device = device
        self.server = server
        self.library = library
        self.companion = companion
        self.on_change = on_change or (lambda _state: None)
        self.queue = Queue()
        self.state = Playback()
        self.play_queue_id = ""
        self._poller: asyncio.Task | None = None
        self._tracks: dict[str, Track] = {}
        self._seen: tuple[str, int, bool, float] | None = None  # track id, offset, playing, when
        # the player's own queue, shown when it isn't one tamp handed it (you started it on the phone)
        self.on_queue: Callable[[Queue], None] = lambda _queue: None
        self._own_pq = ""  # the play queue made from tamp's queue: its tracks are already known
        self._pq_seen: tuple[str, int] = ("", -1)
        # after load(): the play queue the player had, and until when to wait for it to move on
        self._handoff: tuple[str, float] | None = None
        self._loads = 0  # counts load() calls: a queue fetched across one is stale
        self._handed: frozenset[str] = frozenset()  # track ids in the window tamp last handed it

    @property
    def controllable(self) -> bool:
        return self.companion is not None

    def _require(self):
        if self.companion is None:
            raise ReadOnly(
                f"{self.device.name} isn't reachable for remote control (same Wi-Fi? Plexamp's "
                "Remote Control setting on?): showing what it plays"
            )
        return self.companion

    async def start(self) -> None:
        self._poller = asyncio.create_task(self._poll())

    async def _poll(self) -> None:
        while True:
            try:
                await self._poll_once()
            except Exception:  # a flaky network shouldn't take the UI down; try again next second
                pass
            await asyncio.sleep(self.POLL_S)

    async def _poll_once(self, now: float | None = None) -> None:
        """One look at the player. Network calls run in threads; every decision about what to
        show is made here, on the event loop, so it can't race load()."""
        now = time.monotonic() if now is None else now
        timeline = await asyncio.to_thread(self._timeline)
        if timeline is not None:
            self.play_queue_id = timeline.play_queue_id or self.play_queue_id
            await self._follow_queue(timeline)
            key = timeline.rating_key
            track = await asyncio.to_thread(self._track, key) if key else None
            state = self._playback(track, timeline.time_ms, timeline.state == "playing", now, timeline.volume)
        else:
            item, player = await asyncio.to_thread(self._session)
            state = self._from_session(item, player, now)
        self.state = state
        self.on_change(state)

    def _timeline(self) -> Timeline | None:
        if self.companion is None:
            return None
        try:
            return self.companion.timeline()
        except Exception:
            return None  # gone quiet (asleep, off the Wi-Fi): fall back to the server

    async def _follow_queue(self, timeline: Timeline) -> None:
        """Pick up the player's play queue when it's someone else's: one started on the phone, or
        changed there."""
        pq_id = timeline.play_queue_id
        if not self._wants_queue(pq_id, timeline.play_queue_version, timeline.rating_key):
            return
        loads = self._loads
        queue = await asyncio.to_thread(self._fetch_queue, pq_id)
        if queue is None or loads != self._loads or self._handoff is not None:
            return  # tamp handed it a queue meanwhile: this one is on its way out
        self.queue = queue
        self.on_queue(queue)

    def _wants_queue(self, pq_id: str, version: int, rating_key: str) -> bool:
        if not pq_id:
            return False
        seen, self._pq_seen = self._pq_seen, (pq_id, version)
        if self._handoff is not None:
            before, until = self._handoff
            if pq_id != before:  # it moved to the queue tamp handed it
                self._own_pq, self._handoff = pq_id, None
                return False
            if time.monotonic() < until:
                return False  # still on its old queue: the hand-off hasn't landed yet
            self._handoff, seen = None, ("", -1)  # it never moved: follow what it really plays
        if pq_id == self._own_pq:
            return False
        playing_is_known = any(t.id == rating_key for t in self.queue.tracks)
        return (pq_id, version) != seen or not playing_is_known

    def _fetch_queue(self, pq_id: str) -> Queue | None:
        from plexapi.playqueue import PlayQueue

        try:  # own=False: read it, never take it over from the player
            pq = PlayQueue.get(self.server, int(pq_id), own=False, window=REMOTE_WINDOW // 2)
        except Exception:
            return None
        items = [item for item in pq.items if getattr(item, "type", "") == "track"]
        tracks = tuple(self.library.track_from(item) for item in items)
        self._tracks.update((t.id, t) for t in tracks)
        current = next(
            (i for i, item in enumerate(items) if item.playQueueItemID == pq.playQueueSelectedItemID), 0
        )
        return Queue(tracks=tracks, current=current if tracks else -1, source=self.device.name)

    def _track(self, rating_key: str) -> Track:
        if rating_key not in self._tracks:
            self._tracks[rating_key] = self.library.track(rating_key)
        return self._tracks[rating_key]

    def _session(self):
        for item in self.server.sessions():
            player = getattr(item, "player", None)
            if player is not None and player.machineIdentifier == self.device.id and item.type == "track":
                return item, player
        return None, None

    def _from_session(self, item, player, now: float) -> Playback:
        """What the server's session list says it plays (no network: the item has it all)."""
        if item is None:
            self._seen = None
            return Playback(volume=self.state.volume)
        track = self.library.track_from(item)
        return self._playback(
            track, int(item.viewOffset or 0), (player.state or "") == "playing", now, self.state.volume
        )

    def _playback(self, track: Track | None, offset: int, playing: bool, now: float, volume: int) -> Playback:
        if track is None:
            return Playback(volume=volume)
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
            volume=volume,
            index=index,
        )

    async def load(self, queue: Queue) -> None:
        """Hand the player a window of tracks around the current one; it builds the queue."""
        companion = self._require()
        self.queue = queue
        if queue.track is None:
            await self.stop()
            return
        self._loads += 1
        before = self._pq_seen[0]
        if not before:  # not polled yet: learn which queue it has, so that one isn't taken for ours
            timeline = await asyncio.to_thread(self._timeline)
            before = timeline.play_queue_id if timeline else ""
        self._handoff = (before, time.monotonic() + self.HANDOFF_S)
        start = max(0, queue.current - REMOTE_WINDOW // 4)
        window = [t.id for t in queue.tracks[start : start + REMOTE_WINDOW]]
        self._handed = frozenset(window)
        try:
            await asyncio.to_thread(companion.play_queue, self.server, window, queue.track.id)
        except Exception:
            self._handoff = None  # it kept its own queue: go on following that
            raise

    async def queue_changed(self, queue: Queue) -> None:
        """Only additions (add) and jumps (jump) reach a remote player; the Player refuses
        other edits there, so there's nothing to send."""
        self.queue = queue

    async def jump(self, queue: Queue) -> None:
        """Play queue.track from the queue the player already has (skipTo), keeping that queue
        whole: a playlist stays long, a station keeps going."""
        companion = self._require()
        if queue.track is None:
            return
        ours = bool(self._own_pq) and self.play_queue_id == self._own_pq
        if not self.play_queue_id or (ours and queue.track.id not in self._handed):
            await self.load(queue)  # nothing there, or past tamp's own window: hand over a new one
            return
        await asyncio.to_thread(companion.skip_to, queue.track.id)
        self.queue = queue

    async def add(self, queue: Queue, tracks: list[Track], play_next: bool) -> None:
        """Add tracks to the player's play queue on the server, at the front or the end of its
        Up Next, as the Player did to `queue`; nothing restarts. Each track is added as itself,
        so one already in the queue is added again. Read with own=0: never taken over."""
        companion = self._require()
        pq_id = self.play_queue_id
        if not pq_id:
            raise ReadOnly(f"{self.device.name} has nothing queued to add to: play something first")

        def push() -> None:
            from plexapi.playqueue import PlayQueue

            pq = PlayQueue.get(self.server, int(pq_id), own=False)
            for t in reversed(tracks) if play_next else tracks:  # play next: each goes to the front
                item = self.server.fetchItem(f"/library/metadata/{t.id}")
                pq.addItem(item, playNext=play_next, refresh=False)
            companion.refresh_play_queue(pq_id)

        await asyncio.to_thread(push)
        self.queue = queue

    async def skip(self, step: int) -> None:
        await asyncio.to_thread(self._require().command, "skipNext" if step > 0 else "skipPrevious")

    async def toggle(self) -> None:
        await asyncio.to_thread(self._require().command, "play" if self.state.paused else "pause")

    async def seek(self, position_ms: int) -> None:
        await asyncio.to_thread(self._require().seek, max(0, position_ms))

    async def set_volume(self, volume: int) -> None:
        volume = max(0, min(100, volume))
        await asyncio.to_thread(self._require().set_parameters, volume=volume)
        self.state = replace(self.state, volume=volume)

    async def set_repeat(self, mode: str) -> None:
        if self.companion is not None:
            await asyncio.to_thread(
                self.companion.set_parameters, repeat={"off": 0, "one": 1, "all": 2}[mode]
            )

    async def stop(self) -> None:
        if self.companion is not None:
            await asyncio.to_thread(self.companion.command, "stop")

    async def close(self) -> None:
        if self._poller:
            self._poller.cancel()


FollowOutput = RemoteOutput  # a RemoteOutput without a client only follows
