"""What you can do with a row and with the queue: play, radio, the context menu, queue and
playlist edits. A mixin for MainScreen, whose pages and selection it works on."""

from __future__ import annotations

import asyncio
import functools
import random

from rich.text import Text

from tamp import art
from tamp.errors import describe
from tamp.models import Album, Artist, Playlist, Track
from tamp.ui import fmt, pages
from tamp.ui.browser import Browser, Entry, all_tracks, track_block
from tamp.ui.menus import Choose, ContextMenu, InfoScreen, MenuItem, Prompt


def guarded(fn):
    """Run an action; a failure (network, a player that refuses) becomes a message, never a crash."""

    @functools.wraps(fn)
    async def wrapper(self, *args, **kwargs):
        try:
            return await fn(self, *args, **kwargs)
        except Exception as e:
            self.notify(describe(e), severity="error")

    return wrapper


def menu_items(entry: Entry, page_kind: str, liked: bool) -> list[MenuItem]:
    if entry.kind == "track":
        if page_kind == "queue":
            items: list[MenuItem] = [
                ("play", "Play now", "enter"),
                ("move_next", "Move to play next", "N"),
                ("move_up_next_end", "Move to end of Up Next", "a"),
                ("move_up", "Move up", "K"),
                ("move_down", "Move down", "J"),
                ("remove", "Remove from queue", "d"),
            ]
        else:
            items = [
                ("play", "Play from here", "enter"),
                ("play_next", "Play next", "N"),
                ("add", "Add to Up Next", "a"),
                ("play_last", "Play last", ""),
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
            ("add", "Add to Up Next", "a"),
            ("play_last", "Play last", ""),
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
            ("add", "Add to Up Next", "a"),
            ("play_last", "Play last", ""),
        ]
    if entry.kind == "playlist":
        return [
            ("open", "Open", "enter"),
            ("play", "Play", ""),
            ("shuffle", "Shuffle", "s"),
            ("play_next", "Play next", "N"),
            ("add", "Add to Up Next", "a"),
            ("play_last", "Play last", ""),
        ]
    if entry.kind == "station":
        return [("play", "Play station", "enter")]
    return []


class Commands:
    """MainScreen's commands for rows and the queue (it supplies page, selected(), load(), …)."""

    # ── playing ──
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
                await player.jump(int(entry.label))
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

    # ── the context menu ──
    @guarded
    async def on_browser_reorder(self, message: Browser.Reorder) -> None:
        """A queue row was dragged to another row."""
        page, browser = self.page, self.query_one("#browser", Browser)
        if page is None or page.kind != "queue":
            return
        source = page.entries[int(browser.get_option_at_index(message.source).id or 0)]
        target_option = browser.get_option_at_index(message.target)
        if target_option.id is None or source.kind != "track":
            return
        target = page.entries[int(target_option.id)]
        if target.kind == "track":
            await self.tamp.player.move(int(source.label), int(target.label))

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
            self.focus_queue_position(await player.to_up_next(int(entry.label), front=True))
        elif choice in ("move_up", "move_down"):
            await self.action_move(-1 if choice == "move_up" else 1)
        elif choice == "move_up_next_end":
            self.focus_queue_position(await player.to_up_next(int(entry.label), front=False))
        elif choice == "add":
            await self.action_add()
        elif choice == "remove":
            await player.remove(int(entry.label))
        elif choice == "play_last":
            tracks = await self.tracks_of(entry)
            if await player.append(tracks):
                self.notify(f"{len(tracks)} added at the end")
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

    # ── queue and playlists ──
    @guarded
    async def action_repeat(self) -> None:
        await self.tamp.player.cycle_repeat()
        self.notify(f"repeat {self.tamp.player.queue.repeat}")

    @guarded
    async def action_add(self) -> None:
        entry, _ = self.selected()
        if self.on_queue_track(entry):  # in the queue: move it to the end of Up Next
            self.focus_queue_position(await self.tamp.player.to_up_next(int(entry.label), front=False))
        elif entry and (tracks := await self.tracks_of(entry)) and await self.tamp.player.add_up_next(tracks):
            self.notify(f"added {len(tracks)} to Up Next")

    @guarded
    async def action_add_next(self) -> None:
        entry, _ = self.selected()
        if self.on_queue_track(entry):  # in the queue: move it to play next
            self.focus_queue_position(await self.tamp.player.to_up_next(int(entry.label), front=True))
        elif entry and (tracks := await self.tracks_of(entry)) and await self.tamp.player.play_next(tracks):
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
        entry, _ = self.selected()
        if self.page and self.page.kind == "queue" and entry and entry.kind == "track":
            await self.tamp.player.remove(int(entry.label))

    @guarded
    async def action_move(self, delta: int) -> None:
        entry, _ = self.selected()
        if self.on_queue_track(entry):
            position = int(entry.label)
            target = min(max(0, position + delta), len(self.tamp.player.queue.tracks) - 1)
            if await self.tamp.player.move(position, target):
                self.focus_queue_position(target)

    @guarded
    async def action_clear_upcoming(self) -> None:
        await self.tamp.player.clear_upcoming()

    @guarded
    async def action_shuffle_queue(self) -> None:
        if await self.tamp.player.shuffle():
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
