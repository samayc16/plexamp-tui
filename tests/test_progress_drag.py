"""Dragging the progress bar seeks once, on release, to where the mouse let go."""

from textual.app import App, ComposeResult

from tamp.ui.nowplaying import ProgressLine, Seek


class BarApp(App):
    def __init__(self) -> None:
        super().__init__()
        self.seeks: list[float] = []

    def compose(self) -> ComposeResult:
        yield ProgressLine("", id="bar")

    def on_mount(self) -> None:
        self.query_one(ProgressLine).show(position_ms=0, duration_ms=200_000)

    def on_seek(self, message: Seek) -> None:
        self.seeks.append(message.fraction)


async def test_drag_seeks_once_where_released() -> None:
    app = BarApp()
    async with app.run_test(size=(60, 3)) as pilot:
        bar = app.query_one(ProgressLine)
        start, end = bar._bar_x + 2, bar._bar_x + bar._bar_w // 2
        await pilot.mouse_down("#bar", offset=(start, 0))
        await pilot.hover("#bar", offset=(start + 5, 0))
        await pilot.hover("#bar", offset=(end, 0))
        assert app.seeks == []  # nothing seeks while dragging
        assert "1:" in bar.render().plain or bar._drag is not None  # the preview follows the mouse
        await pilot.mouse_up("#bar", offset=(end, 0))
        await pilot.pause()
    assert len(app.seeks) == 1
    assert abs(app.seeks[0] - (end - bar._bar_x) / bar._bar_w) < 0.02


async def test_a_plain_click_still_seeks() -> None:
    app = BarApp()
    async with app.run_test(size=(60, 3)) as pilot:
        bar = app.query_one(ProgressLine)
        x = bar._bar_x + bar._bar_w // 4
        await pilot.click("#bar", offset=(x, 0))
        await pilot.pause()
    assert len(app.seeks) == 1 and abs(app.seeks[0] - 0.25) < 0.05
