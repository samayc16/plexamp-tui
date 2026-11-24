"""Immutable values the UI works with, independent of where the music comes from."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Artist:
    id: str
    title: str
    thumb: str | None = None
    genres: tuple[str, ...] = ()


@dataclass(frozen=True)
class Album:
    id: str
    title: str
    artist: str
    year: int | None = None
    thumb: str | None = None
    track_count: int | None = None


@dataclass(frozen=True)
class Track:
    id: str
    title: str
    artist: str
    album: str
    duration_ms: int
    index: int | None = None
    year: int | None = None
    thumb: str | None = None
    rating: float | None = None  # 0-10; Plexamp's "like" is 10
    codec: str | None = None
    bitrate_kbps: int | None = None
    gain_db: float | None = None  # loudness leveling from Plex's sonic analysis
    has_lyrics: bool = False
    album_id: str = ""
    artist_id: str = ""

    @property
    def liked(self) -> bool:
        return (self.rating or 0) >= 10


@dataclass(frozen=True)
class Playlist:
    id: str
    title: str
    count: int
    smart: bool = False
    radio: bool = False  # a station: an endless, generated queue
    thumb: str | None = None


@dataclass(frozen=True)
class SearchResults:
    artists: tuple[Artist, ...] = ()
    albums: tuple[Album, ...] = ()
    tracks: tuple[Track, ...] = ()
    playlists: tuple[Playlist, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.artists or self.albums or self.tracks or self.playlists)


@dataclass(frozen=True)
class Hub:
    """A row on the home page: "Recently played", "Stations", "More from your library"…"""

    title: str
    kind: str  # album track artist playlist station
    items: tuple = ()


@dataclass(frozen=True)
class ArtistOverview:
    popular: tuple[Track, ...] = ()
    albums: tuple[Album, ...] = ()
    singles: tuple[Album, ...] = ()  # singles and EPs
    similar: tuple[Artist, ...] = ()


@dataclass(frozen=True)
class Stream:
    """How to play a track: a URL plus headers that must not leak into the URL."""

    url: str
    headers: tuple[tuple[str, str], ...] = ()
    gain_db: float | None = None


@dataclass(frozen=True)
class Device:
    """Somewhere to play: this computer, or a Plex/Plexamp player we can remote-control."""

    id: str
    name: str
    product: str = ""
    local: bool = False


THIS_DEVICE = Device(id="local", name="This computer", product="mpv", local=True)


@dataclass(frozen=True)
class Playback:
    """A snapshot of what an output is doing."""

    track: Track | None = None
    position_ms: int = 0
    duration_ms: int = 0
    paused: bool = True
    volume: int = 100
    index: int = -1  # position in the queue
