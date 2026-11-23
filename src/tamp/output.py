"""Where the music plays.

LocalOutput: mpv on this computer. Its playlist only ever holds [current, next] so gapless
playback works while the app's Queue stays the single source of truth.
RemoteOutput: a Plex/Plexamp player elsewhere, driven with Plex's remote-control API.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Protocol

from tamp.library import Library
from tamp.models import Device, Playback, Stream, Track
from tamp.mpv import Mpv, MpvError
from tamp.queue import Queue


class Output(Protocol):
    device: Device

    async def start(self) -> None: ...
    async def load(self, queue: Queue) -> None: ...
    async def queue_changed(self, queue: Queue) -> None: ...
    async def toggle(self) -> None: ...
    async def seek(self, position_ms: int) -> None: ...
    async def set_volume(self, volume: int) -> None: ...
    async def set_repeat(self, mode: str) -> None: ...
    async def stop(self) -> None: ...
    async def close(self) -> None: ...


class LocalOutput:
    """mpv. Calls `on_change(playback)` on every state change and `on_advance()` when mpv moves
    to the next track on its own (gapless)."""

    def __init__(
        self,
        library: Library,
        mpv: Mpv,
        on_change: Callable[[Playback], None],
        on_advance: Callable[[], None],
        on_finished: Callable[[Track], None],
        loudness: bool = True,
    ) -> None:
        from tamp.models import THIS_DEVICE

        self.device = THIS_DEVICE
        self.library = library
        self.mpv = mpv
        self.on_change = on_change
        self.on_advance = on_advance
        self.on_finished = on_finished
        self.loudness = loudness
        self.queue = Queue()
        self.state = Playback()
        self._loaded: list[str] = []  # track ids in mpv's playlist, in order
        self._streams: dict[str, Stream] = {}
        self._headers: tuple[tuple[str, str], ...] | None = None

    async def start(self) -> None:
        await self.mpv.start()

    def handle_event(self, msg: dict) -> None:
        """Called by Mpv for every event (wired up by the app)."""
        event = msg.get("event")
        if event == "property-change":
            name, value = msg.get("name"), msg.get("data")
            s = self.state
            if name == "time-pos" and value is not None:
                self.state = _replace(s, position_ms=int(value * 1000))
            elif name == "duration" and value:
                self.state = _replace(s, duration_ms=int(value * 1000))
            elif name == "pause":
                self.state = _replace(s, paused=bool(value))
            elif name == "volume" and value is not None:
                self.state = _replace(s, volume=int(value))
            elif name == "playlist-pos" and value == 1 and len(self._loaded) > 1:
                self._loaded.pop(0)  # mpv moved on to the prefetched next track
                self.on_advance()
            elif name == "idle-active" and value:
                self.state = _replace(s, paused=True)
            else:
                return
            self.on_change(self.state)
        elif event == "end-file" and msg.get("reason") == "eof" and self.queue.track:
            self.on_finished(self.queue.track)

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
        """Start playing queue.track from the beginning, with the next one prefetched."""
        self.queue = queue
        self._loaded = []
        if queue.track is None:
            await self.stop()
            return
        self.state = Playback(track=queue.track, duration_ms=queue.track.duration_ms, paused=False, volume=self.state.volume, index=queue.current)
        self.on_change(self.state)
        await self._append(queue.track, "replace")
        self._loaded = [queue.track.id]
        await self.mpv.set("pause", False)
        await self._sync_next()

    async def queue_changed(self, queue: Queue) -> None:
        """The queue changed around the playing track: fix up what mpv plays next."""
        self.queue = queue
        if queue.track and self._loaded and queue.track.id != self._loaded[0]:
            await self.load(queue)  # the playing track itself changed
            return
        self.state = _replace(self.state, track=queue.track, index=queue.current)
        await self._sync_next()

    async def _sync_next(self) -> None:
        nxt = self.queue.advanced().track if self.queue.repeat != "one" else None
        if self.queue.advanced().current == self.queue.current:
            nxt = None  # end of the queue
        want = nxt.id if nxt else None
        have = self._loaded[1] if len(self._loaded) > 1 else None
        if want == have:
            return
        if have is not None:
            with contextlib.suppress(MpvError):
                await self.mpv.command("playlist-remove", 1)
            self._loaded = self._loaded[:1]
        if nxt is not None:
            await self._append(nxt, "append")
            self._loaded.append(nxt.id)

    @property
    def controllable(self) -> bool:
        return True

    async def skip(self, step: int) -> None:
        raise NotImplementedError  # the Player moves through its own queue for local playback

    async def toggle(self) -> None:
        await self.mpv.command("cycle", "pause")

    async def seek(self, position_ms: int) -> None:
        await self.mpv.command("seek", max(0, position_ms) / 1000, "absolute")

    async def set_volume(self, volume: int) -> None:
        await self.mpv.set("volume", max(0, min(100, volume)))

    async def set_repeat(self, mode: str) -> None:
        await self.mpv.set("loop-file", "inf" if mode == "one" else "no")
        await self._sync_next()

    async def stop(self) -> None:
        await self.mpv.command("stop")
        self._loaded = []
        self.state = _replace(self.state, track=None, paused=True, position_ms=0)
        self.on_change(self.state)

    async def close(self) -> None:
        await self.mpv.quit()


class ReadOnly(RuntimeError):
    """The player shows up in Plex but doesn't accept remote control."""


class RemoteOutput:
    """A Plex/Plexamp player somewhere else: your phone, a desktop or headless Plexamp, a TV.

    What it's playing comes from the server's session list, which every Plex player reports to
    (so it works for phones too); between reports the position is extrapolated so lyrics stay
    in sync. Controls go through plexapi's PlexClient when the player accepts remote control;
    without one the output is follow-only."""

    POLL_S = 1.0

    def __init__(self, device: Device, server, library, client=None, on_change: Callable[[Playback], None] | None = None) -> None:
        self.device = device
        self.server = server
        self.library = library
        self.client = client
        self.on_change = on_change or (lambda _state: None)
        self.queue = Queue()
        self.state = Playback()
        self._poller: asyncio.Task | None = None
        self._seen: tuple[str, int, float] | None = None  # (track id, reported offset, when we saw it)

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
            player = getattr(item, "player", None) or (item.players[0] if getattr(item, "players", None) else None)
            if player is not None and player.machineIdentifier == self.device.id and item.type == "track":
                return item, player
        return None, None

    async def _poll(self) -> None:
        import time

        while True:
            try:
                item, player = await asyncio.to_thread(self._session)
                now = time.monotonic()
                if item is None:
                    self.state = Playback(volume=self.state.volume)
                    self._seen = None
                else:
                    track = self.library.track_from(item)
                    offset = int(item.viewOffset or 0)
                    playing = (player.state or "") == "playing"
                    if self._seen is None or self._seen[:2] != (track.id, offset):
                        self._seen = (track.id, offset, now)
                    position = self._seen[1] + (int((now - self._seen[2]) * 1000) if playing else 0)
                    position = min(position, track.duration_ms or position)
                    index = next((i for i, t in enumerate(self.queue.tracks) if t.id == track.id), -1)
                    self.state = Playback(track=track, position_ms=position, duration_ms=track.duration_ms,
                                          paused=not playing, volume=self.state.volume, index=index)
                self.on_change(self.state)
            except Exception:  # a flaky network shouldn't take the UI down; try again next second
                pass
            await asyncio.sleep(self.POLL_S)

    async def load(self, queue: Queue) -> None:
        from plexapi.playqueue import PlayQueue

        client = self._require()
        self.queue = queue
        if queue.track is None:
            await self.stop()
            return

        def start() -> None:
            items = [self.server.fetchItem(int(t.id)) for t in queue.tracks[:200]]
            play_queue = PlayQueue.create(self.server, items, startItem=items[min(queue.current, len(items) - 1)])
            client.playMedia(play_queue)

        await asyncio.to_thread(start)

    async def queue_changed(self, queue: Queue) -> None:
        await self.load(queue)

    async def skip(self, step: int) -> None:
        client = self._require()
        await asyncio.to_thread(client.skipNext if step > 0 else client.skipPrevious, "music")

    async def toggle(self) -> None:
        client = self._require()
        await asyncio.to_thread(client.play if self.state.paused else client.pause, "music")

    async def seek(self, position_ms: int) -> None:
        await asyncio.to_thread(self._require().seekTo, max(0, position_ms), "music")

    async def set_volume(self, volume: int) -> None:
        await asyncio.to_thread(self._require().setVolume, max(0, min(100, volume)), "music")
        self.state = _replace(self.state, volume=max(0, min(100, volume)))

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


def _replace(state: Playback, **changes) -> Playback:
    from dataclasses import replace

    return replace(state, **changes)
