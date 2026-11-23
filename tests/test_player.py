"""Player logic against a fake output, and LocalOutput against a real mpv (skipped without it)."""

import asyncio
import shutil

import pytest

from tamp import config
from tamp.demo import DemoLibrary
from tamp.models import Device, Playback, Stream, Track
from tamp.mpv import Mpv
from tamp.output import LocalOutput, ReadOnly
from tamp.player import Player, status_line
from tamp.queue import Queue


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
    config.write_state(config.STATUS_FILE, {"pid": 999_999_999, "state": "playing", "title": "x", "artist": "y"})
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

    mpv = Mpv(tmp_path / "mpv.sock", lambda msg: local.handle_event(msg))
    local = LocalOutput(player.library, mpv, player.changed, on_advance, player.finished)
    await player.use(local)
    await mpv.set("volume", 0)  # silent test
    try:
        await player.play(tracks(2), 0)
        await asyncio.wait_for(advanced.wait(), timeout=10)
        assert player.queue.current == 1
    finally:
        await local.close()
