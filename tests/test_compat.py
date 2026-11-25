"""Old and new versions of tamp share the same files. Each loads what the other wrote, ignores
what it doesn't know, and keeps another version's settings when it saves its own."""

import json
import os
from dataclasses import replace

import pytest

from tamp import cache, config, status
from tamp.demo import DemoLibrary
from tamp.models import Album, Hub
from tamp.player import Player

TRACK = {"id": "1", "title": "t", "artist": "a", "album": "b", "duration_ms": 1000}


@pytest.fixture(autouse=True)
def private_files(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config" / "config.json")
    monkeypatch.setattr(config, "STATUS_FILE", tmp_path / "status.json")
    monkeypatch.setattr(config, "QUEUE_FILE", tmp_path / "queue.json")
    monkeypatch.setattr(cache, "HOME_FILE", tmp_path / "home.json")


def write(path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def saved_config() -> dict:
    return json.loads(config.CONFIG_FILE.read_text())


# ── config.json ──
def test_an_older_config_loads_and_keeps_what_newer_versions_dropped() -> None:
    # an early config: no theme, mpv_args or leveling yet, and "volume", a setting since removed
    write(config.CONFIG_FILE, {"client_id": "abc", "user_token": "t", "server_id": "s", "volume": 80})
    loaded = config.load()
    assert (loaded.client_id, loaded.user_token, loaded.server_id) == ("abc", "t", "s")
    assert loaded.theme == config.Config().theme and loaded.loudness_leveling is True
    config.save(loaded)
    assert saved_config()["volume"] == 80  # still there for that older version


def test_a_newer_versions_settings_survive_a_save() -> None:
    future = {"crossfade_s": 5, "eq": {"bass": 3}, "theme_v2": ["nord", "auto"]}
    write(config.CONFIG_FILE, {"client_id": "abc", "user_token": "t", "theme": "nord", **future})
    loaded = config.load()
    assert loaded.theme == "nord"
    config.save(replace(loaded, theme="dracula"))
    data = saved_config()
    assert data["theme"] == "dracula" and {k: data[k] for k in future} == future
    assert config.load().theme == "dracula"


def test_another_tamps_new_setting_written_meanwhile_survives_this_ones_save() -> None:
    write(config.CONFIG_FILE, {"client_id": "abc"})
    running = config.load()
    write(config.CONFIG_FILE, {"client_id": "abc", "crossfade_s": 5})  # a newer tamp, at the same time
    config.save(replace(running, theme="nord"))
    assert saved_config()["crossfade_s"] == 5


def test_signing_out_leaves_no_token_behind_not_even_a_newer_versions() -> None:
    write(
        config.CONFIG_FILE,
        {"client_id": "abc", "user_token": "t", "refresh_token": "r", "crossfade_s": 5},
    )
    config.save(config.signed_out(config.load()))
    data = saved_config()
    assert "refresh_token" not in data and data["user_token"] == ""
    assert data["crossfade_s"] == 5


def test_saving_over_a_damaged_config_writes_a_good_one() -> None:
    config.CONFIG_DIR.mkdir(parents=True)
    config.CONFIG_FILE.write_text("{half a file")
    config.save(config.load())
    assert saved_config()["client_id"]


# ── queue.json ──
def restored() -> Player:
    player = Player(DemoLibrary(), lambda s, q: None)
    player.restore_queue()
    return player


def test_an_older_queue_file_loads() -> None:
    # only what every version has written: no repeat, Up Next or source yet
    write(config.QUEUE_FILE, {"current": 1, "tracks": [TRACK, {**TRACK, "id": "2"}]})
    q = restored().queue
    assert [t.id for t in q.tracks] == ["1", "2"]
    assert (q.current, q.queued, q.repeat, q.source) == (1, 0, "off", "")


def test_a_newer_queue_file_loads_without_what_it_added() -> None:
    write(
        config.QUEUE_FILE,
        {
            "format": 3,
            "current": 0,
            "repeat": "all",
            "shuffle_order": [1, 0],
            "tracks": [{**TRACK, "disc": 2, "loudness": {"lufs": -9}}, {**TRACK, "id": "2"}],
        },
    )
    q = restored().queue
    assert [t.id for t in q.tracks] == ["1", "2"] and q.repeat == "all"


# ── home.json ──
def test_a_newer_home_cache_loads_the_rows_this_version_knows() -> None:
    write(
        cache.HOME_FILE,
        {
            "key": "k",
            "format": 2,
            "hubs": [
                {"title": "Mixes for you", "kind": "mix", "items": [{"id": "m", "seeds": [1]}]},
                {"title": "Albums", "kind": "album", "items": [{**TRACK, "label": "Warp"}]},
            ],
        },
    )
    assert cache.load_home("k") == [Hub("Albums", "album", (Album("1", "t", "a"),))]


def test_a_damaged_home_cache_is_ignored() -> None:
    write(cache.HOME_FILE, {"key": "k", "hubs": [{"title": "Albums", "kind": "album", "items": [{}]}]})
    assert cache.load_home("k") is None


# ── the status file ──
def test_tamp_status_reads_a_newer_status_file() -> None:
    write(
        config.STATUS_FILE,
        {"pid": os.getpid(), "state": "playing", "title": "t", "artist": "a", "rating": 10, "queue": [1]},
    )
    assert status.line() == "▶ t · a"
    assert json.loads(status.as_json())["rating"] == 10


def test_tamp_status_reads_an_older_status_file() -> None:
    # before position, duration and device were written
    write(config.STATUS_FILE, {"pid": os.getpid(), "state": "paused", "title": "t", "artist": "a"})
    assert status.line() == "⏸ t · a"
