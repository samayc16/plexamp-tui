"""What's playing: the side panel, full-size lyrics, and the full-screen player (F)."""

from __future__ import annotations

import time
from typing import ClassVar

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Static

from tamp.lrc import Lyrics
from tamp.models import Playback
from tamp.queue import Queue
from tamp.ui.fmt import CYAN, DIM, FG, GREEN, ORANGE, PINK, PURPLE, YELLOW, duration, fit

BAR_FULL, BAR_EMPTY, KNOB = "━", "─", "●"


class Seek(Message):
    """The progress bar was clicked: seek to this fraction of the track."""

    def __init__(self, fraction: float) -> None:
        super().__init__()
        self.fraction = fraction


class ProgressLine(Static):
    """0:42 ━━━━━━●──────── 3:51, clickable."""

    def show(self, position_ms: int, duration_ms: int) -> None:
        self._dur = duration_ms
        width = max(10, self.size.width)
        left, right = duration(position_ms), duration(duration_ms)
        self._bar_x = len(left) + 1
        self._bar_w = max(4, width - len(left) - len(right) - 2)
        done = min(self._bar_w - 1, int(self._bar_w * position_ms / duration_ms)) if duration_ms else 0
        self.update(
            Text.assemble(
                (left + " ", DIM),
                (BAR_FULL * done, GREEN),
                (KNOB if duration_ms else BAR_EMPTY, f"bold {GREEN}"),
                (BAR_EMPTY * (self._bar_w - done - 1), DIM),
                (" " + right, DIM),
            )
        )

    def on_click(self, event: events.Click) -> None:
        if getattr(self, "_dur", 0) and self._bar_w:
            self.post_message(Seek(min(1.0, max(0.0, (event.x - self._bar_x) / self._bar_w))))


def lyrics_text(lyrics: Lyrics, position_ms: int, height: int, width: int, big: bool = False) -> Text:
    """The sung line bright and bold, the next ones normal, the past ones dim; centred."""
    if not lyrics.lines:
        return Text()
    current = lyrics.index_at(position_ms)
    anchor = max(current, 0)
    start = max(0, anchor - height // 3) if lyrics.timed else 0
    text = Text()
    for i, line in enumerate(lyrics.lines[start : start + height], start=start):
        words = line.text or "♪"
        if lyrics.timed and i == current:
            style = f"bold {YELLOW}"
        elif lyrics.timed and i < current:
            style = DIM
        else:
            style = FG
        text.append(fit(words, width).rstrip() + "\n" + ("\n" if big else ""), style=style)
    return text


def status_line(state: Playback, queue: Queue, device: str, sleep_at: float | None, loudness: bool) -> Text:
    icon = ("⏸ paused", YELLOW) if state.paused or not state.track else ("▶ playing", GREEN)
    repeat = {"off": "", "all": "  ⟳ all", "one": "  ⟳ one"}.get(queue.repeat, "")
    like = "  ♥" if state.track and state.track.liked else ""
    sleep = f"  zz {max(0, int(sleep_at - time.time()) // 60)}m" if sleep_at else ""
    vol_bars = "▮" * round(state.volume / 20) + "▯" * (5 - round(state.volume / 20))
    return Text.assemble(
        icon,
        (repeat, PURPLE),
        (like, PINK),
        (sleep, ORANGE),
        ("  " + vol_bars, DIM),
        ("\n→ ", DIM),
        (device, CYAN),
        ("  · leveling" if loudness else "", DIM),
    )


def track_info(state: Playback, width: int) -> Text:
    t = state.track
    if t is None:
        return Text("nothing playing\n", style=DIM) + Text("pick something and press enter", style=DIM)
    year = f" ({t.year})" if t.year else ""
    quality = " · ".join(
        x
        for x in (t.codec.upper() if t.codec else "", f"{t.bitrate_kbps} kbps" if t.bitrate_kbps else "")
        if x
    )
    return Text.assemble(
        (fit(t.title, width) + "\n", f"bold {FG}"),
        (fit(t.artist, width) + "\n", PINK),
        (fit(f"{t.album}{year}", width) + "\n", CYAN),
        (quality, DIM),
    )


class NowPlaying(Vertical):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.state = Playback()
        self.queue = Queue()
        self.lyrics: Lyrics | None = None
        self.device = "This computer"
        self.sleep_at: float | None = None
        self.loudness = True

    def compose(self) -> ComposeResult:
        yield Static("", id="np-art")
        yield Static("", id="np-info")
        yield ProgressLine("", id="np-progress")
        yield Static("", id="np-status")
        yield Static("", id="np-below")

    def set_art(self, art: Text | None) -> None:
        self.query_one("#np-art", Static).update(art or Text())

    def refresh_view(self) -> None:
        width = max(10, self.size.width - 2)
        self.query_one("#np-info", Static).update(track_info(self.state, width))
        t = self.state.track
        bar = self.query_one(ProgressLine)
        if t:
            bar.show(self.state.position_ms, self.state.duration_ms or t.duration_ms)
        else:
            bar.update("")
        self.query_one("#np-status", Static).update(
            status_line(self.state, self.queue, self.device, self.sleep_at, self.loudness)
        )
        self.query_one("#np-below", Static).update(self._below(width))

    def _below(self, width: int) -> Text:
        height = max(3, self.size.height - 22)
        if self.lyrics and self.lyrics.lines:
            return Text("lyrics\n", style=f"bold {DIM}") + lyrics_text(
                self.lyrics, self.state.position_ms, height, width
            )
        upcoming = self.queue.upcoming[:height]
        if not upcoming:
            return Text(self.queue.source, style=DIM)
        text = Text(
            f"up next · {self.queue.source}\n" if self.queue.source else "up next\n", style=f"bold {DIM}"
        )
        for t in upcoming:
            text.append(fit(f"{t.title} · {t.artist}", width) + "\n", style=FG)
        return text


class LyricsView(Static):
    """Full-size lyrics (L)."""

    def show(self, lyrics: Lyrics | None, position_ms: int, title: str) -> None:
        height = max(5, self.size.height - 2)
        width = max(10, self.size.width - 4)
        if not lyrics or not lyrics.lines:
            self.update(Text(f"\n  no lyrics for {title}" if title else "\n  nothing playing", style=DIM))
            return
        body = lyrics_text(lyrics, position_ms, height, width - 2)
        tag = "" if lyrics.timed else "  (not synced)"
        indented = Text("\n").join(Text("  ") + line for line in body.split())
        self.update(Text(f"  {title}{tag}\n\n", style=f"bold {PINK}") + indented)


class PlayerScreen(Screen):
    """Big art on the left, large synced lyrics on the right (F)."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,F,q", "app.pop_screen", "back"),
        Binding("space", "app.toggle", "play/pause"),
        Binding("n", "app.next", "next"),
        Binding("p", "app.prev", "prev"),
        Binding("right,right_square_bracket", "app.seek(10000)", "seek", show=False),
        Binding("left,left_square_bracket", "app.seek(-10000)", "seek", show=False),
        Binding("plus,equals_sign", "app.volume(5)", "vol+", show=False),
        Binding("minus", "app.volume(-5)", "vol-", show=False),
        Binding("asterisk", "app.like_playing", "like", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Horizontal(id="fp"):
            with Vertical(id="fp-left"):
                yield Static("", id="fp-art")
                yield Static("", id="fp-info")
                yield ProgressLine("", id="fp-progress")
                yield Static("", id="fp-status")
            yield Static("", id="fp-lyrics")
        hint = "  esc back · space pause · n / p next / previous · ← → seek · + - volume · * like"
        yield Static(Text(hint, style=DIM), id="fp-hint")

    def on_mount(self) -> None:
        self.call_after_refresh(self.app.refresh_art)  # art needs the laid-out size

    def on_resize(self) -> None:
        self.call_after_refresh(self.app.refresh_art)

    def art_size(self) -> int:
        """Rows of art that fit (each row is two pixels; width is twice the rows)."""
        left = self.query_one("#fp-left")
        return max(6, min(left.size.width // 2 - 1, self.size.height - 12))

    def set_art(self, art: Text | None) -> None:
        self.query_one("#fp-art", Static).update(art or Text())

    def refresh_view(
        self, state: Playback, queue: Queue, lyrics: Lyrics | None, device: str, sleep_at, loudness: bool
    ) -> None:
        width = max(10, self.query_one("#fp-left").size.width - 2)
        self.query_one("#fp-info", Static).update(track_info(state, width))
        if state.track:
            self.query_one(ProgressLine).show(state.position_ms, state.duration_ms or state.track.duration_ms)
        self.query_one("#fp-status", Static).update(status_line(state, queue, device, sleep_at, loudness))
        pane = self.query_one("#fp-lyrics", Static)
        if lyrics and lyrics.lines:
            pane.update(
                lyrics_text(
                    lyrics,
                    state.position_ms,
                    max(4, pane.size.height // 2),
                    max(10, pane.size.width - 2),
                    big=True,
                )
            )
        else:
            upcoming = queue.upcoming[: max(3, pane.size.height - 2)]
            text = Text("up next\n\n", style=f"bold {DIM}")
            for t in upcoming:
                text.append(f"{t.title}", style=FG)
                text.append(f"  {t.artist}\n", style=DIM)
            pane.update(text if upcoming else Text("no lyrics for this track", style=DIM))
