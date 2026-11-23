"""`tamp status`: what's playing, read from the status file the app keeps. Imports nothing
heavy, because tmux runs it every few seconds."""

from __future__ import annotations

import json
import os

from tamp import config


def _alive(pid: object) -> bool:
    try:
        os.kill(int(pid), 0)  # type: ignore[arg-type]
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False
    return True


def line(max_width: int = 50) -> str:
    """'▶ Title · Artist', or '' when nothing is playing or tamp isn't running."""
    data = config.read_state(config.STATUS_FILE)
    if not data or data.get("state") == "stopped" or not _alive(data.get("pid")):
        return ""
    icon = "▶" if data.get("state") == "playing" else "⏸"
    text = f"{data.get('title', '')} · {data.get('artist', '')}"
    if len(text) > max_width:
        text = text[: max_width - 1] + "…"
    return f"{icon} {text}"


def as_json() -> str:
    return json.dumps(config.read_state(config.STATUS_FILE))
