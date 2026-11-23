"""Turning models into aligned, coloured rows (Monokai Pro palette)."""

from __future__ import annotations

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


def track_row(t: Track, width: int, playing: bool = False, show_album: bool = True, number: int | None = None) -> Text:
    marker = Text("▶ " if playing else "  ", style=GREEN)
    num = f"{number if number is not None else (t.index or ''):>3} "
    like = "♥" if t.liked else " "
    dur = duration(t.duration_ms)
    rest = max(10, width - len(num) - 2 - len(dur) - 4)
    if show_album:
        title_w = max(8, rest // 2)
        artist_w = max(6, rest - title_w - 2)
        meta = fit(f"{t.artist} · {t.album}", artist_w)
    else:
        title_w, artist_w = rest - 2, 0
        meta = ""
    text = Text.assemble(
        marker,
        (num, DIM),
        (fit(t.title, title_w), f"bold {GREEN}" if playing else FG),
        ("  " if meta else "", ""),
        (meta, DIM),
        (f" {like} ", PINK),
        (dur, DIM),
    )
    return text


def artist_row(a: Artist, width: int) -> Text:
    genres = ", ".join(a.genres[:2])
    return Text.assemble(("  ◉ ", PURPLE), (fit(a.title, max(10, width - 6 - len(genres))), FG), (genres, DIM))


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
