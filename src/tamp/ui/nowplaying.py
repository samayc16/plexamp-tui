"""What's playing: the side panel, full-size lyrics, and the full-screen player (F)."""

from __future__ import annotations

import time
from typing import ClassVar

from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Static

from tamp.lrc import Lyrics
from tamp.models import Playback, Track
from tamp.queue import Queue
from tamp.ui import fmt
from tamp.ui.fmt import duration, fit

BAR_FULL, BAR_EMPTY, KNOB = "━", "─", "●"
LYRIC_LEAD_MS = 250  # land just before the line so you hear it start
LYRIC_META = "tamp_lyric_ms"  # a private tag, not Textual's @click: that would style lines as links


def lyric_seek(event: events.Click) -> int | None:
    """Where a click on a lyric line should seek to, if it landed on one."""
    meta = event.style.meta if event.style else {}
    value = meta.get(LYRIC_META)
    return int(value) if value is not None else None


class Seek(Message):
    """The progress bar was clicked: seek to this fraction of the track."""

    def __init__(self, fraction: float) -> None:
        super().__init__()
        self.fraction = fraction


class ProgressLine(Static):
    """0:42 ━━━━━━●──────── 3:51. Click or drag to seek: while dragging the knob and time
    follow the mouse, and the seek happens once, on release."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._dur = 0
        self._bar_x = 0
        self._bar_w = 0
        self._position = 0
        self._drag: float | None = None  # fraction under the mouse while dragging

    def show(self, position_ms: int, duration_ms: int) -> None:
        self._position, self._dur = position_ms, duration_ms
        self._draw()

    def _draw(self) -> None:
        dragging = self._drag is not None and self._dur
        position = int(self._drag * self._dur) if dragging else self._position
        width = max(10, self.size.width)
        left, right = duration(position), duration(self._dur)
        self._bar_x = len(left) + 1
        self._bar_w = max(4, width - len(left) - len(right) - 2)
        done = min(self._bar_w - 1, int(self._bar_w * position / self._dur)) if self._dur else 0
        self.update(
            Text.assemble(
                (left + " ", f"bold {fmt.YELLOW}" if dragging else fmt.DIM),
                (BAR_FULL * done, fmt.YELLOW if dragging else fmt.GREEN),
                (KNOB if self._dur else BAR_EMPTY, f"bold {fmt.YELLOW if dragging else fmt.GREEN}"),
                (BAR_EMPTY * (self._bar_w - done - 1), fmt.DIM),
                (" " + right, fmt.DIM),
            )
        )

    def _fraction(self, x: int) -> float:
        return min(1.0, max(0.0, (x - self._bar_x) / self._bar_w)) if self._bar_w else 0.0

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if self._dur and event.button == 1:
            self._drag = self._fraction(event.x)
            self.capture_mouse()
            self._draw()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._drag is not None:
            self._drag = self._fraction(event.x)
            self._draw()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self._drag is None:
            return
        fraction = self._drag  # where the press or the last move put it
        self._drag = None
        self.release_mouse()
        self._position = int(fraction * self._dur)  # show the new spot until playback catches up
        self._draw()
        self.post_message(Seek(fraction))


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
        sung = lyrics.timed and i == current
        if sung:
            style = Style(color=fmt.YELLOW, bold=True, underline=False)
            words = fit(words, width).rstrip()
        else:
            colour = fmt.DIM if lyrics.timed and i < current else fmt.FG
            style = Style(color=colour, underline=False)
            words = fit(words, width).rstrip()
        if lyrics.timed and line.time_ms is not None:  # click a line to jump there (see lyric_seek)
            style += Style(meta={LYRIC_META: max(0, line.time_ms - LYRIC_LEAD_MS)})
        text.append(words, style=style)
        text.append("\n" + ("\n" if big else ""))
    return text


def status_line(state: Playback, queue: Queue, device: str, sleep_at: float | None, loudness: bool) -> Text:
    icon = ("⏸ paused", fmt.YELLOW) if state.paused or not state.track else ("▶ playing", fmt.GREEN)
    repeat = {"off": "", "all": "  ⟳ all", "one": "  ⟳ one"}.get(queue.repeat, "")
    like = "  ♥" if state.track and state.track.liked else ""
    sleep = f"  zz {max(0, int(sleep_at - time.time()) // 60)}m" if sleep_at else ""
    vol_bars = "▮" * round(state.volume / 20) + "▯" * (5 - round(state.volume / 20))
    return Text.assemble(
        icon,
        (repeat, fmt.PURPLE),
        (like, fmt.PINK),
        (sleep, fmt.ORANGE),
        ("  " + vol_bars, fmt.DIM),
        ("\n→ ", fmt.DIM),
        (device, fmt.CYAN),
        ("  · leveling" if loudness else "", fmt.DIM),
    )


def track_info(state: Playback, width: int, queued: Track | None = None) -> Text:
    """What's playing; with nothing loaded, the queue's track (press space to play it)."""
    t = state.track or queued
    if t is None:
        return Text("nothing playing\n", style=fmt.DIM) + Text(
            "pick something and press enter", style=fmt.DIM
        )
    year = f" ({t.year})" if t.year else ""
    quality = " · ".join(
        x
        for x in (t.codec.upper() if t.codec else "", f"{t.bitrate_kbps} kbps" if t.bitrate_kbps else "")
        if x
    )
    return Text.assemble(
        (fit(t.title, width) + "\n", f"bold {fmt.FG}"),
        (fit(t.artist, width) + "\n", fmt.PINK),
        (fit(f"{t.album}{year}", width) + "\n", fmt.CYAN),
        (quality, fmt.DIM),
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
        self.query_one("#np-info", Static).update(track_info(self.state, width, self.queue.track))
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
            return Text.assemble(("lyrics\n", f"bold {fmt.DIM}")) + lyrics_text(
                self.lyrics, self.state.position_ms, height, width
            )
        upcoming = self.queue.upcoming[:height]
        if not upcoming:
            return Text(self.queue.source, style=fmt.DIM)
        text = Text.assemble(
            (f"up next · {self.queue.source}\n" if self.queue.source else "up next\n", f"bold {fmt.DIM}")
        )
        for t in upcoming:
            text.append(fit(f"{t.title} · {t.artist}", width) + "\n", style=fmt.FG)
        return text


class LyricsView(Static):
    """Full-size lyrics (L)."""

    def show(self, lyrics: Lyrics | None, position_ms: int, title: str) -> None:
        height = max(5, self.size.height - 2)
        width = max(10, self.size.width - 4)
        if not lyrics or not lyrics.lines:
            self.update(Text(f"\n  no lyrics for {title}" if title else "\n  nothing playing", style=fmt.DIM))
            return
        body = lyrics_text(lyrics, position_ms, height, width - 2)
        tag = "" if lyrics.timed else "  (not synced)"
        indented = Text("\n").join(Text("  ") + line for line in body.split())
        self.update(Text(f"  {title}{tag}\n\n", style=f"bold {fmt.PINK}") + indented)


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
        yield Static(Text(hint, style=fmt.DIM), id="fp-hint")

    def on_mount(self) -> None:
        self.call_after_refresh(self.app.refresh_art)  # art needs the laid-out size

    def on_resize(self) -> None:
        self.call_after_refresh(self.app.refresh_art)

    def art_size(self) -> int:
        """Rows of art that fit (each row is two pixels; width is twice the rows)."""
        left = self.query_one("#fp-left")
        # leave room below for title, artist, album, format, progress, status and where it plays
        return max(6, min(left.size.width // 2 - 1, self.size.height - 14))

    def set_art(self, art: Text | None) -> None:
        self.query_one("#fp-art", Static).update(art or Text())

    def refresh_view(
        self, state: Playback, queue: Queue, lyrics: Lyrics | None, device: str, sleep_at, loudness: bool
    ) -> None:
        width = max(10, self.query_one("#fp-left").size.width - 2)
        self.query_one("#fp-info", Static).update(track_info(state, width, queue.track))
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
            text = Text.assemble(("up next\n\n", f"bold {fmt.DIM}"))
            for t in upcoming:
                text.append(f"{t.title}", style=fmt.FG)
                text.append(f"  {t.artist}\n", style=fmt.DIM)
            pane.update(text if upcoming else Text("no lyrics for this track", style=fmt.DIM))
