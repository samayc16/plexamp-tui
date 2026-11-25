"""Is there a newer tamp? One anonymous GET to GitHub's API, at most once a day.

The app asks in the background: never in --demo and never for a dev install, and `tamp status`
and the other small commands never ask at all. `"check_updates": false` in config.json or
TAMP_NO_UPDATE_CHECK=1 turns it off. update.json, in the state directory, remembers when it
last asked, what it found, and which version it has already told you about."""

from __future__ import annotations

import contextlib
import json
import os
import re
import ssl
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tamp import __version__, config

REPO = "samayc16/plexamp-tui"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"  # skips pre-releases: edge
DOWNLOAD_URL = f"https://github.com/{REPO}/releases/download"
GIT_URL = f"git+https://github.com/{REPO}"
UPDATE_FILE = config.STATE_DIR / "update.json"
USER_AGENT = f"plexamp-tui/{__version__}"
CHECK_EVERY_S = 24 * 60 * 60
TIMEOUT_S = 3
MAX_RESPONSE_BYTES = 1 << 20  # a release's JSON is a few KB
OPT_OUT_ENV = "TAMP_NO_UPDATE_CHECK"

_VERSION = re.compile(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")


def parse_version(text: object) -> tuple[int, int, int] | None:
    """'0.2.0' or 'v0.2.0' → (0, 2, 0). Anything else ('edge', '1.2', '1.2.3rc1') → None."""
    match = _VERSION.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        return None
    major, minor, patch = (int(n) for n in match.groups())
    return major, minor, patch


def is_newer(version: str, than: str = __version__) -> bool:
    new, old = parse_version(version), parse_version(than)
    return new is not None and old is not None and new > old


@dataclass(frozen=True)
class Release:
    version: str  # "0.2.0"
    wheel: bool = True  # it carries its wheel, as every release CI publishes does

    @classmethod
    def named(cls, text: object, wheel: bool = True) -> Release | None:
        """The release 'v0.2.0' or '0.2.0'; None when that isn't a release's version."""
        version = parse_version(text)
        return None if version is None else cls(".".join(map(str, version)), wheel)

    @property
    def tag(self) -> str:
        return f"v{self.version}"

    @property
    def wheel_name(self) -> str:
        return f"plexamp_tui-{self.version}-py3-none-any.whl"

    @property
    def source(self) -> str:
        """What to install: the release's wheel, which needs no git, or else its tag."""
        if self.wheel:
            return f"{DOWNLOAD_URL}/{self.tag}/{self.wheel_name}"
        return f"{GIT_URL}@{self.tag}"


def _tls() -> ssl.SSLContext:
    """certifi's roots when they're installed (requests brings them): a Python that uv
    downloaded may not find the system's."""
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def _get(url: str) -> bytes:
    """An anonymous GET. The User-Agent says plexamp-tui and its version; nothing else goes with
    it: no token, no cookie, nothing that identifies you or this computer."""
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=TIMEOUT_S, context=_tls()) as response:
        return response.read(MAX_RESPONSE_BYTES)


def latest() -> Release | None:
    """The newest numbered release on GitHub; None when there isn't one yet. Raises when the
    network or GitHub fails."""
    try:
        data = json.loads(_get(LATEST_URL))
    except HTTPError as e:
        if e.code == 404:  # the repository has no release yet
            return None
        raise
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        return None
    release = Release.named(data.get("tag_name"))
    assets = data.get("assets")
    if release is None or not isinstance(assets, list):
        return release
    names = {a.get("name") for a in assets if isinstance(a, dict)}
    return replace(release, wheel=release.wheel_name in names)


def check(now: float | None = None, force: bool = False) -> Release | None:
    """The latest release, asking GitHub at most once a day (`force` asks now). A failed attempt
    counts too, so starting tamp offline doesn't ask again at every start."""
    now = time.time() if now is None else now
    state = config.read_state(UPDATE_FILE)
    if not force and _fresh(state.get("checked_at"), now):
        return _remembered(state)
    _remember(checked_at=now)
    release = latest()
    _remember(latest=release.version if release else "", wheel=release.wheel if release else True)
    return release


def _fresh(checked_at: object, now: float) -> bool:
    if type(checked_at) not in (int, float):
        return False
    return 0 <= now - checked_at < CHECK_EVERY_S  # type: ignore[operator]  # a clock gone back: stale


def _remembered(state: dict) -> Release | None:
    wheel = state.get("wheel")
    return Release.named(state.get("latest"), wheel if isinstance(wheel, bool) else True)


def _remember(**values: object) -> None:
    """Merge into update.json, keeping whatever else is in it (a newer tamp's keys too)."""
    with contextlib.suppress(OSError):
        config.write_state(UPDATE_FILE, {**config.read_state(UPDATE_FILE), **values})


def worth_announcing(release: Release | None) -> bool:
    """Newer than this tamp and not announced before: one notice per version."""
    return (
        release is not None
        and is_newer(release.version)
        and config.read_state(UPDATE_FILE).get("notified") != release.version
    )


def announced(release: Release) -> None:
    _remember(notified=release.version)


def enabled(cfg: config.Config, env: Mapping[str, str] = os.environ) -> bool:
    """Off with "check_updates": false in config.json, or TAMP_NO_UPDATE_CHECK=1."""
    return cfg.check_updates and env.get(OPT_OUT_ENV, "").strip().lower() in ("", "0", "false", "no")
