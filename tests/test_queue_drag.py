"""Dragging a row in a draggable Browser posts one Reorder from the pressed row to the drop row."""

from textual.app import App, ComposeResult
from textual.widgets.option_list import Option

from tamp.ui.browser import Browser


class ListApp(App):
    def __init__(self) -> None:
        super().__init__()
        self.moves: list[tuple[int, int]] = []

    def compose(self) -> ComposeResult:
        browser = Browser(*[Option(f"track {n}", id=str(n)) for n in range(6)], id="list")
        browser.draggable = True
        yield browser

    def on_browser_reorder(self, message: Browser.Reorder) -> None:
        self.moves.append((message.source, message.target))


async def test_drag_a_row_down() -> None:
    app = ListApp()
    async with app.run_test(size=(40, 10)) as pilot:
        await pilot.mouse_down("#list", offset=(3, 1))
        await pilot.hover("#list", offset=(3, 2))
        await pilot.hover("#list", offset=(3, 4))
        await pilot.mouse_up("#list", offset=(3, 4))
        await pilot.pause()
    assert app.moves == [(0, 3)]  # the top border shifts rows down a line


async def test_a_click_is_not_a_drag() -> None:
    app = ListApp()
    async with app.run_test(size=(40, 10)) as pilot:
        await pilot.click("#list", offset=(3, 2))
        await pilot.pause()
    assert app.moves == []
