"""Clicking a timed lyric line seeks there (a private style tag the app handles, not a link)."""

from textual.app import App, ComposeResult
from textual.widgets import Static

from tamp import lrc
from tamp.ui.nowplaying import LYRIC_LEAD_MS, lyric_seek, lyrics_text

LYRICS = lrc.parse("[00:05.00]first line\n[00:12.50]second line\n[00:20.00]third line")


class LyricsApp(App):
    def __init__(self) -> None:
        super().__init__()
        self.seeks: list[int] = []

    def compose(self) -> ComposeResult:
        yield Static(lyrics_text(LYRICS, position_ms=0, height=10, width=40), id="lyrics")

    def on_click(self, event) -> None:  # the same hook TampApp uses
        if (position := lyric_seek(event)) is not None:
            self.seeks.append(position)


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


def test_lyrics_do_not_render_as_links() -> None:
    text = lyrics_text(LYRICS, position_ms=13_000, height=10, width=40)
    metas = [span.style.meta for span in text.spans if span.style and span.style.meta]
    assert metas and all("@click" not in m for m in metas)  # Textual would underline those
