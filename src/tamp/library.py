"""The music library the UI browses. `Library` is the interface; `PlexLibrary` talks to a Plex
Media Server, `tamp.demo.DemoLibrary` is an offline stand-in for tests and screenshots.
All methods block on the network; call them from a thread."""

from __future__ import annotations

import copy
import hashlib
import random
from typing import Protocol
from xml.etree import ElementTree

from plexapi.server import PlexServer

from tamp import config, lrc
from tamp.models import Album, Artist, ArtistOverview, Hub, Playlist, SearchResults, Stream, Track


class Library(Protocol):
    name: str

    def home(self) -> list[Hub]: ...
    def album(self, album_id: str) -> Album: ...
    def artist(self, artist_id: str) -> Artist: ...
    def liked(self) -> list[Track]: ...
    def artist_overview(self, artist: Artist) -> ArtistOverview: ...
    def details(self, track: Track) -> list[tuple[str, str]]: ...
    def artists(self, start: int = 0) -> list[Artist]: ...
    def albums(self, artist: Artist | None = None, start: int = 0) -> list[Album]: ...
    def tracks(self, album: Album) -> list[Track]: ...
    def artist_tracks(self, artist: Artist) -> list[Track]: ...
    def playlists(self) -> list[Playlist]: ...
    def playlist_tracks(self, playlist: Playlist) -> list[Track]: ...
    def recently_added(self) -> list[Album]: ...
    def recently_played(self) -> list[Track]: ...
    def search(self, query: str) -> SearchResults: ...
    def stations(self) -> list[Playlist]: ...
    def station_tracks(self, station: Playlist) -> list[Track]: ...
    def artist_radio(self, artist: Artist) -> list[Track]: ...
    def similar(self, track: Track) -> list[Track]: ...
    def sonic_adventure(self, start: Track, end: Track) -> list[Track]: ...
    def stream(self, track: Track) -> Stream: ...
    def track(self, track_id: str) -> Track: ...
    def lyrics(self, track: Track) -> lrc.Lyrics | None: ...
    def art(self, thumb: str | None, size: int) -> bytes | None: ...
    def rate(self, track: Track, rating: float | None) -> Track: ...
    def create_playlist(self, title: str, tracks: list[Track]) -> Playlist: ...
    def add_to_playlist(self, playlist: Playlist, tracks: list[Track]) -> None: ...
    def report(self, track: Track, state: str, position_ms: int) -> None: ...
    def scrobble(self, track: Track) -> None: ...


def _int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


HUB_ITEMS = 14
PAGE = 300  # big libraries are browsed a page at a time ("Load more…")
LIST_LIMIT = 1000
SERVER_TIMEOUT = 60  # a 60,000-album library can take a while to sort
QUICK_TIMEOUT = 5  # one item, or what a player is doing: asked every second while following


def with_timeout(server, seconds: float):
    """The same server connection (session, token, address) with its own timeout, for calls
    that must give up quickly while browsing keeps the long one."""
    if server is None:
        return None
    view = copy.copy(server)
    view._timeout = seconds
    return view


class PlexLibrary:
    """One music section of one Plex Media Server."""

    def __init__(self, server: PlexServer, section_id: str = "") -> None:
        server._timeout = max(getattr(server, "_timeout", 0) or 0, SERVER_TIMEOUT)
        self.server = server
        self.quick = with_timeout(server, QUICK_TIMEOUT)
        sections = [s for s in server.library.sections() if s.type == "artist"]
        if not sections:
            raise LookupError(f"{server.friendlyName} has no music library")
        self.section = next((s for s in sections if str(s.key) == section_id), sections[0])
        self.name = f"{self.section.title} · {server.friendlyName}"
        self._items: dict[str, object] = {}  # id → plexapi object, for actions on what we listed

    # ── conversions ──
    def _artist(self, a) -> Artist:
        self._items[str(a.ratingKey)] = a
        return Artist(
            id=str(a.ratingKey),
            title=a.title,
            thumb=a.thumb,
            genres=tuple(g.tag for g in getattr(a, "genres", []) or []),
        )

    def _album(self, a) -> Album:
        self._items[str(a.ratingKey)] = a
        return Album(
            id=str(a.ratingKey),
            title=a.title,
            artist=a.parentTitle or "",
            year=a.year,
            thumb=a.thumb,
            track_count=_int(getattr(a, "leafCount", None)),
        )

    def _track(self, t) -> Track:
        self._items[str(t.ratingKey)] = t
        media = t.media[0] if t.media else None
        return Track(
            id=str(t.ratingKey),
            title=t.title,
            artist=t.originalTitle or t.grandparentTitle or "",
            album=t.parentTitle or "",
            duration_ms=t.duration or 0,
            index=t.index,
            year=getattr(t, "parentYear", None) or t.year,
            thumb=t.parentThumb or t.thumb,
            rating=t.userRating,
            codec=media.audioCodec if media else None,
            bitrate_kbps=media.bitrate if media else None,
            album_id=str(t.parentRatingKey or ""),
            artist_id=str(t.grandparentRatingKey or ""),
        )

    def _playlist(self, p, radio: bool = False) -> Playlist:
        self._items[str(p.ratingKey) if p.ratingKey else p.key] = p
        return Playlist(
            id=str(p.ratingKey) if p.ratingKey else p.key,
            title=p.title,
            count=_int(getattr(p, "leafCount", None)) or 0,
            smart=bool(getattr(p, "smart", False)),
            radio=radio or bool(getattr(p, "radio", False)),
            thumb=getattr(p, "composite", None) or getattr(p, "thumb", None),
        )

    def track(self, track_id: str) -> Track:
        """One track by id (a remote player's, while following it: quick)."""
        obj = self._items.get(track_id) or self.quick.fetchItem(int(track_id))
        return self._track(obj)

    def track_from(self, item) -> Track:
        """A plexapi track (e.g. from a session) as a Track."""
        return self._track(item)

    def _obj(self, item_id: str):
        obj = self._items.get(item_id)
        if obj is None:
            obj = self.server.fetchItem(int(item_id))
            self._items[item_id] = obj
        return obj

    # ── browsing ──
    def home(self) -> list[Hub]:
        """The section's hubs, the rows Plexamp's home is made of (including Mixes for you).
        Only the first items of each hub come back: a hub can hold tens of thousands."""
        convert = {"album": self._album, "track": self._track, "artist": self._artist}
        hubs = []
        for hub in self.section.hubs(count=HUB_ITEMS, includeMyMixes=1):
            try:
                items = list(hub._partialItems)[:HUB_ITEMS]  # never hub.items(): that loads them all
            except Exception:
                continue
            kind = "station" if "station" in (getattr(hub, "context", "") or "") else hub.type
            if kind in convert:
                converted = [convert[kind](i) for i in items if getattr(i, "type", "") == kind]
            elif kind in ("playlist", "station", "mix"):
                converted = [self._playlist(i, radio=kind != "playlist") for i in items]
                kind = "station" if kind == "mix" else kind
            else:
                continue
            if converted:
                hubs.append(Hub(title=hub.title, kind=kind, items=tuple(converted)))
        return hubs

    def album(self, album_id: str) -> Album:
        return self._album(self._obj(album_id))

    def artist(self, artist_id: str) -> Artist:
        return self._artist(self._obj(artist_id))

    def liked(self) -> list[Track]:
        return [
            self._track(t)
            for t in self.section.searchTracks(
                filters={"userRating": 10}, sort="lastRatedAt:desc", maxresults=LIST_LIMIT
            )
        ]

    def artist_overview(self, artist: Artist) -> ArtistOverview:
        a = self._obj(artist.id)
        try:
            popular = [self._track(t) for t in a.popularTracks()[:10]]
        except Exception:  # older servers have no popularity data
            popular = []
        albums, singles = [], []
        for album in sorted(a.albums(), key=lambda x: x.year or 0, reverse=True):
            formats = " ".join(s.tag for s in getattr(album, "subformats", []) or []).lower()
            small = (_int(getattr(album, "leafCount", None)) or 99) <= 3
            (singles if "single" in formats or "ep" in formats.split() or small else albums).append(
                self._album(album)
            )
        try:
            similar = [self._artist(s) for s in a.sonicallySimilar(limit=12)]
        except Exception:
            similar = []
        return ArtistOverview(tuple(popular), tuple(albums), tuple(singles), tuple(similar))

    def details(self, track: Track) -> list[tuple[str, str]]:
        t = self.server.fetchItem(int(track.id))
        media = t.media[0] if t.media else None
        audio = (
            next((s for s in media.parts[0].streams if s.streamType == 2), None)
            if media and media.parts
            else None
        )
        rows = [
            ("Title", t.title),
            ("Artist", t.originalTitle or t.grandparentTitle or ""),
            (
                "Album",
                f"{t.parentTitle or ''}" + (f" ({t.parentYear})" if getattr(t, "parentYear", None) else ""),
            ),
            (
                "Track",
                f"{t.index or '?'}" + (f" · disc {t.parentIndex}" if getattr(t, "parentIndex", None) else ""),
            ),
            (
                "Format",
                " · ".join(
                    x
                    for x in (
                        (media.audioCodec or "").upper() if media else "",
                        f"{media.bitrate} kbps" if media and media.bitrate else "",
                        f"{audio.samplingRate / 1000:g} kHz"
                        if audio and getattr(audio, "samplingRate", None)
                        else "",
                        f"{audio.bitDepth}-bit" if audio and getattr(audio, "bitDepth", None) else "",
                        f"{media.audioChannels} ch" if media and media.audioChannels else "",
                    )
                    if x
                ),
            ),
            (
                "Loudness",
                f"{audio.gain} dB gain"
                if audio and getattr(audio, "gain", None) is not None
                else "not analysed",
            ),
            ("Plays", str(t.viewCount or 0)),
            ("Last played", t.lastViewedAt.strftime("%Y-%m-%d") if t.lastViewedAt else "never"),
            ("Added", t.addedAt.strftime("%Y-%m-%d") if t.addedAt else ""),
            (
                "Rating",
                "♥ liked"
                if (t.userRating or 0) >= 10
                else (f"{t.userRating / 2:g}★" if t.userRating else ""),
            ),
        ]
        return [(k, v) for k, v in rows if v]

    def artists(self, start: int = 0) -> list[Artist]:
        found = self.section.search(
            libtype="artist", sort="titleSort", container_start=start, container_size=PAGE, maxresults=PAGE
        )
        return [self._artist(a) for a in found]

    def albums(self, artist: Artist | None = None, start: int = 0) -> list[Album]:
        if artist:
            albums = self._obj(artist.id).albums()
            return [self._album(a) for a in sorted(albums, key=lambda a: a.year or 0, reverse=True)]
        found = self.section.search(
            libtype="album", sort="titleSort", container_start=start, container_size=PAGE, maxresults=PAGE
        )
        return [self._album(a) for a in found]

    def tracks(self, album: Album) -> list[Track]:
        return [self._track(t) for t in self._obj(album.id).tracks()]

    def artist_tracks(self, artist: Artist) -> list[Track]:
        return [self._track(t) for t in self._obj(artist.id).tracks()]

    def playlists(self) -> list[Playlist]:
        return [self._playlist(p) for p in self.server.playlists(playlistType="audio")]

    def playlist_tracks(self, playlist: Playlist) -> list[Track]:
        return [self._track(t) for t in self._obj(playlist.id).items() if t.type == "track"]

    def recently_added(self) -> list[Album]:
        return [self._album(a) for a in self.section.recentlyAddedAlbums(maxresults=60)]

    def recently_played(self) -> list[Track]:
        found = self.section.search(
            libtype="track", sort="lastViewedAt:desc", limit=60, filters={"lastViewedAt>>": "3650d"}
        )
        return [self._track(t) for t in found]

    def search(self, query: str) -> SearchResults:
        hits = self.section.hubSearch(query, limit=20)
        by_type: dict[str, list] = {"artist": [], "album": [], "track": [], "playlist": []}
        for hit in hits:
            if hit.type in by_type:
                by_type[hit.type].append(hit)
        return SearchResults(
            artists=tuple(self._artist(a) for a in by_type["artist"]),
            albums=tuple(self._album(a) for a in by_type["album"]),
            tracks=tuple(self._track(t) for t in by_type["track"]),
            playlists=tuple(self._playlist(p) for p in by_type["playlist"]),
        )

    # ── radio ──
    def stations(self) -> list[Playlist]:
        return [self._playlist(s, radio=True) for s in self.section.stations() or []]

    def station_tracks(self, station: Playlist) -> list[Track]:
        obj = self._obj(station.id)
        items = obj.items() if hasattr(obj, "items") else []
        return [self._track(t) for t in items if getattr(t, "type", "") == "track"]

    def artist_radio(self, artist: Artist) -> list[Track]:
        """Like Plexamp's artist radio: the artist plus sonically similar artists, shuffled."""
        a = self._obj(artist.id)
        pool = list(a.tracks())
        for similar in a.sonicallySimilar(limit=8) if hasattr(a, "sonicallySimilar") else []:
            pool.extend(random.sample(similar.tracks(), k=min(6, len(similar.tracks()))))
        random.shuffle(pool)
        return [self._track(t) for t in pool[:100]]

    def similar(self, track: Track) -> list[Track]:
        """Sonically similar tracks (Plex's sonic analysis), the seed first."""
        t = self._obj(track.id)
        return [track] + [self._track(s) for s in t.sonicallySimilar(limit=50)]

    def sonic_adventure(self, start: Track, end: Track) -> list[Track]:
        return [self._track(t) for t in self.section.sonicAdventure(self._obj(start.id), self._obj(end.id))]

    # ── playback ──
    def stream(self, track: Track) -> Stream:
        t = self.server.fetchItem(int(track.id))  # full metadata: parts and loudness
        part = t.media[0].parts[0]
        audio = next((s for s in part.streams if s.streamType == 2), None)
        gain = getattr(audio, "gain", None) if audio else None
        url = self.server.url(part.key, includeToken=False)
        headers = (("X-Plex-Token", self.server._token),)
        return Stream(url=url, headers=headers, gain_db=float(gain) if gain is not None else None)

    def lyrics(self, track: Track) -> lrc.Lyrics | None:
        t = self.server.fetchItem(int(track.id))
        streams = sorted(t.lyricStreams(), key=lambda s: not s.timed)
        for stream in streams:
            r = self.server._session.get(
                self.server.url(stream.key), headers=self.server._headers(), timeout=10
            )
            if not r.ok or not r.text.strip():
                continue
            return _parse_plex_lyrics(r.text)
        return None

    def art(self, thumb: str | None, size: int) -> bytes | None:
        if not thumb:
            return None
        cache = config.ART_DIR / (
            hashlib.sha1(f"{self.server.machineIdentifier}{thumb}{size}".encode()).hexdigest() + ".jpg"
        )
        if cache.exists():
            return cache.read_bytes()
        url = self.server.url(
            f"/photo/:/transcode?width={size}&height={size}&minSize=1&upscale=1&url={thumb}"
        )
        r = self.server._session.get(url, headers=self.server._headers(), timeout=10)
        if not r.ok:
            return None
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(r.content)
        return r.content

    def rate(self, track: Track, rating: float | None) -> Track:
        self._obj(track.id).rate(rating)
        from dataclasses import replace

        return replace(track, rating=rating)

    def create_playlist(self, title: str, tracks: list[Track]) -> Playlist:
        items = [self._obj(t.id) for t in tracks]
        return self._playlist(self.server.createPlaylist(title, items=items))

    def add_to_playlist(self, playlist: Playlist, tracks: list[Track]) -> None:
        self._obj(playlist.id).addItems([self._obj(t.id) for t in tracks])

    def report(self, track: Track, state: str, position_ms: int) -> None:
        """Tell the server what we're playing: shows in its dashboard and updates history."""
        self._obj(track.id).updateTimeline(position_ms, state=state, duration=track.duration_ms)

    def scrobble(self, track: Track) -> None:
        self._obj(track.id).markPlayed()


def _parse_plex_lyrics(body: str) -> lrc.Lyrics:
    """Plex serves raw LRC/TXT, or XML <Line startOffset=…><Span text=…/></Line>."""
    if not body.lstrip().startswith("<"):
        return lrc.parse(body)
    root = ElementTree.fromstring(body)
    lines = []
    for line in root.iter("Line"):
        text = " ".join(span.get("text", "") for span in line.iter("Span")).strip()
        start = _int(line.get("startOffset"))
        lines.append(lrc.Line(start, text))
    timed = bool(lines) and all(line.time_ms is not None for line in lines)
    return lrc.Lyrics(tuple(lines), timed=timed)
