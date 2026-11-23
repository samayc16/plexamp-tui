"""The home page from last time, so tamp opens instantly while the server catches up
(a library with tens of thousands of albums can take seconds to build its hubs)."""

from __future__ import annotations

from dataclasses import asdict

from tamp import config
from tamp.models import Album, Artist, Hub, Playlist, Track

HOME_FILE = config.CACHE_DIR / "home.json"
_KINDS = {"album": Album, "track": Track, "artist": Artist, "playlist": Playlist, "station": Playlist}


def _item(kind: str, data: dict):
    cls = _KINDS[kind]
    fields = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
    if "genres" in fields:
        fields["genres"] = tuple(fields["genres"])
    return cls(**fields)


def save_home(key: str, hubs: list[Hub]) -> None:
    data = {
        "key": key,
        "hubs": [{"title": h.title, "kind": h.kind, "items": [asdict(i) for i in h.items]} for h in hubs],
    }
    try:
        config.write_state(HOME_FILE, data)
    except OSError:
        pass  # a cache is optional


def load_home(key: str) -> list[Hub] | None:
    data = config.read_state(HOME_FILE)
    if data.get("key") != key:
        return None
    try:
        return [
            Hub(h["title"], h["kind"], tuple(_item(h["kind"], i) for i in h["items"]))
            for h in data.get("hubs", [])
        ]
    except (KeyError, TypeError):
        return None  # written by an older version
