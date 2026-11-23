"""Unit tests for the modules with no I/O: lyrics, the queue, art, config storage."""

import io
import os
import random
import stat

import pytest
from PIL import Image

from tamp import art, config, lrc
from tamp.models import Track
from tamp.queue import Queue


def track(n: int) -> Track:
    return Track(id=str(n), title=f"t{n}", artist="a", album="b", duration_ms=1000)


# ── lyrics ────────────────────────────────────────────────────────────
def test_lrc_timed_lines_sorted_with_repeats_and_offset() -> None:
    text = "[ti:Song]\n[offset:+500]\n[00:12.50][01:00.00]Chorus\n[00:05.1]Verse\n"
    lyrics = lrc.parse(text)
    assert lyrics.timed
    assert [(line.time_ms, line.text) for line in lyrics.lines] == [
        (4600, "Verse"),
        (12000, "Chorus"),
        (59500, "Chorus"),
    ]


def test_lrc_index_at_tracks_position() -> None:
    lyrics = lrc.parse("[00:01.00]one\n[00:02.00]two\n[00:03.00]three")
    assert lyrics.index_at(500) == -1
    assert lyrics.index_at(1000) == 0
    assert lyrics.index_at(2999) == 1
    assert lyrics.index_at(60_000) == 2


def test_plain_lyrics_are_untimed() -> None:
    lyrics = lrc.parse("first line\nsecond line\n\n")
    assert not lyrics.timed
    assert [line.text for line in lyrics.lines] == ["first line", "second line"]
    assert lyrics.index_at(10_000) == -1


# ── queue ─────────────────────────────────────────────────────────────
def test_queue_play_next_and_append() -> None:
    q = Queue().play([track(1), track(2)], start=0)
    q = q.play_next([track(9)]).append([track(5)])
    assert [t.id for t in q.tracks] == ["1", "9", "2", "5"]
    assert q.track and q.track.id == "1"


def test_queue_is_immutable() -> None:
    q = Queue().play([track(1)])
    q2 = q.append([track(2)])
    assert len(q.tracks) == 1 and len(q2.tracks) == 2


def test_queue_remove_keeps_current_track() -> None:
    q = Queue().play([track(1), track(2), track(3)], start=2)
    assert q.remove(0).track == track(3)
    assert q.remove(2).current == 1  # removing the playing track falls back to the previous slot


@pytest.mark.parametrize(
    ("index", "to", "current"),
    [(0, 2, 0), (2, 0, 2), (1, 2, 2), (2, 1, 2)],
)
def test_queue_move_follows_current(index: int, to: int, current: int) -> None:
    q = Queue().play([track(1), track(2), track(3)], start=1)
    moved = q.move(index, to)
    assert moved.track == track(2)
    assert moved.current == current


def test_queue_repeat_modes() -> None:
    q = Queue().play([track(1), track(2)], start=1)
    assert q.advanced().current == 1  # off: stays at the end
    assert q.cycle_repeat().advanced().current == 0  # all: wraps
    assert q.cycle_repeat().cycle_repeat().advanced().current == 1  # one: repeats


def test_queue_shuffle_keeps_history_and_current() -> None:
    q = Queue().play([track(n) for n in range(10)], start=3)
    s = q.shuffled(random.Random(1))
    assert s.tracks[:4] == q.tracks[:4]
    assert sorted(t.id for t in s.tracks) == sorted(t.id for t in q.tracks)


# ── art ───────────────────────────────────────────────────────────────
def test_art_renders_half_blocks() -> None:
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), (255, 0, 0)).save(buf, "PNG")
    text = art.render(buf.getvalue(), width=10, height=5)
    assert text.plain.count("▀") == 50
    assert art.render(b"not an image", 10, 5).plain == ""


# ── config ────────────────────────────────────────────────────────────
def test_config_round_trip_is_private(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "tamp")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "tamp" / "config.json")
    first = config.load()
    assert first.client_id and not first.signed_in
    config.save(config.Config(client_id=first.client_id, user_token="secret", user_title="me"))
    mode = stat.S_IMODE(os.stat(config.CONFIG_FILE).st_mode)
    assert mode == 0o600
    loaded = config.load()
    assert loaded.signed_in and loaded.client_id == first.client_id
    assert not config.signed_out(loaded).signed_in
