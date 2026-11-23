"""The main screen: sidebar, the browser you drill into, search, context menus, the queue,
and the now-playing panel."""

from __future__ import annotations

import asyncio
import functools
import random
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
from tamp.models import Album, Artist, Playlist, Track
from tamp.ui import fmt, pages
from tamp.ui.browser import Browser, Entry, Page, all_tracks, track_block
from tamp.ui.menus import Choose, ContextMenu, HelpScreen, InfoScreen, MenuItem, Prompt
from tamp.ui.nowplaying import LyricsView, NowPlaying, PlayerScreen, Seek


def guarded(fn):
    """Run an action; a failure (network, a player that refuses) becomes a message, never a crash."""

    @functools.wraps(fn)
    async def wrapper(self, *args, **kwargs):
        try:
            return await fn(self, *args, **kwargs)
        except Exception as e:
            self.notify(str(e) or type(e).__name__, severity="error")

    return wrapper


def menu_items(entry: Entry, page_kind: str, liked: bool) -> list[MenuItem]:
    if entry.kind == "track":
        if page_kind == "queue":
            items: list[MenuItem] = [
                ("play", "Play now", "enter"),
                ("move_next", "Play next", ""),
                ("remove", "Remove from queue", "d"),
            ]
        else:
            items = [
                ("play", "Play from here", "enter"),
                ("play_next", "Play next", "N"),
                ("add", "Add to queue", "a"),
            ]
        return items + [
            ("radio", "Start radio (similar tracks)", "r"),
            ("go_album", "Go to album", ""),
            ("go_artist", "Go to artist", ""),
            ("add_playlist", "Add to playlist…", "P"),
            ("like", "Unlike" if liked else "Like", "f"),
            ("info", "Track info", "i"),
        ]
    if entry.kind == "album":
        return [
            ("open", "Open", "enter"),
            ("play", "Play", ""),
            ("shuffle", "Shuffle", "s"),
            ("play_next", "Play next", "N"),
            ("add", "Add to queue", "a"),
            ("radio", "Album radio", "r"),
            ("go_artist", "Go to artist", ""),
            ("add_playlist", "Add to playlist…", "P"),
        ]
    if entry.kind == "artist":
        return [
            ("open", "Open", "enter"),
            ("play", "Play all", ""),
            ("shuffle", "Shuffle", "s"),
            ("radio", "Artist radio", "r"),
            ("play_next", "Play next", "N"),
            ("add", "Add to queue", "a"),
        ]
    if entry.kind == "playlist":
        return [
            ("open", "Open", "enter"),
            ("play", "Play", ""),
            ("shuffle", "Shuffle", "s"),
            ("play_next", "Play next", "N"),
            ("add", "Add to queue", "a"),
        ]
    if entry.kind == "station":
        return [("play", "Play station", "enter")]
    return []


class MainScreen(Screen):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("space", "app.toggle", "play/pause"),
        Binding("n", "app.next", "next"),
        Binding("p", "app.prev", "prev"),
        Binding("slash", "search", "search"),
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
        Binding("J", "move(1)", "move down", show=False),
        Binding("K", "move(-1)", "move up", show=False),
        Binding("C", "clear_upcoming", "clear", show=False),
        Binding("w", "save_queue", "save queue", show=False),
        Binding("P", "add_to_playlist", "add to playlist", show=False),
        Binding("z", "sleep", "sleep", show=False),
        Binding("v", "app.loudness", "leveling", show=False),
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
        return max(30, browser.scrollable_content_region.width - 2)

    @staticmethod
    def track_columns(page: Page, width: int) -> fmt.Columns:
        tracks = [e.value for e in page.entries if e.kind == "track"]
        if page.kind == "queue":
            numbers = list(range(1, len(tracks) + 1))
        elif page.kind == "album":
            numbers = [t.index or 0 for t in tracks]  # type: ignore[union-attr]
        else:
            numbers = []
        return fmt.columns(tracks, width, show_album=page.kind != "album", numbers=numbers)  # type: ignore[arg-type]

    def _row(self, i: int, e: Entry, width: int, page: Page, now: Track | None, cols: fmt.Columns) -> Text:
        if e.kind == "track":
            t: Track = e.value  # type: ignore[assignment]
            if page.kind == "queue":
                return fmt.track_row(t, cols, playing=i == self.tamp.player.queue.current, number=i + 1)
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
            self.notify(f"{title}: {e}", severity="error")
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
                self.notify(f"Home: {e}", severity="error")
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
        key = (tuple(t.id for t in q.tracks), q.current, tuple(t.liked for t in q.tracks))
        changed, self._queue_key = key != self._queue_key, key
        if not changed or not self.page or self.page.kind != "queue":
            return
        highlighted = self.query_one("#browser", Browser).highlighted or 0
        self.pages[-1] = pages.queue(q)
        self.pages[-1].highlighted = highlighted
        self.render_page()

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
            self.query_one("#browser").focus()
        elif event.option_list.id == "browser":
            await self.activate()

    @guarded
    async def activate(self) -> None:
        entry, i = self.selected()
        page = self.page
        if entry is None or page is None:
            return
        lib, player = self.tamp.library, self.tamp.player
        if entry.kind == "artist":
            await self.open_artist(entry.value)  # type: ignore[arg-type]
        elif entry.kind == "album":
            await self.open_album(entry.value)  # type: ignore[arg-type]
        elif entry.kind == "playlist":
            pl: Playlist = entry.value  # type: ignore[assignment]
            await self.load(pl.title, lambda r: pages.playlist(pl, r), lib.playlist_tracks, pl)
        elif entry.kind == "station":
            st: Playlist = entry.value  # type: ignore[assignment]
            await self.play_from(lib.station_tracks, st, source=f"Radio · {st.title}")
        elif entry.kind == "track":
            if page.kind == "queue":
                await player.jump(i)
            else:
                tracks, start = track_block(page, i)
                await player.play(tracks, start, source=page.source)
        elif entry.kind == "action":
            await self.run_action(str(entry.value))

    async def open_artist(self, artist: Artist) -> None:
        await self.load(
            artist.title, lambda o: pages.artist(artist, o), self.tamp.library.artist_overview, artist
        )

    async def open_album(self, album: Album) -> None:
        await self.load(album.title, lambda r: pages.album(album, r), self.tamp.library.tracks, album)

    async def run_action(self, action: str) -> None:
        page, lib, player = self.page, self.tamp.library, self.tamp.player
        if page is None:
            return
        if action == "more":
            await self.load_more()
            return
        if action == "play":
            await player.play(all_tracks(page), 0, source=page.source)
        elif action == "shuffle":
            await self.shuffle_play(all_tracks(page), page.source)
        elif action in ("play_all", "shuffle_all") and isinstance(page.subject, Artist):
            tracks = await asyncio.to_thread(lib.artist_tracks, page.subject)
            if action == "play_all":
                await player.play(tracks, 0, source=page.source)
            else:
                await self.shuffle_play(tracks, page.source)
        elif action == "radio":
            await self.radio_for(page.subject)

    async def play_from(self, fn, *args, source: str) -> None:
        tracks = await asyncio.to_thread(fn, *args)
        if not tracks:
            self.notify(f"{source}: nothing to play", severity="warning")
            return
        await self.tamp.player.play(tracks, 0, source=source)

    async def shuffle_play(self, tracks: list[Track], source: str) -> None:
        if tracks:
            await self.tamp.player.play(random.sample(tracks, len(tracks)), 0, source=f"{source} · shuffled")

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
            if isinstance(self.page.subject, Artist):
                return await asyncio.to_thread(lib.artist_tracks, self.page.subject)
            return all_tracks(self.page)
        return []

    async def radio_for(self, target: object) -> None:
        lib = self.tamp.library
        if isinstance(target, Track):
            await self.play_from(lib.similar, target, source=f"Radio · like {target.title}")
        elif isinstance(target, Artist):
            await self.play_from(lib.artist_radio, target, source=f"Radio · {target.title}")
        elif isinstance(target, Album):
            tracks = await asyncio.to_thread(lib.tracks, target)
            if tracks:
                await self.play_from(lib.similar, tracks[0], source=f"Radio · {target.title}")
        else:
            self.notify("radio starts from a track, album or artist", severity="warning")

    # ── context menu ──
    def on_browser_menu_requested(self, message: Browser.MenuRequested) -> None:
        self.open_menu(message.x, message.y)

    def action_menu(self) -> None:
        browser = self.query_one("#browser", Browser)
        row = (browser.highlighted or 0) - int(browser.scroll_offset.y)
        self.open_menu(browser.region.x + 6, browser.region.y + max(0, row) + 1)

    def open_menu(self, x: int, y: int) -> None:
        entry, i = self.selected()
        if entry is None or self.page is None:
            return
        liked = isinstance(entry.value, Track) and entry.value.liked
        items = menu_items(entry, self.page.kind, liked)
        if not items:
            return
        title = str(getattr(entry.value, "title", "") or entry.label)
        self.app.push_screen(
            ContextMenu(title, items, x, y), lambda choice: self.run_worker(self.menu_chose(choice, entry, i))
        )

    @guarded
    async def menu_chose(self, choice: str, entry: Entry, i: int) -> None:
        lib, player = self.tamp.library, self.tamp.player
        value = entry.value
        if not choice:
            return
        if choice == "open":
            await self.activate()
        elif choice == "play":
            if entry.kind == "track":
                await self.activate()
            elif entry.kind == "station":
                await self.play_from(
                    lib.station_tracks, value, source=f"Radio · {getattr(value, 'title', '')}"
                )
            else:
                await player.play(await self.tracks_of(entry), 0, source=getattr(value, "title", ""))
        elif choice == "shuffle":
            await self.shuffle_play(await self.tracks_of(entry), getattr(value, "title", "selection"))
        elif choice == "play_next":
            await self.action_add_next()
        elif choice == "move_next":
            target = player.queue.current + (1 if i > player.queue.current else 0)
            await player.move(i, target)
        elif choice == "add":
            await self.action_add()
        elif choice == "remove":
            await player.remove(i)
        elif choice == "radio":
            await self.radio_for(value)
        elif choice == "go_album" and isinstance(value, Track) and value.album_id:
            await self.open_album(await asyncio.to_thread(lib.album, value.album_id))
        elif choice == "go_artist":
            await self.go_artist(value)
        elif choice == "add_playlist":
            await self._add_to_playlist()
        elif choice == "like" and isinstance(value, Track):
            await self.tamp.toggle_like(value)
        elif choice == "info" and isinstance(value, Track):
            await self.show_info(value)

    async def go_artist(self, value: object) -> None:
        lib = self.tamp.library
        artist = None
        if isinstance(value, Track) and value.artist_id:
            artist = await asyncio.to_thread(lib.artist, value.artist_id)
        elif isinstance(value, Album):
            found = await asyncio.to_thread(lib.search, value.artist)
            artist = next((a for a in found.artists if a.title == value.artist), None)
        if artist:
            await self.open_artist(artist)

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
        search.display = True
        search.value = ""
        search.focus()

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

    @guarded
    async def action_repeat(self) -> None:
        await self.tamp.player.cycle_repeat()
        self.notify(f"repeat {self.tamp.player.queue.repeat}")

    @guarded
    async def action_add(self) -> None:
        entry, _ = self.selected()
        if entry and (tracks := await self.tracks_of(entry)):
            await self.tamp.player.append(tracks)
            self.notify(f"added {len(tracks)} to the queue")

    @guarded
    async def action_add_next(self) -> None:
        entry, _ = self.selected()
        if entry and (tracks := await self.tracks_of(entry)):
            await self.tamp.player.play_next(tracks)
            self.notify(f"{len(tracks)} playing next")

    @guarded
    async def action_shuffle_play(self) -> None:
        entry, _ = self.selected()
        if entry and (tracks := await self.tracks_of(entry)):
            await self.shuffle_play(tracks, getattr(entry.value, "title", None) or entry.label or "selection")

    @guarded
    async def action_radio(self) -> None:
        entry, _ = self.selected()
        target = (
            entry.value if entry and entry.kind != "action" else (self.page.subject if self.page else None)
        )
        await self.radio_for(target)

    @guarded
    async def action_like_selected(self) -> None:
        entry, _ = self.selected()
        if entry and entry.kind == "track":
            await self.tamp.toggle_like(entry.value)

    @guarded
    async def action_info(self) -> None:
        entry, _ = self.selected()
        track = entry.value if entry and entry.kind == "track" else self.tamp.now_track
        if isinstance(track, Track):
            await self.show_info(track)

    async def show_info(self, track: Track) -> None:
        lib = self.tamp.library
        rows = await asyncio.to_thread(lib.details, track)
        data = await asyncio.to_thread(lib.art, track.thumb, 200)
        self.app.push_screen(InfoScreen(track.title, rows, art.render(data, 16, 8) if data else None))

    @guarded
    async def action_remove(self) -> None:
        entry, i = self.selected()
        if self.page and self.page.kind == "queue" and entry and entry.kind == "track":
            await self.tamp.player.remove(i)

    @guarded
    async def action_move(self, delta: int) -> None:
        entry, i = self.selected()
        if self.page and self.page.kind == "queue" and entry and entry.kind == "track":
            await self.tamp.player.move(i, i + delta)
            self.query_one("#browser", Browser).highlighted = max(0, i + delta)

    @guarded
    async def action_clear_upcoming(self) -> None:
        await self.tamp.player.clear_upcoming()

    @guarded
    async def action_shuffle_queue(self) -> None:
        await self.tamp.player.shuffle()
        self.notify("shuffled what's up next")

    def action_save_queue(self) -> None:
        tracks = list(self.tamp.player.queue.tracks)
        if not tracks:
            self.notify("the queue is empty", severity="warning")
            return

        @guarded
        async def save(_self, name: str) -> None:
            if name:
                await asyncio.to_thread(self.tamp.library.create_playlist, name, tracks)
                self.notify(f"saved playlist “{name}”")

        prompt = Prompt("Save the queue as a playlist named:", self.tamp.player.queue.source)
        self.app.push_screen(prompt, lambda name: self.run_worker(save(self, name)))

    def action_add_to_playlist(self) -> None:
        self.run_worker(self._add_to_playlist(), exclusive=True)

    @guarded
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

    def action_sleep(self) -> None:
        options = [None, 15, 30, 45, 60, 90]
        rows = [Text("  off") if m is None else Text(f"  in {m} minutes") for m in options]

        def chosen(i: int) -> None:
            if i >= 0:
                self.tamp.player.sleep_in(options[i])
                self.notify("sleep timer off" if options[i] is None else f"pausing in {options[i]} minutes")

        self.app.push_screen(Choose("Sleep timer", rows), chosen)

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

    def on_seek(self, message: Seek) -> None:
        self.run_worker(self.tamp.seek_fraction(message.fraction))

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
