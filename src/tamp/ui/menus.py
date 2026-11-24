"""Pop-ups: the context menu, the help screen, track info, and small pickers / prompts."""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

from rich.cells import cell_len
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.content import Content
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from tamp.ui import fmt

# (id, label, key hint)
MenuItem = tuple[str, str, str]


class ContextMenu(ModalScreen[str]):
    """Actions for one row, at the mouse (right-click) or under the row (m)."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape,q,m", "dismiss('')", "close")]

    def __init__(self, title: str, items: list[MenuItem], x: int, y: int) -> None:
        super().__init__()
        self.title_text, self.items, self.x, self.y = title, items, x, y

    def compose(self) -> ComposeResult:
        widest = max(len(label) + len(key) + 6 for _, label, key in self.items)
        width = max(cell_len(self.title_text), widest) + 4
        rows = [
            Option(
                Text.assemble((f" {label}".ljust(width - len(key) - 3), fmt.FG), (key, fmt.DIM)), id=item_id
            )
            for item_id, label, key in self.items
        ]
        with Vertical(id="menu") as menu:
            menu.styles.width = width + 2
            menu.border_title = Content(fmt.fit(self.title_text, width - 2).rstrip())  # not markup
            yield OptionList(*rows, markup=False, id="menu-list")

    def on_mount(self) -> None:
        menu = self.query_one("#menu")
        height = len(self.items) + 2
        x = min(self.x, max(0, self.size.width - int(menu.styles.width.value) - 1))
        y = min(self.y, max(0, self.size.height - height - 1))
        menu.styles.offset = (x, y)
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id or "")

    def on_click(self, event) -> None:
        if not self.query_one("#menu").region.contains(event.screen_x, event.screen_y):
            self.dismiss("")  # clicked outside


HELP: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "Play",
        [
            ("enter / double-click", "play from here · open"),
            ("space", "play / pause"),
            ("n · p", "next · previous (restart if past 3 s)"),
            ("] · [  or  → · ←", "seek 10 s forward / back"),
            ("+ · -", "volume"),
            ("R", "repeat: off → all → one"),
            ("click the progress bar", "seek there"),
            ("click a lyric line", "jump to that part of the song"),
        ],
    ),
    (
        "Browse",
        [
            ("j k · ↑ ↓ · g G", "move · top / bottom"),
            ("1 … 8", "home, artists, albums, playlists, stations, recently added, liked, queue"),
            ("/", "search as you type"),
            ("esc · h · backspace", "back"),
            ("m · right-click", "everything you can do with this row"),
            ("i", "track info"),
        ],
    ),
    (
        "Queue and playlists",
        [
            ("a · N", "add to Up Next · play next (Play last is in the m menu)"),
            ("s · S", "shuffle-play this · shuffle what's up next"),
            ("r", "radio: similar tracks, artist radio, album radio"),
            ("d · J K · C", "(queue) remove · move down / up (or shift+↓ ↑) · clear upcoming"),
            ("N · a", "(queue) move to play next · to the end of Up Next"),
            ("drag a queue row", "move it; drop inside Up Next to keep it there"),
            ("w · P", "save the queue as a playlist · add to a playlist"),
            ("f · *", "like the highlighted track · like what's playing"),
        ],
    ),
    (
        "Views and devices",
        [
            ("F", "full-screen player with big art and lyrics"),
            ("L", "lyrics, full size"),
            ("o", "play on: this computer, Plexamp, your phone…"),
            ("z · v", "sleep timer · loudness leveling"),
            ("T", "theme: built-ins, your own, or auto (colours from the album art)"),
            ("?", "this help"),
            ("q", "quit"),
        ],
    ),
    (
        "From the shell",
        [
            ("tamp status", "what's playing (put it in your tmux bar)"),
            ("tamp toggle · next · prev", "control the running tamp (bind them to keys)"),
            ("tamp --demo · tamp logout", "offline demo · forget your sign-in"),
        ],
    ),
]


class HelpScreen(ModalScreen[None]):
    """Every key, grouped, with a filter."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape,question_mark", "dismiss", "close")]

    def compose(self) -> ComposeResult:
        with Vertical(id="help"):
            yield Static(
                Text.assemble(("tamp", f"bold {fmt.PINK}"), ("  keys and mouse", fmt.DIM)), id="help-title"
            )
            yield Input(placeholder="type to filter…", id="help-filter")
            with VerticalScroll():
                yield Static(self._table(""), id="help-body")

    def _table(self, query: str) -> Table:
        q = query.lower().strip()
        table = Table(show_header=False, box=None, padding=(0, 2), expand=True)
        table.add_column(style=f"bold {fmt.YELLOW}", no_wrap=True, ratio=2)
        table.add_column(style=fmt.FG, ratio=5)
        for section, rows in HELP:
            hits = [
                (k, d) for k, d in rows if not q or q in k.lower() or q in d.lower() or q in section.lower()
            ]
            if not hits:
                continue
            table.add_row(Text(section, style=f"bold {fmt.PINK}"), "")
            for key, desc in hits:
                table.add_row(key, desc)
            table.add_row("", "")
        return table

    def on_input_changed(self, event: Input.Changed) -> None:
        self.query_one("#help-body", Static).update(self._table(event.value))


class InfoScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape,i,q", "dismiss", "close")]

    def __init__(self, title: str, rows: list[tuple[str, str]], art: Text | None = None) -> None:
        super().__init__()
        self.title_text, self.rows, self.art = title, rows, art

    def compose(self) -> ComposeResult:
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column(style=fmt.DIM, no_wrap=True)
        table.add_column(style=fmt.FG)
        for key, value in self.rows:
            table.add_row(Text(key), Text(value))  # a str cell would be read as markup
        with Vertical(id="info"):
            yield Static(Text(self.title_text, style=f"bold {fmt.PINK}"))
            if self.art:
                yield Static(self.art, id="info-art")
            yield Static(table)


class Prompt(ModalScreen[str]):
    """Ask for a line of text (a playlist name)."""

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

    def __init__(
        self,
        title: str,
        rows: list[Text],
        current: int = 0,
        on_highlight: Callable[[int], None] | None = None,
    ) -> None:
        super().__init__()
        self.title_text, self.rows, self.current = title, rows, current
        self.on_highlight = on_highlight  # e.g. preview a theme as you move

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

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if self.on_highlight is not None and self.is_mounted:
            self.on_highlight(int(event.option.id or 0))

    def action_cancel(self) -> None:
        self.dismiss(-1)
