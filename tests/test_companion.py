"""The Companion client and RemoteOutput against a stub player that speaks the protocol."""

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from tamp.companion import Companion
from tamp.demo import DemoLibrary
from tamp.models import Device
from tamp.output import ReadOnly, RemoteOutput
from tamp.queue import Queue

PLAYER_ID = "phone-1"


class StubPlayer:
    """Answers like Plexamp: /resources, /player/playback/* (plain OK), /player/timeline/poll."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict, dict]] = []
        self.state = "playing"
        self.rating_key = ""
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None: ...

            def do_GET(self) -> None:
                url = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                stub.requests.append((url.path, query, dict(self.headers)))
                if url.path == "/resources":
                    body = f'<MediaContainer><Player title="iPhone" machineIdentifier="{PLAYER_ID}" product="Plexamp"/></MediaContainer>'
                elif url.path == "/player/timeline/poll":
                    body = (
                        f'<MediaContainer><Timeline type="music" state="{stub.state}" time="12000" duration="200000" '
                        f'ratingKey="{stub.rating_key}" playQueueID="7" volume="80" shuffle="0" repeat="0"/></MediaContainer>'
                    )
                elif url.path.startswith("/player/playback/"):
                    if url.path.endswith("/pause"):
                        stub.state = "paused"
                    elif url.path.endswith("/createPlayQueue"):
                        stub.rating_key = query["key"].rsplit("/", 1)[-1]
                    body = "OK"
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body.encode())

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture
def stub():
    player = StubPlayer()
    yield player
    player.close()


FAKE_SERVER = SimpleNamespace(_baseurl="http://192.168.1.10:32400", machineIdentifier="pms-1", _token="srv")


def test_commands_carry_the_protocol_headers_and_increasing_ids(stub) -> None:
    c = Companion(stub.url, PLAYER_ID, "tamp-client", "secret")
    c.command("pause")
    c.seek(30_000)
    c.set_parameters(volume=40)
    ids = [int(q["commandID"]) for _, q, _ in stub.requests]
    assert ids == [1, 2, 3]
    path, query, hdrs = stub.requests[1]
    assert path == "/player/playback/seekTo" and query["offset"] == "30000" and query["type"] == "music"
    assert hdrs["X-Plex-Target-Client-Identifier"] == PLAYER_ID
    assert hdrs["X-Plex-Client-Identifier"] == "tamp-client"
    assert hdrs["X-Plex-Token"] == "secret"


def test_create_play_queue_names_the_server_and_start(stub) -> None:
    Companion(stub.url, PLAYER_ID, "c", "t").play_queue(FAKE_SERVER, ["10", "11", "12"], start_key="11")
    path, q, _ = stub.requests[-1]
    assert path == "/player/playback/createPlayQueue"
    assert q["uri"] == "server://pms-1/com.plexapp.plugins.library/library/metadata/10,11,12"
    assert q["key"] == "/library/metadata/11"
    assert (q["machineIdentifier"], q["address"], q["port"], q["protocol"]) == (
        "pms-1",
        "192.168.1.10",
        "32400",
        "http",
    )


def test_timeline_and_identity(stub) -> None:
    c = Companion(stub.url, PLAYER_ID, "c", "t")
    stub.rating_key = "42"
    t = c.timeline()
    assert t and (t.state, t.time_ms, t.rating_key, t.play_queue_id, t.volume) == (
        "playing",
        12000,
        "42",
        "7",
        80,
    )
    assert c.identity() == PLAYER_ID
    assert Companion("http://127.0.0.1:9", PLAYER_ID, "c", "t", timeout=0.5).identity() is None


def test_untrusted_address_never_gets_the_token(stub) -> None:
    from tamp.plex import _companion

    assert _companion(stub.url, PLAYER_ID, "c", "secret", trusted=False) is not None
    assert all(h.get("X-Plex-Token", "") == "" for p, _, h in stub.requests if p == "/resources")
    assert _companion(stub.url, "someone-else", "c", "secret", trusted=True) is None  # wrong identity


async def test_remote_output_plays_follows_and_pauses(stub) -> None:
    lib = DemoLibrary()
    tracks = [t for h in lib.home() if h.kind == "track" for t in h.items][:3]
    states = []
    out = RemoteOutput(
        Device(PLAYER_ID, "iPhone"), FAKE_SERVER, lib, Companion(stub.url, PLAYER_ID, "c", "t"), states.append
    )
    await out.load(Queue().play(tracks, 1))
    state = await asyncio.to_thread(out._read_state, 0.0)
    assert state.track and state.track.id == tracks[1].id and not state.paused
    out.state = state
    await out.toggle()
    assert stub.state == "paused"


async def test_follow_only_output_says_why() -> None:
    out = RemoteOutput(Device(PLAYER_ID, "iPhone"), FAKE_SERVER, DemoLibrary())
    with pytest.raises(ReadOnly, match="isn't reachable"):
        await out.toggle()
