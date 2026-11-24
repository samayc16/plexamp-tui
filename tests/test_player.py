"""Player logic against a fake output, and LocalOutput against a real mpv (skipped without it)."""

import asyncio
import shutil

import pytest

from tamp import config
from tamp.demo import DemoLibrary
from tamp.models import Device, Playback, Stream, Track
from tamp.mpv import Mpv
from tamp.output import LocalOutput, ReadOnly, RemoteOutput
from tamp.player import Player
from tamp.queue import Queue
from tamp.status import line as status_line


@pytest.fixture(autouse=True)
def private_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "STATUS_FILE", tmp_path / "status.json")
    monkeypatch.setattr(config, "QUEUE_FILE", tmp_path / "queue.json")


class FakeOutput:
    device = Device(id="fake", name="Fake")
    controllable = True

    def __init__(self) -> None:
        self.loaded: list[Queue] = []
        self.calls: list[str] = []

    async def start(self) -> None: ...
    async def load(self, queue: Queue) -> None:
        self.loaded.append(queue)

    async def queue_changed(self, queue: Queue) -> None:
        self.calls.append("changed")

    async def toggle(self) -> None:
        self.calls.append("toggle")

    async def seek(self, position_ms: int) -> None:
        self.calls.append(f"seek {position_ms}")

    async def set_volume(self, volume: int) -> None: ...
    async def set_repeat(self, mode: str) -> None: ...
    async def stop(self) -> None: ...
    async def close(self) -> None: ...


class RefusingOutput(FakeOutput):
    async def toggle(self) -> None:
        raise ReadOnly("Phone doesn't accept remote control")


def tracks(n: int) -> list[Track]:
    return [Track(id=str(i), title=f"t{i}", artist="a", album="b", duration_ms=10_000) for i in range(n)]


async def test_play_next_and_prev() -> None:
    out, updates = FakeOutput(), []
    player = Player(DemoLibrary(), lambda s, q: updates.append(q))
    await player.use(out)
    await player.play(tracks(3), 0, source="test")
    assert out.loaded[-1].current == 0
    await player.next()
    assert out.loaded[-1].current == 1
    player.state = Playback(track=player.queue.track, position_ms=1000)
    await player.prev()  # within 3 s: previous track
    assert out.loaded[-1].current == 0
    player.state = Playback(track=player.queue.track, position_ms=8000)
    await player.prev()  # later: restart
    assert out.calls[-1] == "seek 0"
    assert updates


async def test_refusing_player_becomes_a_message() -> None:
    errors: list[str] = []
    player = Player(DemoLibrary(), lambda s, q: None, on_error=errors.append)
    await player.use(RefusingOutput())
    await player.play(tracks(1))
    player.state = Playback(track=player.queue.track)  # it's playing over there
    await player.toggle()
    assert errors == ["Phone doesn't accept remote control"]


async def test_replace_track_updates_queue() -> None:
    player = Player(DemoLibrary(), lambda s, q: None)
    await player.use(FakeOutput())
    await player.play(tracks(2))
    from dataclasses import replace

    liked = replace(player.queue.tracks[1], rating=10.0)
    player.replace_track(liked)
    assert player.queue.tracks[1].liked


async def test_queue_survives_a_restart() -> None:
    player = Player(DemoLibrary(), lambda s, q: None)
    await player.use(FakeOutput())
    await player.play(tracks(3), 2, source="saved")
    again = Player(DemoLibrary(), lambda s, q: None)
    again.restore_queue()
    assert again.queue.current == 2 and again.queue.source == "saved"


def test_status_line_empty_without_a_live_tamp() -> None:
    config.write_state(
        config.STATUS_FILE, {"pid": 999_999_999, "state": "playing", "title": "x", "artist": "y"}
    )
    assert status_line() == ""


class ToneLibrary(DemoLibrary):
    def stream(self, track: Track) -> Stream:
        return Stream(url="av://lavfi:sine=frequency=440:duration=1,volume=0.05")


@pytest.mark.skipif(shutil.which("mpv") is None, reason="needs mpv")
async def test_mpv_plays_gapless_into_the_next_track(tmp_path) -> None:
    advanced = asyncio.Event()
    player = Player(ToneLibrary(), lambda s, q: None)
    local: LocalOutput

    def on_advance() -> None:
        player.advanced()
        advanced.set()

    mpv = Mpv(
        tmp_path / "mpv.sock", lambda msg: local.handle_event(msg), ("--ao=null",)
    )  # no sound card needed
    local = LocalOutput(player.library, mpv, player.changed, on_advance, player.finished)
    await player.use(local)
    await mpv.set("volume", 0)  # silent test
    try:
        await player.play(tracks(2), 0)
        await asyncio.wait_for(advanced.wait(), timeout=10)
        assert player.queue.current == 1
    finally:
        await local.close()


class ShortToneLibrary(DemoLibrary):
    def stream(self, track: Track) -> Stream:
        return Stream(url="av://lavfi:sine=frequency=330:duration=0.7,volume=0.05")


async def _local(tmp_path, library) -> tuple[Player, LocalOutput, asyncio.Queue]:
    events: asyncio.Queue = asyncio.Queue()
    player = Player(library, lambda s, q: None)
    local: LocalOutput

    def on_advance() -> None:
        player.advanced()
        events.put_nowait(player.queue.current)

    mpv = Mpv(
        tmp_path / "mpv.sock", lambda msg: local.handle_event(msg), ("--ao=null",)
    )  # no sound card needed
    local = LocalOutput(player.library, mpv, player.changed, on_advance, player.finished)
    await player.use(local)
    await mpv.set("volume", 0)
    return player, local, events


@pytest.mark.skipif(shutil.which("mpv") is None, reason="needs mpv")
async def test_gapless_advances_through_every_track(tmp_path) -> None:
    player, local, events = await _local(tmp_path, ShortToneLibrary())
    try:
        await player.play(tracks(4), 0)
        seen = [await asyncio.wait_for(events.get(), timeout=10) for _ in range(3)]
        assert seen == [1, 2, 3]
        assert len(await local.mpv.get("playlist")) <= 2  # always [current, next]
    finally:
        await local.close()


@pytest.mark.skipif(shutil.which("mpv") is None, reason="needs mpv")
async def test_play_next_after_an_advance_keeps_the_playing_track(tmp_path) -> None:
    player, local, events = await _local(tmp_path, ShortToneLibrary())
    try:
        await player.play(tracks(4), 0)
        await asyncio.wait_for(events.get(), timeout=10)
        await asyncio.sleep(0.2)
        extra = Track(id="x", title="extra", artist="a", album="b", duration_ms=1000)
        await player.play_next([extra])
        playing = await local.mpv.get("playlist-pos")
        assert playing == 0 and player.queue.track and player.queue.track.id == "1"
        assert player.queue.tracks[2].id == "x"
    finally:
        await local.close()


@pytest.mark.skipif(shutil.which("mpv") is None, reason="needs mpv")
async def test_removing_the_only_track_stops(tmp_path) -> None:
    player, local, _ = await _local(tmp_path, ToneLibrary())
    try:
        await player.play(tracks(1), 0)
        await player.remove(0)
        assert await local.mpv.get("idle-active") is True
    finally:
        await local.close()


@pytest.mark.skipif(shutil.which("mpv") is None, reason="needs mpv")
async def test_mpv_restarts_after_a_crash(tmp_path) -> None:
    import os
    import signal

    player, local, _ = await _local(tmp_path, ToneLibrary())
    try:
        os.kill(local.mpv._process.pid, signal.SIGKILL)
        for _ in range(50):
            if not local.mpv.running:
                break
            await asyncio.sleep(0.05)
        assert not local.mpv.running
        await player.use(local)  # starts a fresh mpv
        await player.play(tracks(1), 0)
        assert await local.mpv.get("idle-active") is False
    finally:
        await local.close()


async def test_remote_position_ignores_time_spent_paused() -> None:
    from types import SimpleNamespace

    class Lib:
        def track_from(self, item):
            return Track(id="1", title="t", artist="a", album="b", duration_ms=300_000)

    phone = SimpleNamespace(machineIdentifier="phone", state="paused")
    item = SimpleNamespace(type="track", viewOffset=60_000, player=phone)
    server = SimpleNamespace(sessions=lambda: [item])  # no Companion: the server's sessions
    out = RemoteOutput(Device(id="phone", name="Phone"), server=server, library=Lib())
    await out._poll_once(now=100.0)
    assert out.state.position_ms == 60_000 and out.state.paused
    phone.state = "playing"  # resumed 2 s later; the server still reports the paused offset
    await out._poll_once(now=102.0)
    assert out.state.position_ms == 60_000
    await out._poll_once(now=103.5)
    assert out.state.position_ms == 61_500


async def test_a_private_session_reports_nothing() -> None:
    calls: list[str] = []

    class Spy(DemoLibrary):
        def report(self, track, state, position_ms):
            calls.append("report")

        def scrobble(self, track):
            calls.append("scrobble")

    player = Player(Spy(), lambda s, q: None, report=False)
    await player.use(FakeOutput())
    await player.play(tracks(1))
    t = player.queue.track
    player.changed(Playback(track=t, position_ms=9_500, duration_ms=10_000, paused=False))
    player.finished(t)
    await asyncio.sleep(0.05)
    assert calls == []


async def test_switching_output_shows_the_new_outputs_state_not_the_old_one() -> None:
    player = Player(DemoLibrary(), lambda s, q: None)
    phone, here = FakeOutput(), FakeOutput()
    phone.state = Playback(track=Track(id="1", title="t", artist="a", album="b", duration_ms=1000))
    here.state = Playback()
    await player.use(phone)
    assert player.state.track is not None
    await player.use(here)
    assert player.state.track is None  # not "playing" the phone's song on this computer


async def test_a_failing_player_shows_its_error_without_the_token() -> None:
    import requests

    class Leaky(FakeOutput):
        async def toggle(self) -> None:
            raise requests.ConnectionError("http://10.0.0.2:32500/player/playback/play?token=abc123")

    errors: list[str] = []
    player = Player(DemoLibrary(), lambda s, q: None, on_error=errors.append)
    await player.use(Leaky())
    await player.play(tracks(1))
    player.state = Playback(track=player.queue.track)
    await player.toggle()
    assert errors and "abc123" not in errors[0] and errors[0].startswith("Fake: ")


async def test_a_failing_action_shows_its_error_without_the_token() -> None:
    from tamp.ui.main import guarded

    class Screen:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def notify(self, message: str, **_kw) -> None:
            self.messages.append(message)

        @guarded
        async def act(self) -> None:
            raise RuntimeError("(401) unauthorized; http://h:32400/x?X-Plex-Token=abc123")

    screen = Screen()
    await screen.act()
    assert screen.messages and "abc123" not in screen.messages[0]
