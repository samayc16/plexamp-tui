"""Clicking a timed lyric line seeks there (through Textual's @click actions)."""

from textual.app import App, ComposeResult
from textual.widgets import Static

from tamp import lrc
from tamp.ui.nowplaying import LYRIC_LEAD_MS, lyrics_text

LYRICS = lrc.parse("[00:05.00]first line\n[00:12.50]second line\n[00:20.00]third line")


class LyricsApp(App):
    def __init__(self) -> None:
        super().__init__()
        self.seeks: list[int] = []

    def compose(self) -> ComposeResult:
        yield Static(lyrics_text(LYRICS, position_ms=0, height=10, width=40), id="lyrics")

    def action_seek_to(self, position_ms: int) -> None:
        self.seeks.append(position_ms)


async def test_clicking_a_lyric_line_seeks_to_it() -> None:
    app = LyricsApp()
    async with app.run_test() as pilot:
        await pilot.click("#lyrics", offset=(2, 1))  # the second line
        await pilot.pause()
    assert app.seeks == [12_500 - LYRIC_LEAD_MS]


def test_untimed_lyrics_are_not_clickable() -> None:
    plain = lrc.parse("just words\nmore words")
    text = lyrics_text(plain, position_ms=0, height=10, width=40)
    assert not any(span.style and getattr(span.style, "meta", None) for span in text.spans)
