"""Keys typed right after / go into the search box, never to the list's single-key commands,
even when they arrive before the app has caught up (a busy event loop queues them)."""

from textual import events
from textual.widgets import Input

from tamp.app import TampApp


async def test_type_ahead_after_slash_lands_in_search() -> None:
    app = TampApp(demo=True)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.5)
        for key, char in [("slash", "/"), ("a", "a"), ("r", "r")]:
            app.post_message(events.Key(key, char))  # all queued before any is handled
        await pilot.pause(0.5)
        search = app.screen.query_one("#search", Input)
        assert search.value == "ar"
        assert app.player.queue.queued == 0  # "a" didn't add anything to Up Next


async def test_a_slash_inside_search_is_just_text() -> None:
    app = TampApp(demo=True)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.5)
        await pilot.press("slash", "a", "c", "slash", "d", "c")
        await pilot.pause(0.2)
        assert app.screen.query_one("#search", Input).value == "ac/dc"
