"""First-run screens: sign in with a link (or QR code, or plex.tv/link code), pick a Home user
(PIN if they have one), pick a server and music library. Each screen dismisses with its result."""

from __future__ import annotations

import asyncio
from typing import ClassVar

import qrcode
from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Center, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Input, OptionList, Static
from textual.widgets.option_list import Option

from tamp import plex
from tamp.ui import fmt


def qr_text(data: str) -> Text:
    """A scannable QR code in half-blocks: dark modules on a light quiet zone."""
    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(data)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    if len(matrix) % 2:
        matrix.append([False] * len(matrix[0]))
    dark, light = "#000000", "#ffffff"
    text = Text()
    for y in range(0, len(matrix), 2):
        for x in range(len(matrix[0])):
            top = dark if matrix[y][x] else light
            bottom = dark if matrix[y + 1][x] else light
            text.append("▀", Style(color=top, bgcolor=bottom))
        if y + 2 < len(matrix):
            text.append("\n")
    return text


class SignInScreen(Screen[str]):
    """Dismisses with the plex.tv account token."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("c", "copy", "copy link"),
        Binding("o", "open", "open in browser"),
        Binding("n", "restart", "new code"),
        Binding("q,escape", "app.quit", "quit"),
    ]

    def __init__(self, client_id: str) -> None:
        super().__init__()
        self.client_id = client_id
        self.login: plex.Login | None = None
        self._polling = False

    def compose(self) -> ComposeResult:
        with Vertical(id="signin"):
            yield Static(
                Text("tamp", style=f"bold {fmt.PINK}") + Text("  ·  sign in to Plex", style=fmt.FG),
                id="signin-title",
            )
            yield Static("", id="signin-steps")
            with Center():
                yield Static("", id="signin-qr")
            yield Static("", id="signin-status")
        yield Footer()

    async def on_mount(self) -> None:
        await self.action_restart()
        self.set_interval(2, self._poll)

    async def action_restart(self) -> None:
        self.query_one("#signin-status", Static).update(Text("contacting plex.tv…", style=fmt.DIM))
        try:
            self.login = await asyncio.to_thread(plex.start_login, self.client_id)
        except Exception as e:  # offline, DNS, plex.tv down
            self.query_one("#signin-status", Static).update(
                Text(f"couldn't reach plex.tv: {e}\npress n to try again", style=fmt.PINK)
            )
            return
        steps = Text.assemble(
            ("1  ", fmt.YELLOW),
            ("press ", fmt.FG),
            ("o", f"bold {fmt.GREEN}"),
            (" to open the sign-in page, or ", fmt.FG),
            ("c", f"bold {fmt.GREEN}"),
            (" to copy the link\n", fmt.FG),
            ("2  ", fmt.YELLOW),
            ("or scan this with your phone\n", fmt.FG),
            ("3  ", fmt.YELLOW),
            ("or on any device go to ", fmt.FG),
            ("plex.tv/link", f"bold {fmt.CYAN}"),
            (" and enter ", fmt.FG),
            (self.login.link_code, f"bold {fmt.YELLOW}"),
        )
        self.query_one("#signin-steps", Static).update(steps)
        self._show_qr()
        self.query_one("#signin-status", Static).update(Text("waiting for you to approve…", style=fmt.DIM))

    def _show_qr(self) -> None:
        if not self.login:
            return
        qr = qr_text(self.login.short_url)
        fits = len(qr.plain.splitlines()) + 12 <= self.size.height
        self.query_one("#signin-qr", Static).update(
            qr if fits else Text("(make the window taller to see a QR code)", style=fmt.DIM)
        )

    def on_resize(self) -> None:
        self._show_qr()

    def action_copy(self) -> None:
        if self.login:
            self.app.copy_to_clipboard(self.login.url)
            self.notify("Sign-in link copied")

    def action_open(self) -> None:
        if self.login:
            self.app.open_url(self.login.url)

    async def _poll(self) -> None:
        if not self.login or self._polling:
            return
        self._polling = True
        try:
            token = await asyncio.to_thread(plex.poll_login, self.client_id, self.login)
        except Exception:
            token = None  # a blip; try again on the next tick
        finally:
            self._polling = False
        if token:
            self.dismiss(token)


class PinScreen(ModalScreen[str]):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "back")]

    def __init__(self, user: str, error: str = "") -> None:
        super().__init__()
        self.user, self.error = user, error

    def compose(self) -> ComposeResult:
        with Vertical(id="pin"):
            yield Static(Text.assemble(("PIN for ", fmt.FG), (self.user, f"bold {fmt.YELLOW}")))
            yield Input(password=True, max_length=4, restrict=r"[0-9]*", placeholder="••••", id="pin-input")
            yield Static(Text(self.error, style=fmt.PINK))

    def on_input_changed(self, event: Input.Changed) -> None:
        if len(event.value) == 4:
            self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss("")


class PickScreen(Screen[int]):
    """A titled list; dismisses with the chosen index (-1 to go back)."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "back", "back"),
        Binding("q", "app.quit", "quit"),
    ]

    def __init__(self, title: str, rows: list[Text], hint: str = "") -> None:
        super().__init__()
        self.title_text, self.rows, self.hint = title, rows, hint

    def compose(self) -> ComposeResult:
        with Vertical(id="pick"):
            yield Static(Text(self.title_text, style=f"bold {fmt.PINK}"), id="pick-title")
            if self.hint:
                yield Static(Text(self.hint, style=fmt.DIM))
            yield OptionList(
                *[Option(row, id=str(i)) for i, row in enumerate(self.rows)], markup=False, id="pick-list"
            )
        yield Footer()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(int(event.option.id or 0))

    def action_back(self) -> None:
        self.dismiss(-1)


def user_rows(users: list[plex.HomeUser]) -> list[Text]:
    return [
        Text.assemble(
            ("  ● " if u.admin else "  ○ ", fmt.GREEN if u.admin else fmt.CYAN),
            (u.title, fmt.FG),
            ("  🔒" if u.protected else "", fmt.YELLOW),
            ("  managed" if u.restricted else "", fmt.DIM),
        )
        for u in users
    ]


def server_rows(servers: list) -> list[Text]:
    return [
        Text.assemble(
            ("  ● " if s.presence else "  ○ ", fmt.GREEN if s.presence else fmt.DIM),
            (s.name, fmt.FG),
            ("  yours" if s.owned else "  shared", fmt.DIM),
            ("" if s.presence else "  offline", fmt.PINK),
        )
        for s in servers
    ]


class LoadingScreen(Screen[None]):
    """Something to look at while the server answers."""

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        from textual.widgets import LoadingIndicator

        with Vertical(id="loading"):
            yield Static(Text("tamp", style=f"bold {fmt.PINK}"), id="loading-title")
            yield Static(Text(self.message, style=fmt.DIM))
            yield LoadingIndicator()
