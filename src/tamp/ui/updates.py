"""The app's side of updates: one notice per new release, and U to install it with uv."""

from __future__ import annotations

import asyncio
import shlex
from typing import TYPE_CHECKING

from textual import work

from tamp import __version__, install, update
from tamp.errors import describe

if TYPE_CHECKING:
    from tamp.config import Config
    from tamp.install import Install
    from tamp.update import Release

NOTICE_TIMEOUT_S = 15


def notice(here: Install, release: Release) -> str:
    news = f"plexamp-tui {release.version} is out (you have {__version__})."
    if here.kind == install.UV:
        return f"{news} Press U to update."
    return f"{news} To update: {shlex.join(install.command(here, release))}"


class Updates:
    """Mixed into TampApp, which calls check_for_update once the main screen is up."""

    cfg: Config
    _update_install: Install | None = None
    _update_release: Release | None = None  # newer than this tamp: what U installs
    _update_step = ""  # "installing", then "installed"

    @work(group="update-check", exclusive=True)
    async def check_for_update(self) -> None:
        """In the background, so startup never waits. Failures stay quiet until tomorrow's try."""
        here = self._update_install = await asyncio.to_thread(install.detect)
        if here.kind == install.DEV or not update.enabled(self.cfg):
            return
        try:
            release = await asyncio.to_thread(update.check)
        except Exception:
            return  # offline, rate-limited, GitHub down
        if release is None or not update.is_newer(release.version):
            return
        self._update_release = release
        if update.worth_announcing(release):
            update.announced(release)
            self.notify(notice(here, release), timeout=NOTICE_TIMEOUT_S)

    def action_install_update(self) -> None:
        here, release = self._update_install, self._update_release
        if here is not None and here.kind == install.DEV:
            self.notify("this is a dev install: update it with git")
        elif release is None or here is None:
            self.notify("no update waiting (`tamp update` checks now)")
        elif here.kind != install.UV:
            self.notify(notice(here, release), timeout=NOTICE_TIMEOUT_S)
        elif self._update_step == "installing":
            self.notify(f"already updating to {release.version}…")
        elif self._update_step == "installed":
            self.notify(f"Updated to {release.version}: restart tamp to use it")
        else:
            self.install_update(here, release)

    @work(group="update-install", exclusive=True)
    async def install_update(self, here: Install, release: Release) -> None:
        self._update_step = "installing"
        self.notify(f"updating to {release.version}…")
        try:
            await install.run_quietly(here, release)
        except Exception as e:
            self._update_step = ""
            self.notify(
                f"Couldn't update to {release.version}: {describe(e)}",
                severity="error",
                timeout=NOTICE_TIMEOUT_S,
            )
            return
        self._update_step = "installed"
        self.notify(f"Updated to {release.version}: restart tamp to use it", timeout=NOTICE_TIMEOUT_S)
