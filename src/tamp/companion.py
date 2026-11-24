"""Plex Companion: controlling a Plex / Plexamp player directly over the local network.

This is the protocol Plexamp's own "play on" uses between devices on a LAN. Targets: Plexamp
desktop (with Remote Control enabled in its settings), Plexamp headless / Caldera, Android,
and an iPhone while Plexamp is alive on the same Wi-Fi. Away from home there's no third-party
route (Plexamp relays that through its own cloud), so tamp falls back to following the
player through the server's sessions.

Every request carries the headers the protocol requires and an increasing commandID.
Blocking; call from a thread."""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass
from urllib.parse import urlparse
from xml.etree import ElementTree

import requests

from tamp import PRODUCT
from tamp.plex import headers as plex_headers

TIMEOUT = 4


class PlayerError(RuntimeError):
    """A player didn't answer. The message names the host and the kind of failure, never the
    URL: requests puts the whole URL, query string and all, in its errors."""


class Refused(PlayerError):
    """The player answered with an HTTP error status: it doesn't support that command."""


@dataclass(frozen=True)
class Timeline:
    state: str  # playing paused stopped buffering
    time_ms: int
    duration_ms: int
    rating_key: str
    play_queue_id: str
    volume: int
    shuffle: bool
    repeat: int  # 0 off, 1 one, 2 all
    play_queue_version: int = 0  # bumps when the queue's contents change


class Companion:
    def __init__(
        self, base_url: str, target_id: str, client_id: str, token: str, timeout: float = TIMEOUT
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.host = urlparse(self.base_url).hostname or self.base_url
        self.target_id = target_id
        self.client_id = client_id
        self.token = token
        self.timeout = timeout
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._session = requests.Session()

    def _headers(self) -> dict[str, str]:
        return {
            **plex_headers(self.client_id, self.token),
            "Accept": "application/xml",
            "X-Plex-Target-Client-Identifier": self.target_id,
            "X-Plex-Device-Name": PRODUCT,
        }

    def _get(self, path: str, **params: object) -> requests.Response:
        with self._lock:
            command_id = next(self._ids)
        try:
            r = self._session.get(
                self.base_url + path,
                params={**params, "commandID": command_id},
                headers=self._headers(),
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise PlayerError(f"no answer from {self.host} ({type(e).__name__})") from None
        if not r.ok:
            raise Refused(f"{self.host} refused {path.rsplit('/', 1)[-1]} (HTTP {r.status_code})")
        return r

    # ── who's there ──
    def identity(self) -> str | None:
        """The player's machineIdentifier, or None if nothing answers like a Plex player."""
        try:
            r = self._session.get(self.base_url + "/resources", headers=self._headers(), timeout=self.timeout)
            player = ElementTree.fromstring(r.content).find("Player")
        except (requests.RequestException, ElementTree.ParseError):
            return None
        return player.get("machineIdentifier") if player is not None else None

    # ── playback ──
    def play_queue(self, server, rating_keys: list[str], start_key: str, shuffle: bool = False) -> None:
        """Hand the player a list of tracks starting at `start_key`; it builds the play queue on
        the server itself (Plexamp's preferred way). Only a player that answers that it can't
        (an HTTP error) gets the older playMedia: after a timeout the queue may well have
        arrived, and a second one would replace it."""
        location = urlparse(server._baseurl)
        where = {
            "machineIdentifier": server.machineIdentifier,
            "protocol": location.scheme,
            "address": location.hostname,
            "port": location.port or (443 if location.scheme == "https" else 32400),
        }
        uri = f"server://{server.machineIdentifier}/com.plexapp.plugins.library/library/metadata/{','.join(rating_keys)}"
        try:
            self._get(
                "/player/playback/createPlayQueue",
                uri=uri,
                key=f"/library/metadata/{start_key}",
                type="audio",
                shuffle=int(shuffle),
                **where,
            )
            return
        except Refused:
            pass  # an older player (a TV, say): give it a play queue made on the server
        from plexapi.playqueue import PlayQueue

        items = server.fetchItems([int(k) for k in rating_keys])
        start = next((i for i in items if str(i.ratingKey) == start_key), items[0])
        pq = PlayQueue.create(server, items, startItem=start, shuffle=int(shuffle))
        params = {
            "key": f"/library/metadata/{start_key}",
            "containerKey": f"/playQueues/{pq.playQueueID}?window=100&own=1",
            "type": "music",
            **where,
        }
        # The protocol hands the player a token for the server in the query string. Like Plex's
        # own apps, send a short-lived one made for this, never yours, and only to a player that
        # may have the token at all.
        if self.token and (transient := server.createToken()):
            params["token"] = transient
        self._get("/player/playback/playMedia", **params)

    def refresh_play_queue(self, play_queue_id: str) -> None:
        self._get("/player/playback/refreshPlayQueue", playQueueID=play_queue_id, type="music")

    def command(self, name: str) -> None:
        """play pause stop skipNext skipPrevious"""
        self._get(f"/player/playback/{name}", type="music")

    def seek(self, offset_ms: int) -> None:
        self._get("/player/playback/seekTo", offset=max(0, offset_ms), type="music")

    def set_parameters(self, **params: int) -> None:
        """volume=0-100, shuffle=0|1, repeat=0|1|2"""
        self._get("/player/playback/setParameters", type="music", **params)

    def timeline(self, wait: bool = False) -> Timeline | None:
        r = self._get("/player/timeline/poll", wait=int(wait), includeMetadata=1, type="music")
        for t in ElementTree.fromstring(r.content).iter("Timeline"):
            if t.get("type") != "music":
                continue
            return Timeline(
                state=t.get("state", "stopped"),
                time_ms=int(t.get("time") or 0),
                duration_ms=int(t.get("duration") or 0),
                rating_key=t.get("ratingKey", ""),
                play_queue_id=t.get("playQueueID", ""),
                volume=int(t.get("volume") or 100),
                shuffle=t.get("shuffle") == "1",
                repeat=int(t.get("repeat") or 0),
                play_queue_version=int(t.get("playQueueVersion") or 0),
            )
        return None
