"""The now-playing panel: album art, track info, progress, and synced lyrics (or up next)."""

from __future__ import annotations

import time

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from tamp.lrc import Lyrics
from tamp.models import Playback
from tamp.queue import Queue
from tamp.ui.fmt import CYAN, DIM, FG, GREEN, ORANGE, PINK, PURPLE, YELLOW, duration, fit

BAR_FULL, BAR_EMPTY = "━", "─"


def progress_bar(position_ms: int, duration_ms: int, width: int) -> Text:
    left, right = duration(position_ms), duration(duration_ms)
    bar_w = max(4, width - len(left) - len(right) - 2)
    done = int(bar_w * position_ms / duration_ms) if duration_ms else 0
    done = min(done, bar_w)
    return Text.assemble((left + " ", DIM), (BAR_FULL * done, GREEN), (BAR_EMPTY * (bar_w - done), DIM), (" " + right, DIM))


def lyrics_text(lyrics: Lyrics, position_ms: int, height: int, width: int) -> Text:
    """The current line in bold yellow, centred vertically, earlier lines dimmed."""
    if not lyrics.lines:
        return Text()
    current = lyrics.index_at(position_ms)
    anchor = max(current, 0)
    start = max(0, anchor - height // 2) if lyrics.timed else 0
    text = Text()
    for i, line in enumerate(lyrics.lines[start : start + height], start=start):
        words = line.text or "♪"
        if lyrics.timed and i == current:
            style = f"bold {YELLOW}"
        elif lyrics.timed and i < current:
            style = DIM
        else:
            style = FG
        text.append(fit(words, width).rstrip() + "\n", style=style)
    return text


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
        yield Static("", id="np-progress")
        yield Static("", id="np-status")
        yield Static("", id="np-below")

    def set_art(self, art: Text | None) -> None:
        self.query_one("#np-art", Static).update(art or Text())

    def refresh_view(self) -> None:
        width = max(10, self.size.width - 2)
        t = self.state.track
        info = self.query_one("#np-info", Static)
        if t is None:
            info.update(Text("nothing playing\n", style=DIM) + Text("pick something and press enter", style=DIM))
        else:
            year = f" ({t.year})" if t.year else ""
            quality = " · ".join(x for x in (t.codec.upper() if t.codec else "", f"{t.bitrate_kbps} kbps" if t.bitrate_kbps else "") if x)
            info.update(Text.assemble(
                (fit(t.title, width) + "\n", f"bold {FG}"),
                (fit(t.artist, width) + "\n", PINK),
                (fit(f"{t.album}{year}", width) + "\n", CYAN),
                (quality, DIM),
            ))
        dur = self.state.duration_ms or (t.duration_ms if t else 0)
        self.query_one("#np-progress", Static).update(progress_bar(self.state.position_ms, dur, width) if t else Text())
        self.query_one("#np-status", Static).update(self._status())
        self.query_one("#np-below", Static).update(self._below(width))

    def _status(self) -> Text:
        s, q = self.state, self.queue
        icon = ("⏸ paused", YELLOW) if s.paused or not s.track else ("▶ playing", GREEN)
        repeat = {"off": "", "all": "  ⟳ all", "one": "  ⟳ one"}[q.repeat]
        like = "  ♥" if s.track and s.track.liked else ""
        sleep = ""
        if self.sleep_at:
            sleep = f"  zz {max(0, int(self.sleep_at - time.time()) // 60)}m"
        return Text.assemble(
            icon, (repeat, PURPLE), (like, PINK), (sleep, ORANGE), (f"  vol {s.volume}", DIM),
            ("\n→ ", DIM), (self.device, CYAN), ("  · leveling" if self.loudness else "", DIM),
        )

    def _below(self, width: int) -> Text:
        height = max(3, self.size.height - 22)
        if self.lyrics and self.lyrics.lines:
            return Text("lyrics\n", style=f"bold {DIM}") + lyrics_text(self.lyrics, self.state.position_ms, height, width)
        upcoming = self.queue.upcoming[:height]
        if not upcoming:
            return Text(self.queue.source, style=DIM)
        text = Text(f"up next · {self.queue.source}\n" if self.queue.source else "up next\n", style=f"bold {DIM}")
        for t in upcoming:
            text.append(fit(f"{t.title} · {t.artist}", width) + "\n", style=FG)
        return text


class LyricsView(Static):
    """Full-size lyrics (press L)."""

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
