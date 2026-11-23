"""The Textual app: startup (sign-in → Home user → server → library), then the main screen.
Wires the Player to the UI, loads art and lyrics for whatever is playing, and switches outputs."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace

from rich.text import Text
from textual import work
from textual.app import App

from tamp import art, config, plex
from tamp.demo import DemoLibrary
from tamp.library import Library, PlexLibrary
from tamp.lrc import Lyrics
from tamp.models import THIS_DEVICE, Playback, Track
from tamp.mpv import Mpv, MpvError
from tamp.output import LocalOutput, RemoteOutput, TaskSet
from tamp.player import Player
from tamp.queue import Queue
from tamp.ui import fmt
from tamp.ui.main import MainScreen, guarded
from tamp.ui.menus import Choose
from tamp.ui.nowplaying import NowPlaying, PlayerScreen
from tamp.ui.signin import PickScreen, PinScreen, SignInScreen, server_rows, user_rows

CLI_COMMANDS = {"toggle", "next", "prev", "stop"}


class TampApp(App):
    CSS_PATH = "ui/tamp.tcss"
    TITLE = "tamp"
    ENABLE_COMMAND_PALETTE = False

    def __init__(self, demo: bool = False) -> None:
        super().__init__()
        self.demo = demo
        self.cfg = config.load()
        self.library: Library | None = None
        self.player: Player | None = None
        self.local: LocalOutput | None = None
        self.server = None
        self.current_lyrics: Lyrics | None = None
        self.loudness = self.cfg.loudness_leveling
        self.device_name = THIS_DEVICE.name
        self._dirty = False
        self._shown_track: str | None = None
        self._lyrics_cache: dict[str, Lyrics | None] = {}
        self._art: bytes | None = None
        self._tasks = TaskSet(lambda m: self.notify(m, severity="warning"))

    def on_mount(self) -> None:
        import signal

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGHUP, signal.SIGTERM):  # closed terminal / killed pane: clean up mpv
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, self.exit)
        self.startup()

    # ── startup ──
    @work(exclusive=True)
    async def startup(self) -> None:
        try:
            self.library = DemoLibrary() if self.demo else await self.connect_plex()
        except Exception as e:  # network, auth, no music library
            self.exit(1, message=f"tamp: {e}")
            return
        self.player = Player(
            self.library, self.on_player_update, on_error=lambda m: self.notify(m, severity="warning")
        )
        if not self.demo:
            self.player.restore_queue()
        socket = config.MPV_SOCKET.with_name("mpv-demo.sock") if self.demo else config.MPV_SOCKET
        mpv = Mpv(socket, self.on_mpv_event)
        self.local = LocalOutput(
            self.library, mpv, self.player.changed, self.player.advanced, self.player.finished, self.loudness
        )
        await self.push_screen(MainScreen())
        try:
            await self.player.use(self.local)
        except MpvError as e:
            self.notify(
                f"{e}: you can still browse and control other players (o)", severity="warning", timeout=10
            )
        self._dirty = True
        self.set_interval(0.25, self._tick)
        if not self.demo and self.cfg.device_id != THIS_DEVICE.id:
            await self.switch_output(self.cfg.device_id, quiet=True)

    async def connect_plex(self) -> PlexLibrary:
        cfg = self.cfg
        plex.configure_plexapi(cfg.client_id)
        if not cfg.signed_in:
            token = await self.push_screen_wait(SignInScreen(cfg.client_id))
            cfg = replace(cfg, account_token=token)
            users = await asyncio.to_thread(plex.home_users, cfg.client_id, token)
            user = users[0] if users else None
            if len(users) > 1:
                choice = await self.push_screen_wait(PickScreen("Who's listening?", user_rows(users)))
                user = users[max(0, choice)]
            user_token, error = token, ""
            while user and not user.admin:
                pin = ""
                if user.protected:
                    pin = await self.push_screen_wait(PinScreen(user.title, error))
                    if not pin:  # escape: pick someone else
                        choice = await self.push_screen_wait(PickScreen("Who's listening?", user_rows(users)))
                        user, error = users[max(0, choice)], ""
                        continue
                try:
                    user_token = await asyncio.to_thread(plex.switch_user, cfg.client_id, token, user, pin)
                    break
                except plex.WrongPin:
                    error = "wrong PIN, try again"
            cfg = replace(cfg, user_token=user_token, user_title=user.title if user else "")
            config.save(cfg)

        resources = await asyncio.to_thread(plex.servers, cfg.user_token)
        if not resources:
            raise RuntimeError("no Plex Media Server on this account")
        resource = next((r for r in resources if r.clientIdentifier == cfg.server_id), None)
        if resource is None:
            choice = 0
            if len(resources) > 1:
                choice = max(
                    0, await self.push_screen_wait(PickScreen("Which server?", server_rows(resources)))
                )
            resource = resources[choice]
        self.notify(f"connecting to {resource.name}…", timeout=3)
        self.server = await asyncio.to_thread(plex.connect, resource)
        sections = await asyncio.to_thread(plex.music_sections, self.server)
        if not sections:
            raise RuntimeError(f"{resource.name} has no music library")
        section_id = cfg.section_id
        if not any(str(s.key) == section_id for s in sections):
            choice = 0
            if len(sections) > 1:
                rows = [Text(f"  {s.title}") for s in sections]
                choice = max(0, await self.push_screen_wait(PickScreen("Which music library?", rows)))
            section_id = str(sections[choice].key)
        self.cfg = replace(
            cfg, server_id=resource.clientIdentifier, server_name=resource.name, section_id=section_id
        )
        config.save(self.cfg)
        return await asyncio.to_thread(PlexLibrary, self.server, section_id)

    # ── events ──
    def on_mpv_event(self, msg: dict) -> None:
        if msg.get("event") == "shutdown" and self.player and self.player.output is self.local:
            self.notify("mpv stopped unexpectedly: press space to start it again", severity="warning")
            return
        if msg.get("event") == "client-message":
            args = msg.get("args") or []
            if len(args) == 2 and args[0] == "tamp" and args[1] in CLI_COMMANDS and self.player:
                command = {
                    "toggle": self.player.toggle,
                    "next": self.player.next,
                    "prev": self.player.prev,
                    "stop": self.player.stop,
                }[args[1]]
                self._tasks.spawn(command())
            return
        if self.local and self.player and self.player.output is self.local:
            self.local.handle_event(msg)

    @property
    def now_track(self) -> Track | None:
        """What's playing on the current output (a remote player included), else the queue's."""
        if self.player is None:
            return None
        return self.player.state.track or self.player.queue.track

    def on_player_update(self, state: Playback, queue: Queue) -> None:
        self._dirty = True
        track = state.track or queue.track
        if track and track.id != self._shown_track:
            self._shown_track = track.id
            self.load_track_extras(track)
            if isinstance(self.screen, MainScreen):
                self.screen.rerender()  # move the ▶ marker
        main = self._main()
        if main is not None:
            main.refresh_queue_page()

    def _main(self) -> MainScreen | None:
        return next((s for s in self.screen_stack if isinstance(s, MainScreen)), None)

    @work(group="extras", exclusive=True)
    async def load_track_extras(self, track: Track) -> None:
        """Art and lyrics for the playing track; stale results are dropped."""
        lib = self.library
        if lib is None:
            return
        if track.id not in self._lyrics_cache:
            try:
                self._lyrics_cache[track.id] = await asyncio.to_thread(lib.lyrics, track)
            except Exception:
                self._lyrics_cache[track.id] = None
        if self._shown_track == track.id:
            self.current_lyrics = self._lyrics_cache[track.id]
        image = None
        with contextlib.suppress(Exception):
            image = await asyncio.to_thread(lib.art, track.thumb, 600)
        if self._shown_track == track.id:
            self._art = image
            self.refresh_art()
        self._dirty = True

    def refresh_art(self) -> None:
        """Draw the playing track's art at the size each view has room for."""
        main = self._main()
        if main is not None:
            np = main.query_one(NowPlaying)
            rows = max(5, min((np.size.width - 4) // 2, np.size.height - 20))  # leave room for lyrics
            np.set_art(art.render(self._art, rows * 2, rows) if self._art else None)
        if isinstance(self.screen, PlayerScreen):
            rows = self.screen.art_size()
            self.screen.set_art(art.render(self._art, rows * 2, rows) if self._art else None)

    def _tick(self) -> None:
        if self.player is None or not (self._dirty or not self.player.state.paused):
            return
        self._dirty = False
        main = self._main()
        if main is not None:
            main.tick()
        if isinstance(self.screen, PlayerScreen):
            p = self.player
            self.screen.refresh_view(
                p.state, p.queue, self.current_lyrics, self.device_name, p.sleep_at, self.loudness
            )

    # ── transport (shared by the main screen and the full-screen player) ──
    @guarded
    async def action_toggle(self) -> None:
        if self.player and self.local and self.player.output is self.local and not self.local.mpv.running:
            await self.player.use(self.local)  # mpv died: bring it back
        if self.player:
            await self.player.toggle()

    @guarded
    async def action_next(self) -> None:
        if self.player:
            await self.player.next()

    @guarded
    async def action_prev(self) -> None:
        if self.player:
            await self.player.prev()

    @guarded
    async def action_seek(self, delta: int) -> None:
        if self.player:
            await self.player.seek_by(delta)

    @guarded
    async def action_volume(self, delta: int) -> None:
        if self.player:
            await self.player.volume_by(delta)

    async def seek_fraction(self, fraction: float) -> None:
        if self.player and (track := self.player.state.track):
            await self.player.seek(int(fraction * (self.player.state.duration_ms or track.duration_ms)))

    @guarded
    async def action_like_playing(self) -> None:
        if (track := self.now_track) is not None:
            await self.toggle_like(track)

    @guarded
    async def action_loudness(self) -> None:
        await self.toggle_loudness()

    def action_choose_output(self) -> None:
        self.run_worker(self.choose_output(), exclusive=True, group="output")

    # ── commands used by the screens ──
    async def toggle_like(self, track: Track) -> None:
        if self.library is None or self.player is None:
            return
        rating = None if track.liked else 10.0
        updated = await asyncio.to_thread(self.library.rate, track, rating)
        self.player.replace_track(updated)
        main = self._main()
        if main is not None:
            main.replace_track(updated)
        self.notify(("♥ liked " if updated.liked else "unliked ") + updated.title)

    async def toggle_loudness(self) -> None:
        self.loudness = not self.loudness
        if self.local:
            self.local.loudness = self.loudness
        self.cfg = replace(self.cfg, loudness_leveling=self.loudness)
        config.save(self.cfg)
        self.notify(f"loudness leveling {'on' if self.loudness else 'off'} (from the next track)")

    @guarded
    async def choose_output(self) -> None:
        """Pick where music plays: this computer, or a Plex/Plexamp player (your phone…)."""
        options: list[tuple[str, str, str]] = [(THIS_DEVICE.id, THIS_DEVICE.name, "tamp's own player (mpv)")]
        if self.demo:  # what the picker looks like with a real account; just for show
            options += [
                ("demo-phone", "iPhone", "Plexamp · playing now"),
                ("demo-mac", "MacBook", "Plexamp · this network"),
                ("demo-pi", "Living room", "Plexamp headless"),
            ]
        if not self.demo and self.server is not None:
            self.notify("looking for players…", timeout=2)
            try:
                for device in await asyncio.to_thread(
                    plex.list_players, self.cfg.client_id, self.cfg.user_token, self.server
                ):
                    options.append((device.id, device.name, device.product))
            except Exception as e:
                self.notify(f"couldn't list players: {e}", severity="warning")
        current = next((i for i, o in enumerate(options) if o[0] == self.cfg.device_id), 0)
        rows = [
            Text.assemble(
                ("  ● " if i == current else "  ○ ", fmt.GREEN), (name, fmt.FG), (f"  {desc}", fmt.DIM)
            )
            for i, (_, name, desc) in enumerate(options)
        ]
        choice = await self.push_screen_wait(Choose("Play on", rows, current))
        if choice >= 0:
            await self.switch_output(options[choice][0])

    async def switch_output(self, device_id: str, quiet: bool = False) -> None:
        if self.player is None:
            return
        if device_id.startswith("demo-"):
            self.notify("in the demo, other players are just for show: sign in to control yours")
            return
        resume = bool(self.player.state.track and not self.player.state.paused and not self.player.remote)
        if device_id == THIS_DEVICE.id or self.server is None:
            if self.local:
                await self.player.use(self.local, resume=resume)
            self.device_name = THIS_DEVICE.name
        else:
            try:
                output = await asyncio.to_thread(
                    plex.output_for,
                    device_id,
                    self.cfg.client_id,
                    self.cfg.user_token,
                    self.server,
                    self.library,
                )
            except Exception as e:
                if not quiet:
                    self.notify(f"can't reach that player: {e}", severity="error")
                return
            output.on_change = self.player.changed
            await self.player.use(output, resume=resume and output.controllable)
            self.device_name = output.device.name
            if not output.controllable and not quiet:
                self.notify(
                    "following this player: now playing and lyrics (it isn't reachable for remote control from here)",
                    timeout=8,
                )
        self.cfg = replace(self.cfg, device_id=device_id)
        config.save(self.cfg)
        self._dirty = True

    async def on_unmount(self) -> None:
        if self.player:
            self.player.clear_status()
            if self.player.output:
                with contextlib.suppress(Exception):
                    await self.player.output.close()
        if self.local and self.player and self.player.output is not self.local:
            with contextlib.suppress(Exception):
                await self.local.close()


__all__ = ["RemoteOutput", "TampApp"]
