"""`tamp --demo`: an invented library that needs no Plex server. Audio is a soft tone generated
by mpv itself, art is drawn on the fly, and the lyrics are original. Used by the tests and
for screenshots."""

from __future__ import annotations

import io
import random
from dataclasses import replace

from PIL import Image, ImageDraw

from tamp import lrc
from tamp.models import Album, Artist, ArtistOverview, Hub, Playlist, SearchResults, Stream, Track

_CATALOGUE = {
    "Aurora Drift": [
        (
            "Night Signals",
            2021,
            ["Low Orbit", "Static Bloom", "Glass Harbour", "Neon Tide", "Afterglow Radio"],
        ),
        ("Weightless", 2024, ["Undertow", "Paper Moons", "Satellite Hearts", "Slow Fade"]),
    ],
    "The Copper Lanterns": [
        ("Railway Hymns", 2019, ["Coal Smoke Morning", "Lantern Song", "Iron Bridge", "Last Train West"]),
        ("Small Fires", 2023, ["Kindling", "Ember Waltz", "Ash & Honey", "Small Fires"]),
    ],
    "Mono Lake": [
        ("Salt Flats", 2022, ["Mirage", "Brine", "Pale Horizon", "Dust Devils", "Evaporation"]),
    ],
    "Fjordline": [
        ("Northern Lines", 2020, ["Polar Night", "Aurora Borealis", "Fjord", "Midnight Sun"]),
    ],
}

_PALETTE = [
    (255, 97, 136),
    (169, 220, 118),
    (255, 216, 102),
    (120, 220, 232),
    (171, 157, 242),
    (252, 152, 103),
]

_LYRICS = """[00:00.50]Counting the lights on the harbour wall
[00:04.00]every one a small slow star
[00:07.50]the radio hums what the sea can't say
[00:11.00]and I don't know where you are
[00:15.00]Low orbit, low orbit
[00:18.50]circling the same old town
[00:22.00]low orbit, low orbit
[00:25.50]waiting for the sky to come down
[00:30.00](instrumental)
"""


def _id(*parts: str) -> str:
    return "demo-" + "-".join(p.lower().replace(" ", "_").replace("&", "and") for p in parts)


class DemoLibrary:
    name = "Demo library · offline"

    def __init__(self, seed: int = 7) -> None:
        self._rng = random.Random(seed)
        self._artists: list[Artist] = []
        self._albums: list[Album] = []
        self._tracks: dict[str, list[Track]] = {}
        self._ratings: dict[str, float | None] = {}
        self._playlists: dict[str, list[Track]] = {}
        for a_i, (artist, albums) in enumerate(_CATALOGUE.items()):
            artist_obj = Artist(id=_id(artist), title=artist, thumb=f"art:{a_i}:0")
            self._artists.append(artist_obj)
            for b_i, (title, year, songs) in enumerate(albums):
                album = Album(
                    id=_id(artist, title),
                    title=title,
                    artist=artist,
                    year=year,
                    thumb=f"art:{a_i}:{b_i}",
                    track_count=len(songs),
                )
                self._albums.append(album)
                self._tracks[album.id] = [
                    Track(
                        id=_id(artist, title, song),
                        title=song,
                        artist=artist,
                        album=title,
                        duration_ms=30_000 + 7_000 * ((a_i + n) % 5),
                        index=n + 1,
                        year=year,
                        thumb=album.thumb,
                        codec="flac",
                        bitrate_kbps=900 + 40 * n,
                        gain_db=-1.5 - n * 0.4,
                        has_lyrics=song == "Low Orbit",
                        album_id=album.id,
                        artist_id=artist_obj.id,
                    )
                    for n, song in enumerate(songs)
                ]
        everything = self._all_tracks()
        self._playlists = {
            _id("playlist", "Late night"): [
                t for t in everything if t.artist in ("Aurora Drift", "Mono Lake")
            ][:9],
            _id("playlist", "Road trip"): [
                t for t in everything if t.artist in ("The Copper Lanterns", "Fjordline")
            ],
        }

    def _all_tracks(self) -> list[Track]:
        return [
            replace(t, rating=self._ratings.get(t.id, t.rating)) for ts in self._tracks.values() for t in ts
        ]

    def home(self) -> list[Hub]:
        return [
            Hub("Recently played", "track", tuple(self.recently_played()[:8])),
            Hub("Recently added", "album", tuple(self.recently_added())),
            Hub("Stations", "station", tuple(self.stations())),
            Hub("Your playlists", "playlist", tuple(self.playlists())),
            Hub("Artists", "artist", tuple(self._artists)),
        ]

    def album(self, album_id: str) -> Album:
        return next(a for a in self._albums if a.id == album_id)

    def artist(self, artist_id: str) -> Artist:
        return next(a for a in self._artists if a.id == artist_id)

    def liked(self) -> list[Track]:
        return [t for t in self._all_tracks() if t.liked]

    def artist_overview(self, artist: Artist) -> ArtistOverview:
        tracks = self.artist_tracks(artist)
        albums = self.albums(artist)
        return ArtistOverview(
            popular=tuple(sorted(tracks, key=lambda t: t.title)[:5]),
            albums=tuple(a for a in albums if (a.track_count or 0) > 4),
            singles=tuple(a for a in albums if (a.track_count or 0) <= 4),
            similar=tuple(a for a in self._artists if a.id != artist.id),
        )

    def details(self, track: Track) -> list[tuple[str, str]]:
        rows = [
            ("Title", track.title),
            ("Artist", track.artist),
            ("Album", f"{track.album} ({track.year})"),
            ("Track", str(track.index)),
            (
                "Format",
                f"{(track.codec or '').upper()} · {track.bitrate_kbps} kbps · 44.1 kHz · 16-bit · 2 ch",
            ),
            ("Loudness", f"{track.gain_db:.1f} dB gain" if track.gain_db is not None else "not analysed"),
            ("Plays", "3"),
            ("Rating", "♥ liked" if self._ratings.get(track.id, track.rating) == 10 else ""),
        ]
        return [(k, v) for k, v in rows if v]

    def artists(self) -> list[Artist]:
        return list(self._artists)

    def albums(self, artist: Artist | None = None) -> list[Album]:
        return [a for a in self._albums if artist is None or a.artist == artist.title]

    def tracks(self, album: Album) -> list[Track]:
        return [replace(t, rating=self._ratings.get(t.id, t.rating)) for t in self._tracks.get(album.id, [])]

    def artist_tracks(self, artist: Artist) -> list[Track]:
        return [t for t in self._all_tracks() if t.artist == artist.title]

    def playlists(self) -> list[Playlist]:
        liked = [t for t in self._all_tracks() if t.liked]
        result = [Playlist(id=_id("liked"), title="❤ Liked", count=len(liked), smart=True)]
        result += [
            Playlist(id=pid, title=pid.split("-")[-1].replace("_", " ").capitalize(), count=len(ts))
            for pid, ts in self._playlists.items()
        ]
        return result

    def playlist_tracks(self, playlist: Playlist) -> list[Track]:
        if playlist.id == _id("liked"):
            return [t for t in self._all_tracks() if t.liked]
        return list(self._playlists.get(playlist.id, []))

    def recently_added(self) -> list[Album]:
        return sorted(self._albums, key=lambda a: a.year or 0, reverse=True)

    def recently_played(self) -> list[Track]:
        return self._all_tracks()[:12]

    def search(self, query: str) -> SearchResults:
        q = query.lower()
        return SearchResults(
            artists=tuple(a for a in self._artists if q in a.title.lower()),
            albums=tuple(a for a in self._albums if q in a.title.lower()),
            tracks=tuple(t for t in self._all_tracks() if q in t.title.lower()),
            playlists=tuple(p for p in self.playlists() if q in p.title.lower()),
        )

    def stations(self) -> list[Playlist]:
        return [
            Playlist(id=_id("station", "library"), title="Library Radio", count=0, radio=True),
            Playlist(id=_id("station", "deep"), title="Deep Cuts Radio", count=0, radio=True),
        ]

    def station_tracks(self, station: Playlist) -> list[Track]:
        pool = self._all_tracks()
        self._rng.shuffle(pool)
        return pool

    def artist_radio(self, artist: Artist) -> list[Track]:
        own = self.artist_tracks(artist)
        others = [t for t in self._all_tracks() if t.artist != artist.title]
        self._rng.shuffle(others)
        mix = own + others[: len(own)]
        self._rng.shuffle(mix)
        return mix

    def similar(self, track: Track) -> list[Track]:
        rest = [t for t in self._all_tracks() if t.id != track.id]
        self._rng.shuffle(rest)
        return [track] + rest[:15]

    def sonic_adventure(self, start: Track, end: Track) -> list[Track]:
        middle = [t for t in self._all_tracks() if t.id not in (start.id, end.id)]
        return [start] + self._rng.sample(middle, k=min(6, len(middle))) + [end]

    def track(self, track_id: str) -> Track:
        return next(t for t in self._all_tracks() if t.id == track_id)

    def stream(self, track: Track) -> Stream:
        freq = 220 + (int.from_bytes(track.id.encode()[-2:], "big") % 12) * 22
        seconds = track.duration_ms / 1000
        # a quiet tone that fades in and out, generated by mpv's libavfilter input
        url = f"av://lavfi:sine=frequency={freq}:duration={seconds:.1f},volume=0.15,afade=t=in:d=1,afade=t=out:st={seconds - 1.5:.1f}:d=1.5"
        return Stream(url=url, gain_db=track.gain_db)

    def lyrics(self, track: Track) -> lrc.Lyrics | None:
        return lrc.parse(_LYRICS) if track.has_lyrics else None

    def art(self, thumb: str | None, size: int) -> bytes | None:
        if not thumb or not thumb.startswith("art:"):
            return None
        _, a, b = thumb.split(":")
        c1 = _PALETTE[int(a) % len(_PALETTE)]
        c2 = _PALETTE[(int(a) + int(b) + 2) % len(_PALETTE)]
        image = Image.new("RGB", (size, size))
        draw = ImageDraw.Draw(image)
        for y in range(size):
            mix = y / size
            draw.line([(0, y), (size, y)], fill=tuple(int(c1[i] * (1 - mix) + c2[i] * mix) for i in range(3)))
        r = size // 4
        draw.ellipse([size / 2 - r, size / 2 - r, size / 2 + r, size / 2 + r], fill=(45, 42, 46))
        draw.ellipse([size / 2 - r / 5, size / 2 - r / 5, size / 2 + r / 5, size / 2 + r / 5], fill=c1)
        buf = io.BytesIO()
        image.save(buf, "PNG")
        return buf.getvalue()

    def rate(self, track: Track, rating: float | None) -> Track:
        self._ratings[track.id] = rating
        return replace(track, rating=rating)

    def create_playlist(self, title: str, tracks: list[Track]) -> Playlist:
        pid = _id("playlist", title)
        self._playlists[pid] = list(tracks)
        return Playlist(id=pid, title=title, count=len(tracks))

    def add_to_playlist(self, playlist: Playlist, tracks: list[Track]) -> None:
        self._playlists.setdefault(playlist.id, []).extend(tracks)

    def report(self, track: Track, state: str, position_ms: int) -> None:
        return None

    def scrobble(self, track: Track) -> None:
        return None
