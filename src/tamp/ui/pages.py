"""Building pages from library data."""

from __future__ import annotations

from tamp.models import Album, Artist, ArtistOverview, Hub, Playlist, SearchResults, Track
from tamp.queue import Queue
from tamp.ui.browser import Entry, Header, Page
from tamp.ui.fmt import duration

SECTIONS = [
    ("home", "⌂ Home"),
    ("artists", "◉ Artists"),
    ("albums", "◎ Albums"),
    ("playlists", "≡ Playlists"),
    ("stations", "✦ Stations"),
    ("recent", "◷ Recently added"),
    ("liked", "♥ Liked"),
    ("queue", "» Queue"),
]

_ENTRY_KIND = {
    "track": "track",
    "album": "album",
    "artist": "artist",
    "playlist": "playlist",
    "station": "station",
}


def home(hubs: list[Hub]) -> Page:
    page = Page(title="Home", kind="home", source="Home")
    for hub in hubs:
        kind = _ENTRY_KIND.get(hub.kind)
        if kind and hub.items:
            page.entries.append(Entry("heading", label=hub.title))
            page.entries += [Entry(kind, item) for item in hub.items]
    if not page.entries:
        page.entries = [Entry("heading", label="nothing here yet: try / to search")]
    return page


def listing(title: str, kind: str, items: list) -> Page:
    entry_kind = {
        "artists": "artist",
        "albums": "album",
        "recent": "album",
        "playlists": "playlist",
        "stations": "station",
    }[kind]
    page = Page(title=title, kind=kind, source=title, entry_kind=entry_kind)
    page.entries = [Entry(entry_kind, item) for item in items] or [Entry("heading", label="nothing here yet")]
    if len(items) >= PAGE_SIZE:
        page.entries.append(MORE)
    return page


PAGE_SIZE = 300
MORE = Entry("action", "more", "Load more…   (or / to search everything)")


def extend(page: Page, items: list) -> int:
    """Append the next page of a listing; returns the index of the first new row."""
    if page.entries and page.entries[-1] == MORE:
        page.entries.pop()
    first = len(page.entries)
    page.entries += [Entry(page.entry_kind, item) for item in items]
    if len(items) >= PAGE_SIZE:
        page.entries.append(MORE)
    return first


def tracks(
    title: str, kind: str, items: list[Track], header: Header | None = None, subject: object = None
) -> Page:
    page = Page(title=title, kind=kind, source=title, header=header, subject=subject)
    if items:
        page.entries = [Entry("action", "play", "Play"), Entry("action", "shuffle", "Shuffle")]
        if kind == "album":
            page.entries.append(Entry("action", "radio", "Album radio"))
    page.entries += [Entry("track", t) for t in items]
    if not items:
        page.entries = [Entry("heading", label="no tracks")]
    return page


def album(a: Album, items: list[Track]) -> Page:
    total = duration(sum(t.duration_ms for t in items))
    meta = " · ".join(x for x in (str(a.year or ""), f"{len(items)} tracks", total) if x)
    return tracks(a.title, "album", items, Header(a.title, a.artist, meta, a.thumb), subject=a)


def playlist(p: Playlist, items: list[Track]) -> Page:
    total = duration(sum(t.duration_ms for t in items))
    kind = "smart playlist" if p.smart else "playlist"
    return tracks(
        p.title,
        "playlist",
        items,
        Header(p.title, kind, f"{len(items)} tracks · {total}", p.thumb),
        subject=p,
    )


def artist(a: Artist, overview: ArtistOverview) -> Page:
    counts = f"{len(overview.albums)} albums" + (
        f" · {len(overview.singles)} singles & EPs" if overview.singles else ""
    )
    header = Header(a.title, ", ".join(a.genres[:3]), counts, a.thumb)
    page = Page(title=a.title, kind="artist", source=a.title, header=header, subject=a)
    page.entries = [
        Entry("action", "play_all", "Play all"),
        Entry("action", "shuffle_all", "Shuffle"),
        Entry("action", "radio", "Artist radio"),
    ]
    for label, kind, items in (
        ("Popular", "track", overview.popular),
        ("Albums", "album", overview.albums),
        ("Singles & EPs", "album", overview.singles),
        ("Similar artists", "artist", overview.similar),
    ):
        if items:
            page.entries.append(Entry("heading", label=label))
            page.entries += [Entry(kind, item) for item in items]
    return page


def search(query: str, r: SearchResults) -> Page:
    page = Page(title=f"Search “{query}”", kind="search", source=f"Search “{query}”")
    for label, kind, items in (
        ("Tracks", "track", r.tracks),
        ("Artists", "artist", r.artists),
        ("Albums", "album", r.albums),
        ("Playlists", "playlist", r.playlists),
    ):
        if items:
            page.entries.append(Entry("heading", label=label))
            page.entries += [Entry(kind, item) for item in items]
    if r.empty:
        page.entries = [Entry("heading", label=f"nothing matches “{query}”")]
    return page


def queue(q: Queue) -> Page:
    """Played · Now playing · Up next (what you added) · Next from <source>. Each track row
    carries its queue position in `label`."""
    page = Page(title="Queue", kind="queue", source=q.source)
    if not q.tracks:
        page.entries = [Entry("heading", label="the queue is empty: press a on anything to add it")]
        return page

    def section(label: str, start: int, items: tuple) -> None:
        if items:
            page.entries.append(Entry("heading", label=label))
            page.entries += [Entry("track", t, str(start + n)) for n, t in enumerate(items)]

    section("Played", 0, q.history)
    section("Now playing", q.current, (q.track,) if q.track else ())
    page.highlighted = len(page.entries) - 1
    section("Up next", q.current + 1, q.up_next)
    section(f"Next from {q.source}" if q.source else "Next", q.current + 1 + q.queued, q.rest)
    return page
