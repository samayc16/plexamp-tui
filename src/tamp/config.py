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
    volume: int = 100
    loudness_leveling: bool = True

    @property
    def signed_in(self) -> bool:
        return bool(self.user_token)


def load() -> Config:
    try:
        data = json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        data = {}
    known = {k: v for k, v in data.items() if k in Config.__dataclass_fields__}
    config = Config(**known)
    return config if config.client_id else replace(config, client_id=str(uuid.uuid4()))


def save(config: Config) -> None:
    """Write atomically with 0600 permissions: the file holds your Plex token."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(asdict(config), f, indent=2)
    os.replace(tmp, CONFIG_FILE)


def signed_out(config: Config) -> Config:
    return replace(config, account_token="", user_token="", user_title="", server_id="", server_name="", section_id="")


def write_state(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def read_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
