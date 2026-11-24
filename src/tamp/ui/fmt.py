"""Turning models into aligned, coloured rows, in the current theme's colours."""

from __future__ import annotations

from dataclasses import dataclass

from rich.cells import cell_len, set_cell_size
from rich.text import Text

from tamp.models import Album, Artist, Playlist, Track
from tamp.ui import theme

# the colour names the UI code uses, mapped to theme roles; read at render time (fmt.PINK)
ROLES = {
    "PINK": "accent", "GREEN": "playing", "YELLOW": "highlight", "CYAN": "album",
    "PURPLE": "extra", "ORANGE": "warm", "DIM": "dim", "FG": "fg",
}  # fmt: skip


def __getattr__(name: str) -> str:
    if name in ROLES:
        return getattr(theme.current(), ROLES[name])
    raise AttributeError(name)


def colour(name: str) -> str:
    return getattr(theme.current(), ROLES[name])


def duration(ms: int) -> str:
    s = max(0, ms) // 1000
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fit(text: str, width: int) -> str:
    """Pad or cut to exactly `width` terminal cells (CJK and most emoji take two each)."""
    if width <= 0:
        return ""
    used = cell_len(text)
    return text + " " * (width - used) if used <= width else set_cell_size(text, width - 1) + "…"


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
    want_title = max((cell_len(t.title) for t in tracks), default=MIN_TITLE)
    want_artist = max((cell_len(t.artist) for t in tracks), default=MIN_ARTIST)
    want_album = max((cell_len(t.album) for t in tracks), default=MIN_ALBUM) if show_album else 0
    title = min(want_title, avail)
    artist = min(want_artist, max(MIN_ARTIST, avail - title - GAP))
    album = min(want_album, max(0, avail - title - artist - 2 * GAP)) if show_album else 0
    album = album if album >= MIN_ALBUM else 0  # a sliver of album is just "…": leave it out
    # Not everything fits: share the room, titles first, and drop the album column below its minimum.
    if title + GAP + artist + (GAP + album if album else 0) > avail:
        artist = max(MIN_ARTIST, min(want_artist, (avail - GAP) * 3 // 10))
        title = max(MIN_TITLE, avail - GAP - artist)
        album = 0
    if show_album and album == 0 and want_album:
        spare = avail - title - artist - 2 * GAP
        if spare >= MIN_ALBUM:
            album = min(want_album, spare)
    title = avail - artist - GAP - (album + GAP if album else 0)  # leftover space goes to titles
    if title < MIN_TITLE and album:  # short titles: take their minimum back from the album column
        album -= MIN_TITLE - title
        album = album if album >= MIN_ALBUM else 0
        title = avail - artist - GAP - (album + GAP if album else 0)
    if title < MIN_TITLE:  # still short: the artist column gives way too, down to its minimum
        artist = max(MIN_ARTIST, artist - (MIN_TITLE - title))
        title = avail - artist - GAP - (album + GAP if album else 0)
    return Columns(num=num, title=max(MIN_TITLE, title), artist=artist, album=album)


def track_row(t: Track, cols: Columns, playing: bool = False, number: int | None = None) -> Text:
    num = f"{number if number is not None else '':>{cols.num - 1}} " if cols.num else ""
    parts = [
        ("▶ " if playing else "  ", colour("GREEN")),
        (num, colour("DIM")),
        (fit(t.title, cols.title), f"bold {colour('GREEN')}" if playing else colour("FG")),
        (" " * GAP, ""),
        (fit(t.artist, cols.artist), colour("PINK") if playing else colour("DIM")),
    ]
    if cols.album:
        parts += [(" " * GAP, ""), (fit(t.album, cols.album), colour("DIM"))]
    parts += [
        (f" {'♥' if t.liked else ' '} ", colour("PINK")),
        (duration(t.duration_ms).rjust(7), colour("DIM")),
    ]
    return Text.assemble(*parts)


def artist_row(a: Artist, width: int) -> Text:
    genres = ", ".join(a.genres[:2])
    return Text.assemble(
        ("  ◉ ", colour("PURPLE")),
        (fit(a.title, max(10, width - 6 - cell_len(genres))), colour("FG")),
        (genres, colour("DIM")),
    )


def album_row(a: Album, width: int, show_artist: bool = True) -> Text:
    year = f"{a.year}" if a.year else ""
    count = f"{a.track_count} tracks" if a.track_count else ""
    right = f"  {year:>4}  {count:>10}"  # leading gap: a long title never runs into the year
    left = f"{a.title}" + (f"  ·  {a.artist}" if show_artist else "")
    return Text.assemble(
        ("  ◎ ", colour("CYAN")),
        (fit(left, max(10, width - 6 - len(right))), colour("FG")),
        (right, colour("DIM")),
    )


def playlist_row(p: Playlist, width: int) -> Text:
    icon, tint = (
        ("  ✦ ", colour("ORANGE"))
        if p.radio
        else (("  ⚙ ", colour("YELLOW")) if p.smart else ("  ≡ ", colour("YELLOW")))
    )
    count = f"  {p.count} tracks" if p.count and not p.radio else ("  mix" if p.radio else "")
    return Text.assemble(
        (icon, tint), (fit(p.title, max(10, width - 6 - len(count))), colour("FG")), (count, colour("DIM"))
    )


def action_row(icon: str, label: str, tint: str | None = None) -> Text:
    tint = tint or colour("GREEN")  # looked up now: the theme may have changed since import
    return Text.assemble((f"  {icon} ", tint), (label, f"bold {tint}"))


def heading(label: str) -> Text:
    return Text(f" {label}", style=f"bold {colour('DIM')}")
