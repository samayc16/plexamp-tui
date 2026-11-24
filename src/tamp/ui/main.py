"""The main screen: sidebar, the browser you drill into, search, the queue page and the
now-playing panel. What you can do with a row or the queue lives in tamp.ui.commands."""

from __future__ import annotations

import asyncio
from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import Footer, Input, OptionList, Static
from textual.widgets.option_list import Option

from tamp import art
from tamp.errors import describe
from tamp.models import Track
from tamp.ui import fmt, pages
from tamp.ui.browser import Browser, Entry, Page
from tamp.ui.commands import Commands
from tamp.ui.menus import HelpScreen
from tamp.ui.nowplaying import LyricsView, NowPlaying, PlayerScreen


class MainScreen(Commands, Screen):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("space", "app.toggle", "play/pause"),
        Binding("n", "app.next", "next"),
        Binding("p", "app.prev", "prev"),
        # priority: handled the moment it arrives, so keys typed right after it are already routed
        # to the search box, not to the list (where a adds, r starts radio, space pauses)
        Binding("slash", "search", "search", priority=True),
        Binding("m", "menu", "menu"),
        Binding("F", "full_player", "player"),
        Binding("L", "lyrics", "lyrics"),
        Binding("o", "app.choose_output", "play on"),
        Binding("question_mark", "help", "help"),
        Binding("q", "app.quit", "quit"),
        Binding("escape,backspace,h", "back", "back", show=False),
        Binding("a", "add", "add", show=False),
        Binding("N", "add_next", "play next", show=False),
        Binding("s", "shuffle_play", "shuffle", show=False),
        Binding("r", "radio", "radio", show=False),
        Binding("f", "like_selected", "like", show=False),
        Binding("asterisk", "app.like_playing", "like playing", show=False),
        Binding("i", "info", "info", show=False),
        Binding("right,right_square_bracket", "app.seek(10000)", "seek", show=False),
        Binding("left,left_square_bracket", "app.seek(-10000)", "seek", show=False),
        Binding("minus", "app.volume(-5)", "vol-", show=False),
        Binding("plus,equals_sign", "app.volume(5)", "vol+", show=False),
        Binding("R", "repeat", "repeat", show=False),
        Binding("S", "shuffle_queue", "shuffle queue", show=False),
        Binding("d", "remove", "remove", show=False),
        Binding("J,shift+down", "move(1)", "move down", show=False),
        Binding("K,shift+up", "move(-1)", "move up", show=False),
        Binding("C", "clear_upcoming", "clear", show=False),
        Binding("w", "save_queue", "save queue", show=False),
        Binding("P", "add_to_playlist", "add to playlist", show=False),
        Binding("z", "sleep", "sleep", show=False),
        Binding("v", "app.loudness", "leveling", show=False),
        Binding("T", "app.choose_theme", "theme", show=False),
        Binding("j", "cursor(1)", "down", show=False),
        Binding("k", "cursor(-1)", "up", show=False),
        Binding("g", "cursor_edge('top')", "top", show=False),
        Binding("G", "cursor_edge('bottom')", "bottom", show=False),
        *[
            Binding(str(i + 1), f"section('{key}')", label, show=False)
            for i, (key, label) in enumerate(pages.SECTIONS)
        ],
    ]

    def __init__(self) -> None:
        super().__init__()
        self.pages: list[Page] = []
        self.showing_lyrics = False
        self._search_timer: Timer | None = None
        self._queue_key: tuple = ()

    @property
    def tamp(self):
        return self.app  # TampApp: .library, .player, .now_track, …

    def compose(self) -> ComposeResult:
        yield Static("", id="topbar")
        with Horizontal(id="body"):
            yield OptionList(
                *[
                    Option(Text.assemble((f"{n} ", fmt.DIM), (label, fmt.FG)), id=key)
                    for n, (key, label) in enumerate(pages.SECTIONS, start=1)
                ],
                markup=False,
                id="nav",
            )
            with Vertical(id="center"):
                with Horizontal(id="page-header"):
                    yield Static("", id="page-art")
                    yield Static("", id="page-meta")
                yield Static("", id="crumb")
                yield Input(placeholder="search artists, albums, tracks, playlists…", id="search")
                yield Browser(markup=False, id="browser")
                yield LyricsView("", id="lyrics")
            yield NowPlaying(id="np")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#search").display = False
        self.query_one("#lyrics").display = False
        self.query_one("#page-header").display = False
        self.update_topbar()
        await self.open_section("home")
        self.query_one("#browser").focus()

    def update_topbar(self) -> None:
        app = self.tamp
        who = f"{app.cfg.user_title} @ " if app.cfg.user_title and not app.demo and not app.hide_names else ""
        library = app.library.name if app.library else ""
        if app.hide_names:
            library = library.split(" · ")[0]  # the section's name, not the server's
        private = "  · private session" if app.private else ""
        self.query_one("#topbar", Static).update(
            Text.assemble(
                (" tamp ", f"bold {fmt.PINK}"),
                ("  ", ""),
                (f"{who}{library}", fmt.DIM),
                (private, fmt.ORANGE),
            )
        )

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
        if page is None:
            return
        browser = self.query_one("#browser", Browser)
        width = self.row_width(browser)
        now = self.tamp.now_track
        cols = self.track_columns(page, width)
        rows: list[Option | None] = []
        for i, e in enumerate(page.entries):
            if e.kind == "heading":
                if rows:
                    rows.append(None)
                rows.append(Option(fmt.heading(e.label), disabled=True))
            else:
                rows.append(Option(self._row(i, e, width, page, now, cols), id=str(i)))
        browser.draggable = page.kind == "queue"
        browser.clear_options()
        browser.add_options(rows)
        if browser.option_count:
            browser.highlighted = min(page.highlighted, browser.option_count - 1)
        crumb = " › ".join(p.title for p in self.pages)
        self.query_one("#crumb", Static).update(Text(f" {crumb}", style=f"bold {fmt.PINK}"))
        self._show_header(page)

    @staticmethod
    def row_width(browser: Browser) -> int:
        """Cells a row may use: the list's width minus its padding and scrollbar, so long pages
        (which grow a scrollbar) never wrap."""
        # always leave room for the scrollbar: it appears only once the rows are in
        scrollbar = browser.styles.scrollbar_size_vertical if not browser.show_vertical_scrollbar else 0
        return max(30, browser.scrollable_content_region.width - scrollbar - 2)

    @staticmethod
    def track_columns(page: Page, width: int) -> fmt.Columns:
        tracks = [e.value for e in page.entries if e.kind == "track"]
        if page.kind == "queue":
            numbers = [int(e.label) + 1 for e in page.entries if e.kind == "track"]
        elif page.kind == "album":
            numbers = [t.index or 0 for t in tracks]  # type: ignore[union-attr]
        else:
            numbers = []
        return fmt.columns(tracks, width, show_album=page.kind != "album", numbers=numbers)  # type: ignore[arg-type]

    def _row(self, i: int, e: Entry, width: int, page: Page, now: Track | None, cols: fmt.Columns) -> Text:
        if e.kind == "track":
            t: Track = e.value  # type: ignore[assignment]
            if page.kind == "queue":
                position = int(e.label)
                return fmt.track_row(
                    t, cols, playing=position == self.tamp.player.queue.current, number=position + 1
                )
            album = page.kind == "album"
            playing = now is not None and now.id == t.id
            return fmt.track_row(t, cols, playing=playing, number=t.index if album else None)
        if e.kind == "artist":
            return fmt.artist_row(e.value, width)  # type: ignore[arg-type]
        if e.kind == "album":
            return fmt.album_row(e.value, width, show_artist=page.kind != "artist")  # type: ignore[arg-type]
        if e.kind in ("playlist", "station"):
            return fmt.playlist_row(e.value, width)  # type: ignore[arg-type]
        icon, colour = {
            "play": ("▶", fmt.GREEN),
            "play_all": ("▶", fmt.GREEN),
            "shuffle": ("⇄", fmt.YELLOW),
            "shuffle_all": ("⇄", fmt.YELLOW),
            "radio": ("✦", fmt.ORANGE),
        }.get(str(e.value), ("•", fmt.FG))
        return fmt.action_row(icon, e.label, colour)

    def _show_header(self, page: Page) -> None:
        box = self.query_one("#page-header")
        h = page.header
        box.display = h is not None and self.size.height >= 24
        self.query_one("#crumb").display = not box.display
        if not box.display or h is None:
            return
        trail = " › ".join(p.title for p in self.pages[:-1])
        self.query_one("#page-meta", Static).update(
            Text.assemble(
                (trail + "\n" if trail else "\n", fmt.DIM),
                (h.title + "\n", f"bold {fmt.FG}"),
                (h.subtitle + "\n", fmt.PINK),
                (h.meta, fmt.DIM),
            )
        )
        self.query_one("#page-art", Static).update(Text())
        self.run_worker(self._load_header_art(page, h.thumb), group="header-art", exclusive=True)

    async def _load_header_art(self, page: Page, thumb: str | None) -> None:
        try:
            data = await asyncio.to_thread(self.tamp.library.art, thumb, 200) if thumb else None
        except Exception:
            data = None
        if data and self.page is page:
            self.query_one("#page-art", Static).update(art.render(data, 12, 6))

    def push(self, page: Page, replace_all: bool = False, replace_top: bool = False) -> None:
        if self.page:
            self.page.highlighted = self.query_one("#browser", Browser).highlighted or 0
        if replace_all:
            self.pages = []
        elif replace_top and self.pages:
            self.pages.pop()
        self.pages.append(page)
        self.render_page()

    async def load(
        self, title: str, build, fn, *args, replace_all: bool = False, replace_top: bool = False
    ) -> None:
        """Run a library call off the UI thread, then show `build(result)`."""
        self.query_one("#crumb", Static).update(Text(f" {title} …", style=fmt.DIM))
        try:
            result = await asyncio.to_thread(fn, *args)
        except Exception as e:
            self.notify(f"{title}: {describe(e)}", severity="error")
            self.render_page()
            return
        self.push(build(result), replace_all=replace_all, replace_top=replace_top)

    async def open_section(self, key: str) -> None:
        lib = self.tamp.library
        self.show_lyrics(False)
        nav = self.query_one("#nav", OptionList)
        nav.highlighted = next((i for i, (k, _) in enumerate(pages.SECTIONS) if k == key), 0)
        if key == "queue":
            self.push(pages.queue(self.tamp.player.queue), replace_all=True)
            return
        if key == "home":
            await self.open_home()
            return
        loaders = {
            "artists": (
                "Artists",
                lambda r: self.paged(pages.listing("Artists", "artists", r), lib.artists),
                lib.artists,
            ),
            "albums": (
                "Albums",
                lambda r: self.paged(pages.listing("Albums", "albums", r), lib.albums),
                lib.albums,
            ),
            "playlists": ("Playlists", lambda r: pages.listing("Playlists", "playlists", r), lib.playlists),
            "stations": ("Stations", lambda r: pages.listing("Stations", "stations", r), lib.stations),
            "recent": (
                "Recently added",
                lambda r: pages.listing("Recently added", "recent", r),
                lib.recently_added,
            ),
            "liked": ("Liked", lambda r: pages.tracks("♥ Liked", "liked", r), lib.liked),
        }
        title, build, fn = loaders[key]
        await self.load(title, build, fn, replace_all=True)

    @staticmethod
    def paged(page: Page, fn) -> Page:
        page.loader = lambda start: fn(start=start)
        return page

    async def open_home(self) -> None:
        """Last session's home at once (if any), then the fresh one when the server answers."""
        app = self.tamp
        cached = app.cached_home()
        shown = None
        if cached:
            shown = pages.home(cached)
            self.push(shown, replace_all=True)
        try:
            hubs = await asyncio.to_thread(app.library.home)
        except Exception as e:
            if shown is None:
                self.notify(f"Home: {describe(e)}", severity="error")
            return
        app.store_home(hubs)
        if shown is None:
            self.push(pages.home(hubs), replace_all=True)
        elif self.page is shown:  # still looking at it: swap in the fresh one, keep the cursor
            fresh = pages.home(hubs)
            fresh.highlighted = self.query_one("#browser", Browser).highlighted or 0
            self.pages[-1] = fresh
            self.render_page()

    async def load_more(self) -> None:
        page = self.page
        if page is None or page.loader is None:
            return
        start = sum(1 for e in page.entries if e.kind == page.entry_kind)
        self.query_one("#crumb", Static).update(Text(f" {page.title} … loading more", style=fmt.DIM))
        items = await asyncio.to_thread(page.loader, start)
        page.highlighted = pages.extend(page, items)
        self.render_page()

    def refresh_queue_page(self) -> None:
        """Rebuild the queue page only when the queue itself changed, not on every tick."""
        q = self.tamp.player.queue
        key = (tuple(t.id for t in q.tracks), q.current, q.queued, tuple(t.liked for t in q.tracks))
        changed, self._queue_key = key != self._queue_key, key
        if not changed or not self.page or self.page.kind != "queue":
            return
        highlighted = self.query_one("#browser", Browser).highlighted or 0
        self.pages[-1] = pages.queue(q)
        self.pages[-1].highlighted = highlighted
        self.render_page()

    def on_queue_track(self, entry: Entry | None) -> bool:
        return bool(self.page and self.page.kind == "queue" and entry and entry.kind == "track")

    def focus_queue_position(self, position: int) -> None:
        """After a move, rebuild the queue page and keep the cursor on the moved track."""
        self.refresh_queue_page()
        if not self.page or self.page.kind != "queue":
            return
        found = next(
            (i for i, e in enumerate(self.page.entries) if e.kind == "track" and e.label == str(position)),
            None,
        )
        if found is not None:
            self.page.highlighted = found
            self.query_one("#browser", Browser).highlighted = found

    def rerender(self) -> None:
        if self.page:
            self.page.highlighted = self.query_one("#browser", Browser).highlighted or 0
        self.render_page()

    # ── selection ──
    def selected(self) -> tuple[Entry | None, int]:
        browser = self.query_one("#browser", Browser)
        if browser.highlighted is None or not self.page:
            return None, -1
        option = browser.get_option_at_index(browser.highlighted)
        if option.id is None:
            return None, -1
        i = int(option.id)
        return self.page.entries[i], i

    async def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "nav":
            await self.open_section(event.option.id or "home")
            if event.option_list.has_focus:  # not if you moved on (to search) while it loaded
                self.query_one("#browser").focus()
        elif event.option_list.id == "browser":
            await self.activate()

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
        for widget_id in ("#browser", "#nav"):
            widget = self.query_one(widget_id, OptionList)
            if widget.has_focus:
                widget.action_cursor_down() if delta > 0 else widget.action_cursor_up()

    def action_cursor_edge(self, edge: str) -> None:
        browser = self.query_one("#browser", Browser)
        browser.action_first() if edge == "top" else browser.action_last()

    def action_search(self) -> None:
        self.show_lyrics(False)
        search = self.query_one("#search", Input)
        if search.has_focus:  # a / typed in the search itself ("AC/DC")
            search.insert_text_at_cursor("/")
            return
        search.display = True
        search.value = ""
        # focus now, not on the next tick: keys typed straight after / may already be queued, and
        # in the list they're commands (a adds, r starts radio, space pauses)
        self.set_focus(search)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "search":
            return
        if self._search_timer:
            self._search_timer.stop()
        query = event.value.strip()
        if len(query) >= 2:
            self._search_timer = self.set_timer(
                0.35, lambda: self.run_worker(self.run_search(query), group="search", exclusive=True)
            )

    async def run_search(self, query: str) -> None:
        replace_top = bool(self.page and self.page.kind == "search")
        lib = self.tamp.library
        await self.load(
            f"Search “{query}”", lambda r: pages.search(query, r), lib.search, query, replace_top=replace_top
        )

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search":
            event.input.display = False
            self.query_one("#browser").focus()

    def action_help(self) -> None:
        self.app.push_screen(HelpScreen())

    def action_full_player(self) -> None:
        self.app.push_screen(PlayerScreen())
        self.tamp.refresh_art()

    def action_lyrics(self) -> None:
        self.show_lyrics(not self.showing_lyrics)

    def show_lyrics(self, on: bool) -> None:
        self.showing_lyrics = on
        self.query_one("#browser").display = not on
        self.query_one("#lyrics").display = on
        if on:
            self.tick()

    # ── updates from the app ──
    def restyle(self) -> None:
        """The theme changed: redraw everything that has colours baked into its text."""
        nav = self.query_one("#nav", OptionList)
        for n, (key, label) in enumerate(pages.SECTIONS, start=1):
            nav.replace_option_prompt(key, Text.assemble((f"{n} ", fmt.DIM), (label, fmt.FG)))
        self.update_topbar()
        self.rerender()
        self.tick()

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

    def replace_track(self, track: Track) -> None:
        """Show a track's new details (liked) wherever it's listed."""
        for page in self.pages:
            page.entries = [
                Entry(e.kind, track, e.label)
                if e.kind == "track" and getattr(e.value, "id", None) == track.id
                else e
                for e in page.entries
            ]
        self.rerender()
