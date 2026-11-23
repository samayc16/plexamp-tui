"""The main screen: sidebar, a browser you drill into (artist → album → tracks), search, the
queue, the now-playing panel and lyrics."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Input, OptionList, Static
from textual.widgets.option_list import Option

from tamp.models import Album, Artist, Playlist, Track
from tamp.ui import fmt
from tamp.ui.widgets import LyricsView, NowPlaying

SECTIONS = [
    ("recent", "◷ Recently added"),
    ("artists", "◉ Artists"),
    ("albums", "◎ Albums"),
    ("playlists", "≡ Playlists"),
    ("stations", "✦ Stations"),
    ("played", "↺ Recently played"),
    ("queue", "» Queue"),
]


@dataclass(frozen=True)
class Entry:
    kind: str  # artist album track playlist station action heading
    value: object = None
    label: str = ""


@dataclass
class Page:
    title: str
    entries: list[Entry] = field(default_factory=list)
    tracks: list[Track] = field(default_factory=list)  # what "play from here" plays
    source: str = ""
    kind: str = ""
    highlighted: int = 0


class Prompt(ModalScreen[str]):
    """Ask for a line of text (playlist name)."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "cancel")]

    def __init__(self, question: str, value: str = "") -> None:
        super().__init__()
        self.question, self.value = question, value

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt"):
            yield Static(Text(self.question, style=f"bold {fmt.YELLOW}"))
            yield Input(value=self.value, id="prompt-input")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())

    def action_cancel(self) -> None:
        self.dismiss("")


class Choose(ModalScreen[int]):
    """Pick one of a few options; dismisses with the index or -1."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape,q", "cancel", "cancel")]

    def __init__(self, title: str, rows: list[Text], current: int = 0) -> None:
        super().__init__()
        self.title_text, self.rows, self.current = title, rows, current

    def compose(self) -> ComposeResult:
        with Vertical(id="choose"):
            yield Static(Text(self.title_text, style=f"bold {fmt.PINK}"))
            yield OptionList(
                *[Option(r, id=str(i)) for i, r in enumerate(self.rows)], markup=False, id="choose-list"
            )

    def on_mount(self) -> None:
        self.query_one(OptionList).highlighted = self.current

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(int(event.option.id or 0))

    def action_cancel(self) -> None:
        self.dismiss(-1)


HELP = """[b]play[/b]  enter play / open   a add to queue   N play next   s shuffle-play   r radio from this
[b]transport[/b]  space pause   n / p next / previous   ← → or [ ] seek 10s   - + volume   R repeat
[b]library[/b]  1-7 sections   / search   esc back   f like the highlighted track   * like what's playing
[b]queue[/b]  7   d remove   J / K move   C clear upcoming   S shuffle upcoming   w save as playlist
[b]more[/b]  L lyrics   o output device   P add to playlist   z sleep timer   v loudness leveling   q quit"""


class MainScreen(Screen):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("space", "toggle", "play/pause"),
        Binding("n", "next", "next"),
        Binding("p", "prev", "prev"),
        Binding("slash", "search", "search"),
        Binding("L", "lyrics", "lyrics"),
        Binding("o", "output", "output"),
        Binding("question_mark", "help", "help"),
        Binding("q", "app.quit", "quit"),
        Binding("escape,backspace,h", "back", "back", show=False),
        Binding("a", "add", "add", show=False),
        Binding("N", "add_next", "play next", show=False),
        Binding("s", "shuffle_play", "shuffle", show=False),
        Binding("r", "radio", "radio", show=False),
        Binding("f", "like_selected", "like", show=False),
        Binding("asterisk", "like_playing", "like playing", show=False),
        Binding("right,right_square_bracket", "seek(10000)", "seek", show=False),
        Binding("left,left_square_bracket", "seek(-10000)", "seek", show=False),
        Binding("minus", "volume(-5)", "vol-", show=False),
        Binding("plus,equals_sign", "volume(5)", "vol+", show=False),
        Binding("R", "repeat", "repeat", show=False),
        Binding("S", "shuffle_queue", "shuffle queue", show=False),
        Binding("d", "remove", "remove", show=False),
        Binding("J", "move(1)", "move down", show=False),
        Binding("K", "move(-1)", "move up", show=False),
        Binding("C", "clear_upcoming", "clear", show=False),
        Binding("w", "save_queue", "save queue", show=False),
        Binding("P", "add_to_playlist", "add to playlist", show=False),
        Binding("z", "sleep", "sleep", show=False),
        Binding("v", "loudness", "leveling", show=False),
        Binding("j", "cursor(1)", "down", show=False),
        Binding("k", "cursor(-1)", "up", show=False),
        Binding("g", "cursor_edge('top')", "top", show=False),
        Binding("G", "cursor_edge('bottom')", "bottom", show=False),
        *[
            Binding(str(i + 1), f"section('{key}')", label, show=False)
            for i, (key, label) in enumerate(SECTIONS)
        ],
    ]

    def __init__(self) -> None:
        super().__init__()
        self.pages: list[Page] = []
        self.showing_lyrics = False

    @property
    def tamp(self):
        return self.app  # TampApp: .library, .player, .lyrics_for(), …

    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            yield OptionList(
                *[Option(Text(label), id=key) for key, label in SECTIONS], markup=False, id="nav"
            )
            with Vertical(id="center"):
                yield Static("", id="crumb")
                yield Input(placeholder="search artists, albums, tracks, playlists", id="search")
                yield OptionList(markup=False, id="browser")
                yield LyricsView("", id="lyrics")
            yield NowPlaying(id="np")
        yield Static("", id="hint")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#search").display = False
        self.query_one("#lyrics").display = False
        self.query_one("#hint", Static).update(Text(" ? for every key", style=fmt.DIM))
        await self.open_section("recent")
        self.query_one("#browser").focus()

    def on_resize(self) -> None:
        self.query_one("#np").display = self.size.width >= 100
        self.query_one("#nav").display = self.size.width >= 70
        self.render_page()

    # ── pages ──
    @property
    def page(self) -> Page | None:
        return self.pages[-1] if self.pages else None

    def render_page(self) -> None:
        page = self.page
        browser = self.query_one("#browser", OptionList)
        if page is None:
            return
        width = max(30, browser.size.width - 4)
        playing = self.tamp.player.queue.track if self.tamp.player else None
        rows: list[Option | None] = []
        for i, e in enumerate(page.entries):
            if e.kind == "heading":
                if rows:
                    rows.append(None)  # a separator line before every heading but the first
                rows.append(Option(fmt.heading(e.label), disabled=True))
                continue
            rows.append(Option(self._row(e, width, page, playing), id=str(i)))
        browser.clear_options()
        browser.add_options(rows)
        if browser.option_count:
            browser.highlighted = min(page.highlighted, browser.option_count - 1)
        crumb = " › ".join(p.title for p in self.pages)
        self.query_one("#crumb", Static).update(Text(f" {crumb}", style=f"bold {fmt.PINK}"))

    def _row(self, e: Entry, width: int, page: Page, playing: Track | None) -> Text:
        if e.kind == "track":
            t: Track = e.value  # type: ignore[assignment]
            is_queue = page.kind == "queue"
            number = page.tracks.index(t) + 1 if is_queue and t in page.tracks else None
            now = (is_queue and number is not None and number - 1 == self.tamp.player.queue.current) or (
                not is_queue and playing is not None and playing.id == t.id
            )
            return fmt.track_row(t, width, playing=now, show_album=page.kind not in ("album",), number=number)
        if e.kind == "artist":
            return fmt.artist_row(e.value, width)  # type: ignore[arg-type]
        if e.kind == "album":
            return fmt.album_row(e.value, width, show_artist=page.kind != "artist")  # type: ignore[arg-type]
        if e.kind in ("playlist", "station"):
            return fmt.playlist_row(e.value, width)  # type: ignore[arg-type]
        icon, colour = {
            "play": ("▶", fmt.GREEN),
            "shuffle": ("⇄", fmt.YELLOW),
            "radio": ("✦", fmt.ORANGE),
        }.get(str(e.value), ("•", fmt.FG))
        return fmt.action_row(icon, e.label, colour)

    def push(self, page: Page, replace_all: bool = False) -> None:
        if self.page:
            self.page.highlighted = self.query_one("#browser", OptionList).highlighted or 0
        if replace_all:
            self.pages = []
        self.pages.append(page)
        self.render_page()

    async def load(self, title: str, kind: str, fn, *args, replace_all: bool = False) -> None:
        """Run a library call off the UI thread, then show its result as a page."""
        self.query_one("#crumb", Static).update(Text(f" {title} …", style=fmt.DIM))
        try:
            result = await asyncio.to_thread(fn, *args)
        except Exception as e:
            self.notify(f"{title}: {e}", severity="error")
            self.render_page()
            return
        self.push(self.make_page(title, kind, result), replace_all=replace_all)

    def make_page(self, title: str, kind: str, items) -> Page:
        page = Page(title=title, kind=kind, source=title)
        if kind in ("tracks", "album", "playlist", "played"):
            page.tracks = list(items)
            if items:
                page.entries += [Entry("action", "play", "Play"), Entry("action", "shuffle", "Shuffle")]
            page.entries += [Entry("track", t) for t in items]
        elif kind == "artists":
            page.entries = [Entry("artist", a) for a in items]
        elif kind in ("albums", "recent"):
            page.entries = [Entry("album", a) for a in items]
        elif kind == "playlists":
            page.entries = [Entry("playlist", p) for p in items]
        elif kind == "stations":
            page.entries = [Entry("station", s) for s in items]
        elif kind == "artist":
            albums, tracks = items
            page.tracks = tracks
            page.entries = [
                Entry("action", "play", "Play all"),
                Entry("action", "shuffle", "Shuffle"),
                Entry("action", "radio", "Artist radio"),
            ]
            page.entries += [Entry("heading", label="Albums")] + [Entry("album", a) for a in albums]
        elif kind == "search":
            r = items
            for label, entries in (
                ("Artists", [Entry("artist", a) for a in r.artists]),
                ("Albums", [Entry("album", a) for a in r.albums]),
                ("Tracks", [Entry("track", t) for t in r.tracks]),
                ("Playlists", [Entry("playlist", p) for p in r.playlists]),
            ):
                if entries:
                    page.entries += [Entry("heading", label=label)] + entries
            page.tracks = list(r.tracks)
            if r.empty:
                page.entries = [Entry("heading", label="nothing found")]
        return page

    async def open_section(self, key: str) -> None:
        lib = self.tamp.library
        self.show_lyrics(False)
        if key == "queue":
            self.push(self.queue_page(), replace_all=True)
            return
        loaders = {
            "recent": ("Recently added", "recent", lib.recently_added),
            "artists": ("Artists", "artists", lib.artists),
            "albums": ("Albums", "albums", lib.albums),
            "playlists": ("Playlists", "playlists", lib.playlists),
            "stations": ("Stations", "stations", lib.stations),
            "played": ("Recently played", "played", lib.recently_played),
        }
        title, kind, fn = loaders[key]
        await self.load(title, kind, fn, replace_all=True)

    def queue_page(self) -> Page:
        q = self.tamp.player.queue
        page = Page(title="Queue", kind="queue", source=q.source, tracks=list(q.tracks))
        page.entries = [Entry("track", t) for t in q.tracks]
        page.highlighted = max(0, q.current)
        if not q.tracks:
            page.entries = [Entry("heading", label="the queue is empty: press a on anything to add it")]
        return page

    def rerender(self) -> None:
        if self.page:
            self.page.highlighted = self.query_one("#browser", OptionList).highlighted or 0
        self.render_page()

    def refresh_queue_page(self) -> None:
        if self.page and self.page.kind == "queue":
            highlighted = self.query_one("#browser", OptionList).highlighted or 0
            self.pages[-1] = self.queue_page()
            self.pages[-1].highlighted = highlighted
            self.render_page()

    # ── selection ──
    def selected(self) -> tuple[Entry | None, int]:
        browser = self.query_one("#browser", OptionList)
        if browser.highlighted is None or not self.page:
            return None, -1
        option = browser.get_option_at_index(browser.highlighted)
        if option.id is None:
            return None, -1
        i = int(option.id)
        return self.page.entries[i], i

    async def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "nav":
            await self.open_section(event.option.id or "recent")
            self.query_one("#browser").focus()
            return
        await self.activate()

    async def activate(self) -> None:
        entry, _ = self.selected()
        page = self.page
        if entry is None or page is None:
            return
        lib, player = self.tamp.library, self.tamp.player
        if entry.kind == "artist":
            artist: Artist = entry.value  # type: ignore[assignment]
            await self.load(artist.title, "artist", lambda: (lib.albums(artist), lib.artist_tracks(artist)))
        elif entry.kind == "album":
            album: Album = entry.value  # type: ignore[assignment]
            await self.load(album.title, "album", lib.tracks, album)
        elif entry.kind == "playlist":
            pl: Playlist = entry.value  # type: ignore[assignment]
            await self.load(pl.title, "playlist", lib.playlist_tracks, pl)
        elif entry.kind == "station":
            st: Playlist = entry.value  # type: ignore[assignment]
            await self.play_from(lib.station_tracks, st, source=f"Radio · {st.title}")
        elif entry.kind == "track":
            if page.kind == "queue":
                await player.jump(page.tracks.index(entry.value))
            else:
                await player.play(page.tracks, page.tracks.index(entry.value), source=page.source)
        elif entry.kind == "action":
            if entry.value == "play":
                await player.play(page.tracks, 0, source=page.source)
            elif entry.value == "shuffle":
                await self.shuffle_play(page.tracks, page.source)
            elif entry.value == "radio":
                await self.action_radio()

    async def play_from(self, fn, *args, source: str) -> None:
        try:
            tracks = await asyncio.to_thread(fn, *args)
        except Exception as e:
            self.notify(f"{source}: {e}", severity="error")
            return
        if not tracks:
            self.notify(f"{source}: nothing to play", severity="warning")
            return
        await self.tamp.player.play(tracks, 0, source=source)

    async def shuffle_play(self, tracks: list[Track], source: str) -> None:
        import random

        mixed = random.sample(tracks, len(tracks))
        await self.tamp.player.play(mixed, 0, source=f"{source} · shuffled")

    async def tracks_of(self, entry: Entry) -> list[Track]:
        lib = self.tamp.library
        if entry.kind == "track":
            return [entry.value]  # type: ignore[list-item]
        if entry.kind == "album":
            return await asyncio.to_thread(lib.tracks, entry.value)
        if entry.kind == "artist":
            return await asyncio.to_thread(lib.artist_tracks, entry.value)
        if entry.kind == "playlist":
            return await asyncio.to_thread(lib.playlist_tracks, entry.value)
        if entry.kind == "action" and self.page:
            return list(self.page.tracks)
        return []

    # ── actions ──
    async def action_back(self) -> None:
        if self.showing_lyrics:
            self.show_lyrics(False)
            return
        search = self.query_one("#search", Input)
        if search.display and search.has_focus:
            search.display = False
            self.query_one("#browser").focus()
            return
        if len(self.pages) > 1:
            self.pages.pop()
            self.render_page()

    async def action_section(self, key: str) -> None:
        await self.open_section(key)

    def action_cursor(self, delta: int) -> None:
        browser = self.query_one("#browser", OptionList)
        if browser.has_focus:
            browser.action_cursor_down() if delta > 0 else browser.action_cursor_up()
        elif (nav := self.query_one("#nav", OptionList)).has_focus:
            nav.action_cursor_down() if delta > 0 else nav.action_cursor_up()

    def action_cursor_edge(self, edge: str) -> None:
        browser = self.query_one("#browser", OptionList)
        browser.action_first() if edge == "top" else browser.action_last()

    def action_search(self) -> None:
        self.show_lyrics(False)
        search = self.query_one("#search", Input)
        search.display = True
        search.value = ""
        search.focus()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "search" or not event.value.strip():
            return
        query = event.value.strip()
        event.input.display = False
        self.query_one("#browser").focus()
        await self.load(f"Search “{query}”", "search", self.tamp.library.search, query)

    async def action_toggle(self) -> None:
        await self.tamp.player.toggle()

    async def action_next(self) -> None:
        await self.tamp.player.next()

    async def action_prev(self) -> None:
        await self.tamp.player.prev()

    async def action_seek(self, delta: int) -> None:
        await self.tamp.player.seek_by(delta)

    async def action_volume(self, delta: int) -> None:
        await self.tamp.player.volume_by(delta)

    async def action_repeat(self) -> None:
        await self.tamp.player.cycle_repeat()
        self.notify(f"repeat {self.tamp.player.queue.repeat}")

    async def action_add(self) -> None:
        entry, _ = self.selected()
        if entry and (tracks := await self.tracks_of(entry)):
            await self.tamp.player.append(tracks)
            self.notify(f"added {len(tracks)} to the queue")

    async def action_add_next(self) -> None:
        entry, _ = self.selected()
        if entry and (tracks := await self.tracks_of(entry)):
            await self.tamp.player.play_next(tracks)
            self.notify(f"{len(tracks)} playing next")

    async def action_shuffle_play(self) -> None:
        entry, _ = self.selected()
        if entry and (tracks := await self.tracks_of(entry)):
            await self.shuffle_play(tracks, entry.label or getattr(entry.value, "title", "selection"))

    async def action_radio(self) -> None:
        """Track → sonically similar; artist → artist radio; on an artist page → that artist."""
        entry, _ = self.selected()
        lib = self.tamp.library
        target = entry.value if entry else None
        if entry and entry.kind == "action" and self.page and self.page.kind == "artist":
            target = next((e.value for e in self.page.entries if e.kind == "album"), None)
            artist_name = self.page.title
            artists = await asyncio.to_thread(lib.search, artist_name)
            target = next((a for a in artists.artists if a.title == artist_name), None)
        if isinstance(target, Track):
            await self.play_from(lib.similar, target, source=f"Radio · like {target.title}")
        elif isinstance(target, Artist):
            await self.play_from(lib.artist_radio, target, source=f"Radio · {target.title}")
        elif isinstance(target, Album):
            tracks = await asyncio.to_thread(lib.tracks, target)
            if tracks:
                await self.play_from(lib.similar, tracks[0], source=f"Radio · {target.title}")
        else:
            self.notify("radio works on a track, album or artist", severity="warning")

    async def action_like_selected(self) -> None:
        entry, _ = self.selected()
        if entry and entry.kind == "track":
            await self.tamp.toggle_like(entry.value)

    async def action_like_playing(self) -> None:
        if self.tamp.player.queue.track:
            await self.tamp.toggle_like(self.tamp.player.queue.track)

    async def action_remove(self) -> None:
        entry, _ = self.selected()
        if self.page and self.page.kind == "queue" and entry and entry.kind == "track":
            await self.tamp.player.remove(self.page.tracks.index(entry.value))

    async def action_move(self, delta: int) -> None:
        entry, _ = self.selected()
        if self.page and self.page.kind == "queue" and entry and entry.kind == "track":
            i = self.page.tracks.index(entry.value)
            await self.tamp.player.move(i, i + delta)
            self.query_one("#browser", OptionList).highlighted = max(0, i + delta)

    async def action_clear_upcoming(self) -> None:
        await self.tamp.player.clear_upcoming()

    async def action_shuffle_queue(self) -> None:
        await self.tamp.player.shuffle()
        self.notify("shuffled what's up next")

    def action_save_queue(self) -> None:
        tracks = list(self.tamp.player.queue.tracks)
        if not tracks:
            self.notify("the queue is empty", severity="warning")
            return

        async def save(name: str) -> None:
            if name:
                try:
                    await asyncio.to_thread(self.tamp.library.create_playlist, name, tracks)
                    self.notify(f"saved playlist “{name}”")
                except Exception as e:
                    self.notify(f"couldn't save: {e}", severity="error")

        self.app.push_screen(
            Prompt("Save the queue as a playlist named:", self.tamp.player.queue.source), save
        )

    def action_add_to_playlist(self) -> None:
        self.run_worker(self._add_to_playlist(), exclusive=True)

    async def _add_to_playlist(self) -> None:
        entry, _ = self.selected()
        if entry is None:
            return
        tracks = await self.tracks_of(entry)
        lib = self.tamp.library
        playlists = [p for p in await asyncio.to_thread(lib.playlists) if not p.smart and not p.radio]
        rows = [fmt.action_row("+", "New playlist…")] + [fmt.playlist_row(p, 40) for p in playlists]
        choice = await self.app.push_screen_wait(Choose("Add to playlist", rows))
        if choice < 0:
            return
        if choice == 0:
            name = await self.app.push_screen_wait(Prompt("New playlist name:"))
            if name:
                await asyncio.to_thread(lib.create_playlist, name, tracks)
                self.notify(f"created “{name}”")
            return
        target = playlists[choice - 1]
        await asyncio.to_thread(lib.add_to_playlist, target, tracks)
        self.notify(f"added {len(tracks)} to “{target.title}”")

    def action_output(self) -> None:
        self.run_worker(self.tamp.choose_output(), exclusive=True)

    def action_sleep(self) -> None:
        options = [None, 15, 30, 45, 60, 90]
        rows = [Text("  off") if m is None else Text(f"  in {m} minutes") for m in options]

        def chosen(i: int) -> None:
            if i >= 0:
                self.tamp.player.sleep_in(options[i])
                self.notify("sleep timer off" if options[i] is None else f"pausing in {options[i]} minutes")

        self.app.push_screen(Choose("Sleep timer", rows), chosen)

    async def action_loudness(self) -> None:
        await self.tamp.toggle_loudness()

    def action_help(self) -> None:
        self.notify(HELP, title="keys", timeout=20)

    def action_lyrics(self) -> None:
        self.show_lyrics(not self.showing_lyrics)

    def show_lyrics(self, on: bool) -> None:
        self.showing_lyrics = on
        self.query_one("#browser").display = not on
        self.query_one("#lyrics").display = on
        if on:
            self.tick()

    def replace_track(self, track: Track) -> None:
        """Show a track's new details (liked) wherever it's listed."""
        for page in self.pages:
            page.entries = [
                Entry(e.kind, track, e.label)
                if e.kind == "track" and getattr(e.value, "id", None) == track.id
                else e
                for e in page.entries
            ]
            page.tracks = [track if t.id == track.id else t for t in page.tracks]
        highlighted = self.query_one("#browser", OptionList).highlighted or 0
        if self.page:
            self.page.highlighted = highlighted
        self.render_page()

    # ── updates from the app ──
    def tick(self) -> None:
        """Redraw what moves: progress, lyrics."""
        np = self.query_one(NowPlaying)
        app = self.tamp
        np.state, np.queue, np.lyrics = app.player.state, app.player.queue, app.current_lyrics
        np.device, np.sleep_at, np.loudness = app.device_name, app.player.sleep_at, app.loudness
        np.refresh_view()
        if self.showing_lyrics:
            track = app.player.state.track
            title = f"{track.title} · {track.artist}" if track else ""
            self.query_one(LyricsView).show(app.current_lyrics, app.player.state.position_ms, title)
