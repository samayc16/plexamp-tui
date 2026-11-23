"""Turning models into aligned, coloured rows (Monokai Pro palette)."""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text

from tamp.models import Album, Artist, Playlist, Track

PINK, GREEN, YELLOW, CYAN, PURPLE, ORANGE = "#ff6188", "#a9dc76", "#ffd866", "#78dce8", "#ab9df2", "#fc9867"
DIM, FG = "#727072", "#fcfcfa"


def duration(ms: int) -> str:
    s = max(0, ms) // 1000
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fit(text: str, width: int) -> str:
    """Pad or cut to exactly `width` cells."""
    if width <= 0:
        return ""
    return text.ljust(width) if len(text) <= width else text[: width - 1] + "…"


@dataclass(frozen=True)
class Columns:
    """Column widths for a page of tracks, so every row lines up."""

    num: int
    title: int
    artist: int
    album: int  # 0: no album column (album pages, or not enough room)


GAP = 2
MIN_TITLE, MIN_ARTIST, MIN_ALBUM = 16, 10, 10


def columns(
    tracks: list[Track], width: int, show_album: bool = True, numbers: list[int] | None = None
) -> Columns:
    """Widths from the page's own content: titles get room first, then artists, then albums;
    the album column goes when there isn't room for it."""
    num = max((len(str(n)) for n in numbers or [] if n), default=0)
    num = num + 1 if num else 0
    fixed = 2 + num + 3 + 7  # marker, number, " ♥ ", duration
    avail = max(MIN_TITLE, width - fixed)
    want_title = max((len(t.title) for t in tracks), default=MIN_TITLE)
    want_artist = max((len(t.artist) for t in tracks), default=MIN_ARTIST)
    want_album = max((len(t.album) for t in tracks), default=MIN_ALBUM) if show_album else 0
    title = min(want_title, avail)
    artist = min(want_artist, max(MIN_ARTIST, avail - title - GAP))
    album = min(want_album, max(0, avail - title - artist - 2 * GAP)) if show_album else 0
    # Not everything fits: share the room, titles first, and drop the album column below its minimum.
    if title + GAP + artist + (GAP + album if album else 0) > avail:
        artist = max(MIN_ARTIST, min(want_artist, (avail - GAP) * 3 // 10))
        title = max(MIN_TITLE, avail - GAP - artist)
        album = 0
    if show_album and album == 0 and want_album:
        spare = avail - title - artist - 2 * GAP
        if spare >= MIN_ALBUM:
            album = min(want_album, spare)
    title = max(
        MIN_TITLE, avail - artist - GAP - (album + GAP if album else 0)
    )  # leftover space goes to titles
    return Columns(num=num, title=title, artist=artist, album=album)


def track_row(t: Track, cols: Columns, playing: bool = False, number: int | None = None) -> Text:
    num = f"{number if number is not None else '':>{cols.num - 1}} " if cols.num else ""
    parts = [
        ("▶ " if playing else "  ", GREEN),
        (num, DIM),
        (fit(t.title, cols.title), f"bold {GREEN}" if playing else FG),
        (" " * GAP, ""),
        (fit(t.artist, cols.artist), PINK if playing else DIM),
    ]
    if cols.album:
        parts += [(" " * GAP, ""), (fit(t.album, cols.album), DIM)]
    parts += [(f" {'♥' if t.liked else ' '} ", PINK), (duration(t.duration_ms).rjust(7), DIM)]
    return Text.assemble(*parts)


def artist_row(a: Artist, width: int) -> Text:
    genres = ", ".join(a.genres[:2])
    return Text.assemble(
        ("  ◉ ", PURPLE), (fit(a.title, max(10, width - 6 - len(genres))), FG), (genres, DIM)
    )


def album_row(a: Album, width: int, show_artist: bool = True) -> Text:
    year = f"{a.year}" if a.year else ""
    count = f"{a.track_count} tracks" if a.track_count else ""
    right = f"{year:>4}  {count:>10}"
    left = f"{a.title}" + (f"  ·  {a.artist}" if show_artist else "")
    return Text.assemble(("  ◎ ", CYAN), (fit(left, max(10, width - 6 - len(right))), FG), (right, DIM))


def playlist_row(p: Playlist, width: int) -> Text:
    icon, colour = ("  ✦ ", ORANGE) if p.radio else (("  ⚙ ", YELLOW) if p.smart else ("  ≡ ", YELLOW))
    count = "" if p.radio else f"{p.count} tracks"
    return Text.assemble((icon, colour), (fit(p.title, max(10, width - 6 - len(count))), FG), (count, DIM))


def action_row(icon: str, label: str, colour: str = GREEN) -> Text:
    return Text.assemble((f"  {icon} ", colour), (label, f"bold {colour}"))


def heading(label: str) -> Text:
    return Text(f" {label}", style=f"bold {DIM}")
