"""The Companion client and RemoteOutput against a stub player that speaks the protocol."""

import asyncio
import threading
import time
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
        self.delay: dict[str, float] = {}  # path -> seconds to wait before answering
        self.refuse: set[str] = set()  # paths answered with HTTP 400, like a player without them
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None: ...

            def do_GET(self) -> None:
                url = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                stub.requests.append((url.path, query, dict(self.headers)))
                time.sleep(stub.delay.get(url.path, 0))
                if url.path in stub.refuse:
                    self.send_response(400)
                    self.end_headers()
                    return
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


def _tokens_sent(stub) -> list[str]:
    return [p for p, _, h in stub.requests if h.get("X-Plex-Token")]


def _local(_host: str) -> bool:
    return True


def _elsewhere(_host: str) -> bool:
    return False


def test_untrusted_address_never_gets_the_token(stub) -> None:
    """A player found only by the LAN broadcast (GDM), at an address plex.tv doesn't list for
    that device, can claim any identity: it never gets the token, not even after the probe."""
    from tamp.plex import _companion

    c = _companion(stub.url, PLAYER_ID, "c", "secret", listed=False, local=_local)
    assert c is not None
    c.timeline()
    c.command("pause")
    assert len(stub.requests) == 3 and _tokens_sent(stub) == []


def test_every_probe_goes_without_the_token(stub) -> None:
    from tamp.plex import _companion

    assert _companion(stub.url, "someone-else", "c", "secret", listed=True, local=_local) is None
    assert stub.requests and _tokens_sent(stub) == []  # whatever answers there learns nothing


def test_the_token_follows_only_a_matching_identity_on_this_network(stub) -> None:
    from tamp.plex import _companion

    c = _companion(stub.url, PLAYER_ID, "c", "secret", listed=True, local=_local)
    assert c is not None
    c.command("pause")
    assert _tokens_sent(stub) == ["/player/playback/pause"]  # the probe before it went without


def test_a_listed_address_off_this_network_never_gets_the_token(stub) -> None:
    """plex.tv lists your devices' home-LAN addresses; on a café network the same address is
    someone else's machine, so the token only goes where this computer is directly attached."""
    from tamp.plex import _companion

    c = _companion(stub.url, PLAYER_ID, "c", "secret", listed=True, local=_elsewhere)
    assert c is not None
    c.timeline()
    assert _tokens_sent(stub) == []


@pytest.mark.parametrize(
    ("host", "source", "local"),
    [
        ("127.0.0.1", None, True),
        ("::1", None, True),
        ("192.168.1.20", "192.168.1.57", True),
        ("192.168.1.20", "192.168.7.57", False),  # routed elsewhere: another network
        ("192.168.1.20", "10.0.0.8", False),
        ("192.168.1.20", None, False),  # no route at all
        ("fd00:1:2:3::20", "fd00:1:2:3::99", True),
        ("fd00:1:2:3::20", "fd00:1:2:4::99", False),
        ("203.0.113.9", "192.168.1.57", False),
        ("phone.local", "192.168.1.57", False),  # a name can point somewhere else later
    ],
)
def test_on_local_network(host: str, source: str | None, local: bool) -> None:
    from tamp.plex import on_local_network

    assert on_local_network(host, source=lambda _h: source) is local


def test_a_gdm_address_plex_tv_lists_for_the_device_may_have_the_token(stub, monkeypatch) -> None:
    from tamp import plex
    from tamp.models import Device

    monkeypatch.setattr(plex, "LOCAL_PLAYER", "http://127.0.0.1:9")  # never a real Plexamp here
    monkeypatch.setattr(plex, "on_local_network", _local)
    device = Device(PLAYER_ID, "iPhone")
    # plex.tv's addresses for the phone don't answer; the broadcast finds it at the stub
    listed = [plex.PlayerInfo(device, ("http://localhost:9",))]
    monkeypatch.setattr(plex, "players", lambda token: listed)
    monkeypatch.setattr(plex, "gdm_players", lambda: {PLAYER_ID: plex.PlayerInfo(device, (stub.url,))})
    out = plex.output_for(PLAYER_ID, "c", "secret", server=None, library=None)
    assert out.companion is not None and out.companion.token == ""  # an address only GDM vouches for

    listed[0] = plex.PlayerInfo(device, ("http://127.0.0.1:9",))  # the stub's address, another port
    out = plex.output_for(PLAYER_ID, "c", "secret", server=None, library=None)
    assert out.companion is not None and out.companion.token == "secret"


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


# ── the token stays out of URLs and messages ──
def _server(**extra) -> SimpleNamespace:
    return SimpleNamespace(
        _baseurl="http://192.168.1.10:32400",
        machineIdentifier="pms-1",
        _token="server-token",
        fetchItems=lambda keys: [SimpleNamespace(ratingKey=k) for k in keys],
        **extra,
    )


@pytest.fixture
def made_play_queues(monkeypatch):
    import plexapi.playqueue

    made: list[list] = []

    def create(server, items, startItem=None, shuffle=0):
        made.append(items)
        return SimpleNamespace(playQueueID=99)

    monkeypatch.setattr(plexapi.playqueue.PlayQueue, "create", staticmethod(create))
    return made


def _every_value(stub) -> str:
    return " ".join(f"{p} {q} {h}" for p, q, h in stub.requests if p != "/resources")


def test_a_slow_player_gets_no_second_queue_and_the_error_shows_no_url(stub, made_play_queues) -> None:
    from tamp.companion import PlayerError

    stub.delay["/player/playback/createPlayQueue"] = 1.0
    c = Companion(stub.url, PLAYER_ID, "c", "secret", timeout=0.3)
    with pytest.raises(PlayerError) as raised:
        c.play_queue(_server(createToken=lambda: "transient-1"), ["10", "11"], "11")
    assert [p for p, _, _ in stub.requests] == ["/player/playback/createPlayQueue"]
    assert made_play_queues == []  # nothing was queued a second time
    message = str(raised.value)
    assert "127.0.0.1" in message and "Timeout" in message
    assert "http" not in message and "secret" not in message and "token" not in message.lower()


def test_an_unreachable_player_error_names_the_host_not_the_url() -> None:
    from tamp.companion import PlayerError

    c = Companion("http://127.0.0.1:9", PLAYER_ID, "c", "secret", timeout=0.5)
    with pytest.raises(PlayerError) as raised:
        c.play_queue(_server(), ["10"], "10")
    assert str(raised.value) == "no answer from 127.0.0.1 (ConnectionError)"


def test_a_player_without_create_play_queue_gets_play_media_and_a_transient_token(
    stub, made_play_queues
) -> None:
    stub.refuse.add("/player/playback/createPlayQueue")
    Companion(stub.url, PLAYER_ID, "c", "secret").play_queue(
        _server(createToken=lambda: "transient-1"), ["10", "11"], "11"
    )
    path, query, _ = stub.requests[-1]
    assert path == "/player/playback/playMedia"
    assert query["containerKey"].startswith("/playQueues/99?") and query["key"] == "/library/metadata/11"
    assert query["token"] == "transient-1"  # what Plex's own apps send: short-lived, for this server
    assert "server-token" not in _every_value(stub)


def test_a_player_that_may_not_have_the_token_gets_none_in_play_media(stub, made_play_queues) -> None:
    stub.refuse.add("/player/playback/createPlayQueue")
    Companion(stub.url, PLAYER_ID, "c", "").play_queue(
        _server(createToken=lambda: "transient-1"), ["10"], "10"
    )
    path, query, headers = stub.requests[-1]
    assert path == "/player/playback/playMedia" and "token" not in query
    assert "X-Plex-Token" not in headers


@pytest.mark.parametrize(
    "text",
    [
        "GET http://10.0.0.2:32500/player/playback/playMedia?key=1&token=abc123&commandID=2",
        "(401) unauthorized; http://10.0.0.2:32400/library?X-Plex-Token=abc123 ",
        "{'X-Plex-Token': 'abc123', 'Accept': 'application/xml'}",
        "X-Plex-Token: abc123",
        '{"authToken":"abc123"}',
        "/photo/:/transcode?url=%2Fa%3FX-Plex-Token%3Dabc123%26b%3D1",
    ],
)
def test_error_text_never_shows_a_token(text: str) -> None:
    from tamp.errors import redact

    assert "abc123" not in redact(text)


def test_redaction_leaves_ordinary_text_alone() -> None:
    from tamp.errors import redact

    for text in ("Song [Live]", "no answer from 10.0.0.2 (ConnectTimeout)", "tokens: 3 left"):
        assert redact(text) == text
