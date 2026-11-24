"""A slow or failing server: quitting never waits for it, polling gives up quickly, and a
player tamp can't follow says so once instead of failing silently every second."""

import asyncio
import time
from types import SimpleNamespace

from tamp.background import DaemonExecutor
from tamp.library import QUICK_TIMEOUT, SERVER_TIMEOUT, PlexLibrary
from tamp.models import Device, Track
from tamp.output import RemoteOutput


def test_quitting_does_not_wait_for_a_call_still_in_flight() -> None:
    async def main() -> None:
        asyncio.get_running_loop().set_default_executor(DaemonExecutor())
        asyncio.get_running_loop().create_task(asyncio.to_thread(time.sleep, 5))  # a dead server
        await asyncio.sleep(0.1)

    started = time.monotonic()
    asyncio.run(main())  # asyncio.run joins the default executor's threads on the way out
    assert time.monotonic() - started < 2


async def test_the_daemon_executor_still_returns_results_and_errors() -> None:
    asyncio.get_running_loop().set_default_executor(DaemonExecutor())
    assert await asyncio.to_thread(sum, [1, 2, 3]) == 6
    try:
        await asyncio.to_thread(int, "x")
    except ValueError:
        pass
    else:
        raise AssertionError("the error should come back")


class Server:
    """Records the timeout in force for each call, like plexapi's server.query would use."""

    _timeout = 0
    friendlyName = "Home"

    def __init__(self, fail: Exception | None = None) -> None:
        self.seen: list[float] = []
        self.failing = [fail]  # shared with the quick-timeout copy, as a real connection is
        self.library = SimpleNamespace(
            sections=lambda: [SimpleNamespace(type="artist", key=1, title="Music")]
        )

    def sessions(self) -> list:
        self.seen.append(self._timeout)
        if self.failing[0]:
            raise self.failing[0]
        return []

    def fetchItem(self, key):
        self.seen.append(self._timeout)
        return SimpleNamespace(
            ratingKey=key, title="t", originalTitle=None, grandparentTitle="a", parentTitle="b",
            duration=1000, index=1, parentYear=None, year=None, parentThumb=None, thumb=None,
            userRating=None, media=[], parentRatingKey=2, grandparentRatingKey=3,
        )  # fmt: skip


class Lib:
    def track_from(self, item) -> Track:
        return Track(id="1", title="t", artist="a", album="b", duration_ms=1000)


async def test_polling_the_server_gives_up_quickly() -> None:
    server = Server()
    server._timeout = SERVER_TIMEOUT
    out = RemoteOutput(Device("phone", "Phone"), server, Lib())
    await out._poll_once()
    assert server.seen == [QUICK_TIMEOUT] and server._timeout == SERVER_TIMEOUT


def test_looking_up_a_track_gives_up_quickly_browsing_waits_longer() -> None:
    server = Server()
    library = PlexLibrary(server)
    library.track("5")
    assert server.seen == [QUICK_TIMEOUT] and server._timeout == SERVER_TIMEOUT


async def test_a_player_tamp_cant_follow_says_so_once() -> None:
    revoked = RuntimeError("(401) unauthorized; http://h:32400/status/sessions?X-Plex-Token=abc123")
    server = Server(fail=revoked)
    errors: list[str] = []
    out = RemoteOutput(Device("phone", "Phone"), server, Lib())
    out.on_error, out.POLL_S, out.FAILURES_BEFORE_SAYING = errors.append, 0.01, 3
    await out.start()
    try:
        await asyncio.sleep(0.3)
        assert len(errors) == 1 and "401" in errors[0] and "abc123" not in errors[0]
        server.failing[0] = None  # back again
        await asyncio.sleep(0.1)
        server.failing[0] = revoked  # and gone again: worth saying again
        await asyncio.sleep(0.3)
        assert len(errors) == 2
    finally:
        await out.close()
