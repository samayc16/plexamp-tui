"""The list you browse: pages of entries, and an OptionList that understands the mouse
(click selects, double-click plays, right-click opens the context menu)."""

from __future__ import annotations

from dataclasses import dataclass, field

from textual import events
from textual.message import Message
from textual.widgets import OptionList

from tamp.models import Track


@dataclass(frozen=True)
class Entry:
    kind: str  # artist album track playlist station action heading
    value: object = None
    label: str = ""


@dataclass(frozen=True)
class Header:
    """The banner above an album, artist or playlist page."""

    title: str
    subtitle: str = ""
    meta: str = ""
    thumb: str | None = None


@dataclass
class Page:
    title: str
    kind: str = ""
    entries: list[Entry] = field(default_factory=list)
    source: str = ""  # shown as "up next · <source>"
    header: Header | None = None
    highlighted: int = 0
    subject: object = None  # the album / artist / playlist the page is about


def track_block(page: Page, index: int) -> tuple[list[Track], int]:
    """The run of track rows around entry `index` and where it sits in that run: "play from
    here" plays the section you clicked in, and duplicates resolve to the row, not the value."""
    entries = page.entries
    start = index
    while start > 0 and entries[start - 1].kind == "track":
        start -= 1
    end = index
    while end + 1 < len(entries) and entries[end + 1].kind == "track":
        end += 1
    return [e.value for e in entries[start : end + 1]], index - start  # type: ignore[misc]


def all_tracks(page: Page) -> list[Track]:
    return [e.value for e in page.entries if e.kind == "track"]  # type: ignore[misc]


class Browser(OptionList):
    """An OptionList where a single click only selects; double-click activates; right-click asks
    for the context menu."""

    class MenuRequested(Message):
        def __init__(self, index: int, x: int, y: int) -> None:
            super().__init__()
            self.index, self.x, self.y = index, x, y

    def _option_at(self, event: events.MouseEvent) -> int | None:
        index = event.style.meta.get("option")
        if index is None or self._options[index].disabled:
            return None
        return index

    def _on_click(self, event: events.Click) -> None:
        index = self._option_at(event)
        if index is None:
            return
        event.stop()
        event.prevent_default()  # the base OptionList would play on a single click
        if event.button == 3:
            return  # handled on press (_on_mouse_down)
        self.highlighted = index
        if event.chain >= 2:
            self.action_select()

    def _on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button == 3 and (index := self._option_at(event)) is not None:
            event.stop()
            event.prevent_default()
            self.highlighted = index
            self.post_message(self.MenuRequested(index, event.screen_x, event.screen_y))
