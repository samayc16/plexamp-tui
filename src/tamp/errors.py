"""Error text that is safe to show. Library and network errors can quote a URL or a header,
and a Plex token must never reach the screen, a notification or a terminal."""

from __future__ import annotations

import re

# `token=…`, `X-Plex-Token: …`, `'X-Plex-Token': '…'`, `"authToken":"…"`, `X-Plex-Token%3D…`
_TOKEN = re.compile(r"(?i)([\w-]*token)(['\"]?\s*[=:]\s*['\"]?|%3D)([^\s&'\",;)}\]<>%]+)")


def redact(text: str) -> str:
    return _TOKEN.sub(r"\1\2…", text)


def describe(error: BaseException) -> str:
    """An exception as a message for the user, with any token taken out."""
    return redact(str(error) or type(error).__name__)
