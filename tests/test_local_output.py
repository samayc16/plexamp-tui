"""LocalOutput against a stand-in mpv that records commands, and the real mpv where the
version matters (skipped without it)."""

import asyncio
import shutil

import pytest

from tamp.demo import DemoLibrary
from tamp.models import Stream, Track
from tamp.mpv import Mpv, parse_version
from tamp.output import LocalOutput
from tamp.player import Player
from tamp.queue import Queue


def tracks(n: int) -> list[Track]:
    return [Track(id=str(i), title=f"t{i}", artist="a", album="b", duration_ms=10_000) for i in range(n)]


class GainLibrary(DemoLibrary):
    def stream(self, track: Track) -> Stream:
        return Stream(url=f"tone://{track.id}", gain_db=-3.0)


class FakeMpv:
    def __init__(self, version: tuple[int, int] | None) -> None:
        self.version = version
        self.commands: list[tuple] = []

    async def start(self) -> None: ...

    async def command(self, *args):
        self.commands.append(args)

    async def set(self, prop: str, value) -> None: ...

    def loadfiles(self) -> list[tuple]:
        return [c for c in self.commands if c[0] == "loadfile"]


def _output(mpv: FakeMpv, errors: list[str] | None = None, library=None) -> LocalOutput:
    errors = [] if errors is None else errors
    return LocalOutput(
        library or GainLibrary(), mpv, lambda _s: None, lambda: None, lambda _t: None, on_error=errors.append
    )


@pytest.mark.parametrize(
    ("text", "version"),
    [
        ("mpv 0.35.1", (0, 35)),
        ("mpv 0.37.0", (0, 37)),
        ("mpv v0.42.0", (0, 42)),
        ("mpv v0.38.0-458-g1234abcd", (0, 38)),
        ("mpv 0.37.0+git20240101", (0, 37)),
        ("something else", None),
        (None, None),
    ],
)
def test_mpv_versions(text, version) -> None:
    assert parse_version(text) == version


async def test_mpv_0_38_gets_the_gain_as_a_per_file_option() -> None:
    mpv = FakeMpv((0, 38))
    await _output(mpv).load(Queue().play(tracks(2), 0))
    assert mpv.loadfiles() == [
        ("loadfile", "tone://0", "replace", -1, {"volume-gain": "-3.00"}),
        ("loadfile", "tone://1", "append", -1, {"volume-gain": "-3.00"}),
    ]


async def test_older_mpv_plays_without_leveling_and_says_so_once() -> None:
    mpv, errors = FakeMpv((0, 37)), []
    out = _output(mpv, errors)
    await out.load(Queue().play(tracks(3), 0))
    await out.load(Queue().play(tracks(3), 1))
    assert {c[2:] for c in mpv.loadfiles()} == {("replace",), ("append",)}  # no index, no options
    assert len(errors) == 1 and "0.38" in errors[0]


async def test_without_a_gain_every_mpv_gets_the_plain_form() -> None:
    mpv, errors = FakeMpv((0, 35)), []
    await _output(mpv, errors, library=DemoLibrary()).load(Queue().play(tracks(1), 0))
    assert mpv.loadfiles()[0][2:] == ("replace",) and errors == []


class ToneLibrary(DemoLibrary):
    def stream(self, track: Track) -> Stream:
        return Stream(url="av://lavfi:sine=frequency=330:duration=0.7,volume=0.05", gain_db=-3.0)


@pytest.mark.skipif(shutil.which("mpv") is None, reason="needs mpv")
async def test_this_mpv_plays_gapless_with_leveling_on(tmp_path) -> None:
    """Whatever mpv is installed (CI runs an old one too): leveled tracks still play through."""
    events: asyncio.Queue = asyncio.Queue()
    errors: list[str] = []
    player = Player(ToneLibrary(), lambda s, q: None)
    local: LocalOutput

    def on_advance() -> None:
        player.advanced()
        events.put_nowait(player.queue.current)

    mpv = Mpv(tmp_path / "mpv.sock", lambda msg: local.handle_event(msg), ("--ao=null",))
    local = LocalOutput(
        player.library, mpv, player.changed, on_advance, player.finished, on_error=errors.append
    )
    await player.use(local)
    await mpv.set("volume", 0)
    try:
        assert mpv.version is not None
        await player.play(tracks(3), 0)
        seen = [await asyncio.wait_for(events.get(), timeout=10) for _ in range(2)]
        assert seen == [1, 2]
        assert (errors == []) == (mpv.version >= (0, 38))
    finally:
        await local.close()


# ── a second tamp ──
async def test_a_second_tamp_leaves_the_first_ones_socket_and_pidfile(tmp_path, monkeypatch) -> None:
    import json
    import os
    import socket

    from tamp.mpv import AlreadyRunning, private_dir

    monkeypatch.setattr(Mpv, "available", staticmethod(lambda: True))
    second = Mpv(tmp_path / "mpv.sock", lambda _m: None)
    private_dir(second.socket.parent)
    first = socket.socket(socket.AF_UNIX)  # the first tamp's mpv, listening
    first.bind(str(second.socket))
    first.listen()
    pidfile = second.socket.with_suffix(".pid")
    pidfile.write_text(json.dumps({"mpv": 999_999_999, "owner": os.getpid()}))  # a live owner
    try:
        with pytest.raises(AlreadyRunning):
            await second.start()
        await second.quit()  # what quitting the second tamp does
        assert second.socket.exists() and pidfile.exists()
    finally:
        first.close()
        second.socket.unlink(missing_ok=True)
        pidfile.unlink(missing_ok=True)


async def test_a_player_that_does_not_own_the_state_files_leaves_them_alone() -> None:
    from tamp import config
    from tamp.models import Playback

    config.write_state(config.STATUS_FILE, {"pid": 1, "title": "the first tamp's"})
    config.QUEUE_FILE.unlink(missing_ok=True)
    player = Player(DemoLibrary(), lambda s, q: None, persist=False)
    player.output = None
    await player.play(tracks(2), 0)
    player.changed(Playback(track=player.queue.track, paused=False))
    player.clear_status()
    assert config.read_state(config.STATUS_FILE)["title"] == "the first tamp's"
    assert not config.QUEUE_FILE.exists()


async def test_the_app_stands_aside_when_another_tamp_runs(monkeypatch) -> None:
    from tamp import config
    from tamp.app import TampApp
    from tamp.mpv import AlreadyRunning

    async def refuse(self) -> None:
        raise AlreadyRunning("another tamp is already running (its mpv socket is in use)")

    monkeypatch.setattr(Mpv, "start", refuse)
    config.write_state(config.STATUS_FILE, {"pid": 1, "title": "the first tamp's"})
    app = TampApp(demo=True)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.5)
        assert app.player.persist is False
    assert config.read_state(config.STATUS_FILE)["title"] == "the first tamp's"


async def test_the_demo_never_writes_your_queue_or_status() -> None:
    from tamp import config
    from tamp.app import TampApp

    config.QUEUE_FILE.unlink(missing_ok=True)
    config.STATUS_FILE.unlink(missing_ok=True)
    app = TampApp(demo=True)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.3)
        await app.player.play(app.library.liked()[:2] or tracks(2), 0)
        await pilot.pause(0.3)
    assert not config.QUEUE_FILE.exists() and not config.STATUS_FILE.exists()


async def test_repeat_all_on_a_single_track_queues_it_again() -> None:
    mpv = FakeMpv((0, 38))
    out = _output(mpv, library=GainLibrary())
    await out.load(Queue(tracks=tuple(tracks(1)), current=0, repeat="all"))
    assert [c[1:3] for c in mpv.loadfiles()] == [("tone://0", "replace"), ("tone://0", "append")]
    await out.set_repeat("off")  # and off again: nothing after it
    assert mpv.commands[-1] == ("playlist-remove", 1)
