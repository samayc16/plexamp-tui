"""Titles and error text are full of square brackets ("Song [Live]"). They show as typed:
never eaten as markup, never a crash ("a[/i]"), never an action ("[@click=…]")."""

import html
import re

from textual.app import App

from tamp.app import TampApp
from tamp.ui.menus import ContextMenu, InfoScreen

TRICKY = ["Song [Live]", "a[/i]", "[@click=app.quit]x[/]", "Track [/]"]


def screen_text(app: App) -> str:
    svg = app.export_screenshot()
    return html.unescape(re.sub(r"<[^>]+>", "", svg)).replace("\xa0", " ")


async def test_notifications_show_titles_and_errors_as_typed() -> None:
    app = TampApp(demo=True)
    async with app.run_test(size=(140, 40), notifications=True) as pilot:
        await pilot.pause(0.3)
        for text in TRICKY:
            app.screen.notify(f"♥ liked {text}")  # from a screen, as the main screen does
        app.notify("player: refused (X-Plex-Token=abc123)", severity="error")
        await pilot.pause(0.3)
        messages = [n.message for n in app._notifications]
        assert all(not n.markup for n in app._notifications)
        assert [f"♥ liked {t}" for t in TRICKY] == messages[:4]
        assert "abc123" not in messages[4]
        assert "liked Song [Live]" in screen_text(app)
        assert app.is_running


class MenuApp(App):
    CSS = "#menu { border: round white; }"  # tamp's own stylesheet needs its theme variables

    def __init__(self, screen) -> None:
        super().__init__()
        self.start = screen

    def on_mount(self) -> None:
        self.push_screen(self.start)


async def test_a_context_menu_title_is_shown_as_typed() -> None:
    app = MenuApp(ContextMenu("Song [Live] a[/i]", [("play", "Play now", "enter")], 2, 2))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0.2)
        assert "Song [Live] a[/i]" in screen_text(app)


async def test_track_info_is_shown_as_typed() -> None:
    rows = [("Title", "Song [Live]"), ("Album", "a[/i]"), ("Artist", "[@click=app.quit]x[/]")]
    app = MenuApp(InfoScreen("Song [Live]", rows))
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0.2)
        text = screen_text(app)
        assert "Song [Live]" in text and "a[/i]" in text and "[@click=app.quit]x[/]" in text
