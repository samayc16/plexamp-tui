"""Where tamp keeps things. The Plex token lives in the config file, readable only by you;
it never goes in the dotfiles repo."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default) / "tamp"


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config")
STATE_DIR = _xdg("XDG_STATE_HOME", ".local/state")
CACHE_DIR = _xdg("XDG_CACHE_HOME", ".cache")
CONFIG_FILE = CONFIG_DIR / "config.json"
STATUS_FILE = STATE_DIR / "status.json"  # what's playing, for `tamp status` / tmux
QUEUE_FILE = STATE_DIR / "queue.json"

MPV_SOCKET = STATE_DIR / "mpv.sock"  # shortened by tamp.mpv if the path is too long
ART_DIR = CACHE_DIR / "art"


@dataclass(frozen=True)
class Config:
    client_id: str = ""  # identifies this install to Plex (X-Plex-Client-Identifier)
    account_token: str = ""  # the plex.tv account you signed in with
    user_token: str = ""  # the Home user you picked (same as account_token for the owner)
    user_title: str = ""
    server_id: str = ""  # machineIdentifier of the chosen Plex Media Server
    server_name: str = ""
    section_id: str = ""  # the music library
    device_id: str = "local"  # where to play: "local" or a player's machineIdentifier
    loudness_leveling: bool = True
    # extra mpv options for local playback, e.g. ["--audio-device=coreaudio/BuiltInSpeakerDevice"]
    # (`mpv --audio-device=help` lists devices); options only, and never the control socket
    mpv_args: tuple[str, ...] = ()
    theme: str = "monokai-pro"  # a built-in, one of yours (~/.config/tamp/themes/*.toml), or "auto"

    @property
    def signed_in(self) -> bool:
        return bool(self.user_token)


def load() -> Config:
    config = Config(**_valid(read_state(CONFIG_FILE)))
    return config if config.client_id else replace(config, client_id=str(uuid.uuid4()))


def _valid(data: dict) -> dict:
    """The fields that have the right type. Anything else (a hand edit gone wrong, "no" for
    false) falls back to its default instead of crashing tamp or passing through."""
    defaults = Config()
    fields: dict = {}
    for name in Config.__dataclass_fields__:
        if name not in data:
            continue
        value = data[name]
        if name == "mpv_args":
            fields[name] = mpv_args(value)
        elif type(value) is type(getattr(defaults, name)):  # exact: true isn't 1, 1 isn't true
            fields[name] = value
    return fields


def mpv_args(value: object) -> tuple[str, ...]:
    """Keep the mpv options that are safe to pass: `--name[=value]`, not tamp's own IPC socket."""
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(
        a
        for a in value
        if isinstance(a, str) and a.startswith("--") and not a.startswith("--input-ipc-server")
    )


def save(config: Config) -> None:
    """Write atomically with 0600 permissions: the file holds your Plex token. Keys this
    version doesn't know (a newer tamp's settings, or one an older tamp still reads) are kept."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
    data = {**asdict(config), **_others(signed_in=config.signed_in)}
    tmp = CONFIG_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, CONFIG_FILE)


_SECRET_WORDS = ("token", "secret", "password")


def _others(signed_in: bool) -> dict:
    """What config.json holds besides this version's settings, read afresh so that another tamp
    running meanwhile keeps its changes. Signed out, nothing that looks like a credential stays:
    a newer tamp may keep one under a name this version can't clear."""
    return {
        key: value
        for key, value in read_state(CONFIG_FILE).items()
        if key not in Config.__dataclass_fields__
        and (signed_in or not any(word in key.lower() for word in _SECRET_WORDS))
    }


def signed_out(config: Config) -> Config:
    return replace(
        config, account_token="", user_token="", user_title="", server_id="", server_name="", section_id=""
    )


def write_state(path: Path, data: dict) -> None:
    """Private (0600): your queue and what you're playing are nobody else's business."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def read_state(path: Path) -> dict:
    """A state file's contents; {} when it's missing, unreadable or not a JSON object."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
