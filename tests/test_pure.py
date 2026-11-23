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


# ── up next (Plexamp's queue model) ──
def ids(q: Queue) -> list[str]:
    return [t.id for t in q.tracks]


def album_playing() -> Queue:
    return Queue().play([track(n) for n in range(1, 5)], start=0)  # playing 1, rest 2 3 4


def test_add_up_next_goes_before_the_rest_in_order() -> None:
    q = album_playing().add_up_next([track(8)]).add_up_next([track(9)])
    assert ids(q) == ["1", "8", "9", "2", "3", "4"]
    assert [t.id for t in q.up_next] == ["8", "9"] and [t.id for t in q.rest] == ["2", "3", "4"]


def test_play_next_goes_to_the_front_of_up_next() -> None:
    q = album_playing().add_up_next([track(8)]).play_next([track(9)])
    assert [t.id for t in q.up_next] == ["9", "8"]


def test_play_last_goes_to_the_very_end() -> None:
    q = album_playing().add_up_next([track(8)]).append([track(9)])
    assert ids(q)[-1] == "9" and [t.id for t in q.up_next] == ["8"]


def test_advancing_uses_up_up_next_first() -> None:
    q = album_playing().add_up_next([track(8), track(9)])
    q = q.advanced()
    assert q.track.id == "8" and [t.id for t in q.up_next] == ["9"]
    q = q.advanced().advanced()
    assert q.track.id == "2" and q.queued == 0


def test_jumping_back_leaves_up_next_as_part_of_the_rest() -> None:
    q = album_playing().advanced().add_up_next([track(8)])  # playing 2, up next 8
    assert q.jump(0).queued == 0
    assert q.jump(q.current + 1).track.id == "8" and q.jump(q.current + 1).queued == 0


def test_dragging_into_and_out_of_up_next() -> None:
    q = album_playing().add_up_next([track(8)])  # 1 | 8 | 2 3 4
    into = q.move(3, 2)  # drag "3" to just after "8"
    assert ids(into) == ["1", "8", "3", "2", "4"] and [t.id for t in into.up_next] == ["8", "3"]
    out = q.move(1, 4)  # drag "8" down among the rest
    assert out.queued == 0 and ids(out) == ["1", "2", "3", "4", "8"]
    within = into.move(2, 1)  # reorder inside up next
    assert [t.id for t in within.up_next] == ["3", "8"]


def test_removing_from_up_next_shrinks_it() -> None:
    q = album_playing().add_up_next([track(8), track(9)])
    assert q.remove(1).queued == 1 and q.remove(4).queued == 2


def test_shuffle_keeps_up_next_in_order() -> None:
    q = Queue().play([track(n) for n in range(10)], start=0).add_up_next([track(50), track(51)])
    s = q.shuffled(random.Random(3))
    assert [t.id for t in s.up_next] == ["50", "51"]
    assert sorted(t.id for t in s.rest) == sorted(t.id for t in q.rest)


def test_moving_a_queued_track_into_up_next() -> None:
    q = album_playing().add_up_next([track(8)])  # 1 | 8 | 2 3 4
    front = q.to_up_next(4, front=True)  # "4" to play next
    assert [t.id for t in front.up_next] == ["4", "8"] and [t.id for t in front.rest] == ["2", "3"]
    end = q.to_up_next(3, front=False)  # "3" to the end of up next
    assert [t.id for t in end.up_next] == ["8", "3"] and [t.id for t in end.rest] == ["2", "4"]
    again = q.to_up_next(1, front=False)  # already the only up-next track: stays put
    assert ids(again) == ids(q) and again.queued == 1


def test_moving_a_played_track_back_into_up_next() -> None:
    q = album_playing().advanced().advanced()  # 1 2 | playing 3 | 4
    moved = q.to_up_next(0, front=True)
    assert moved.track.id == "3" and [t.id for t in moved.up_next] == ["1"] and moved.history[0].id == "2"


def test_mpv_args_keep_only_options_and_never_the_socket() -> None:
    from tamp.config import mpv_args

    assert mpv_args(["--ao=null", "rm -rf", "--input-ipc-server=/tmp/x", 3]) == ("--ao=null",)
    assert mpv_args("--ao=null") == ()


def test_track_rows_are_exactly_the_width_whatever_the_mix() -> None:
    from tamp.ui import fmt

    albums = ["Promo Only Modern Rock Radio Fall 2006", "Sing-A-Longs And Lullabies For The Film", "X"]
    for title in ("Up", "Upside Down", "A Title That Goes On For Quite A While Indeed"):
        for artist in ("Sia", "Jack Johnson", "Red Hot Chili Peppers & Friends"):
            tracks = [
                Track(id=str(i), title=title, artist=artist, album=a, duration_ms=1000)
                for i, a in enumerate(albums)
            ]
            for width in range(44, 120, 7):  # below ~43 even the minimum columns cannot fit
                for numbers in ([], [1, 2, 12]):
                    cols = fmt.columns(tracks, width, show_album=True, numbers=numbers)
                    for t in tracks:
                        row = fmt.track_row(t, cols, number=12 if numbers else None)
                        assert len(row.plain) == width, (title, artist, width, numbers, cols)
