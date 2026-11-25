"""The notice that a newer tamp is out, and U to install it. GitHub and uv are faked."""

import threading
from dataclasses import replace
from pathlib import Path

import pytest
from textual.binding import Binding

from tamp import __version__, install, update
from tamp.app import TampApp
from tamp.demo import DemoLibrary
from tamp.ui import main as main_screen
from tamp.ui import menus, nowplaying, signin
from tamp.ui.main import MainScreen
from tamp.update import Release

NOTICE = f"plexamp-tui 0.2.0 is out (you have {__version__}). Press U to update."


@pytest.fixture(autouse=True)
def private_state(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "UPDATE_FILE", tmp_path / "update.json")


def installed_as(monkeypatch, kind: str, latest: str = "0.2.0") -> list[Release]:
    """tamp is installed `kind` and GitHub's latest is `latest`; returns what uv installed."""
    monkeypatch.setattr(install, "detect", lambda: install.Install(kind, Path("/tools/plexamp-tui")))
    monkeypatch.setattr(update, "check", lambda: Release.named(latest))
    installed: list[Release] = []

    async def run_quietly(here: install.Install, release: Release) -> None:
        installed.append(release)

    monkeypatch.setattr(install, "run_quietly", run_quietly)
    return installed


def messages(app: TampApp) -> list[str]:
    return [n.message for n in app._notifications]


async def test_a_newer_release_is_announced_once_and_U_installs_it(monkeypatch) -> None:
    installed = installed_as(monkeypatch, install.UV)
    app = TampApp(demo=True)
    async with app.run_test(size=(120, 36), notifications=True) as pilot:
        await pilot.pause(0.3)
        await app.check_for_update().wait()  # the demo never checks by itself
        await app.check_for_update().wait()
        assert messages(app).count(NOTICE) == 1
        await pilot.press("U")
        await pilot.pause(0.3)
        assert installed == [Release("0.2.0")]
        assert "Updated to 0.2.0: restart tamp to use it" in messages(app)
        await pilot.press("U")
        await pilot.pause(0.1)
        assert installed == [Release("0.2.0")]  # once is enough


async def test_the_notice_comes_once_per_version_across_restarts(monkeypatch) -> None:
    installed_as(monkeypatch, install.UV)
    for _ in range(2):
        app = TampApp(demo=True)
        async with app.run_test(size=(120, 36), notifications=True) as pilot:
            await pilot.pause(0.2)
            await app.check_for_update().wait()
            shown = messages(app)
    assert NOTICE not in shown  # the second run remembered


async def test_a_failed_update_says_why_without_a_token(monkeypatch) -> None:
    installed_as(monkeypatch, install.UV)

    async def fail(here: install.Install, release: Release) -> None:
        raise install.UpdateFailed("error: Failed to download\n  cause: 404 (X-Plex-Token=abc123)")

    monkeypatch.setattr(install, "run_quietly", fail)
    app = TampApp(demo=True)
    async with app.run_test(size=(120, 36), notifications=True) as pilot:
        await pilot.pause(0.2)
        await app.check_for_update().wait()
        await pilot.press("U")
        await pilot.pause(0.3)
        error = next(n for n in app._notifications if n.severity == "error")
        assert error.message.startswith("Couldn't update to 0.2.0: error: Failed to download")
        assert "abc123" not in error.message


async def test_pipx_gets_the_command_not_a_key(monkeypatch) -> None:
    installed = installed_as(monkeypatch, install.PIPX)
    app = TampApp(demo=True)
    async with app.run_test(size=(120, 36), notifications=True) as pilot:
        await pilot.pause(0.2)
        await app.check_for_update().wait()
        await pilot.press("U")
        await pilot.pause(0.2)
        notices = [m for m in messages(app) if "0.2.0 is out" in m]
        assert notices and all("To update: pipx install --force https://" in m for m in notices)
        assert "Press U" not in notices[0]
        assert not installed


@pytest.mark.parametrize("how", ["dev", "config", "env", "up to date"])
async def test_no_notice_for_a_dev_install_an_opt_out_or_the_latest(monkeypatch, how) -> None:
    installed = installed_as(monkeypatch, install.DEV if how == "dev" else install.UV)
    if how == "env":
        monkeypatch.setenv("TAMP_NO_UPDATE_CHECK", "1")
    if how == "up to date":
        monkeypatch.setattr(update, "check", lambda: Release.named(__version__))
    else:
        monkeypatch.setattr(update, "check", lambda: pytest.fail("checked"))
    app = TampApp(demo=True)
    if how == "config":
        app.cfg = replace(app.cfg, check_updates=False)
    async with app.run_test(size=(120, 36), notifications=True) as pilot:
        await pilot.pause(0.2)
        await app.check_for_update().wait()
        assert not any("is out" in m for m in messages(app))
        await pilot.press("U")
        await pilot.pause(0.2)
        assert not installed
        if how == "dev":
            assert any("dev install" in m for m in messages(app))


async def test_a_failed_check_is_silent(monkeypatch) -> None:
    installed_as(monkeypatch, install.UV)

    def offline() -> None:
        raise OSError("network is unreachable")

    monkeypatch.setattr(update, "check", offline)
    app = TampApp(demo=True)
    async with app.run_test(size=(120, 36), notifications=True) as pilot:
        await pilot.pause(0.2)
        await app.check_for_update().wait()
        assert not any("unreachable" in m for m in messages(app))


async def test_the_demo_never_checks(monkeypatch) -> None:
    installed_as(monkeypatch, install.UV)
    monkeypatch.setattr(update, "check", lambda: pytest.fail("checked"))
    app = TampApp(demo=True)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.5)
        assert not [w for w in app.workers if w.group == "update-check"]


async def test_startup_never_waits_for_the_check(monkeypatch) -> None:
    installed_as(monkeypatch, install.UV)
    answer = threading.Event()

    def slow_check() -> Release | None:
        answer.wait(5)
        return Release("0.2.0")

    async def connect_plex(self) -> DemoLibrary:  # a real start, with an invented library
        return DemoLibrary()

    monkeypatch.setattr(update, "check", slow_check)
    monkeypatch.setattr(TampApp, "connect_plex", connect_plex)
    app = TampApp()
    async with app.run_test(size=(120, 36), notifications=True) as pilot:
        await pilot.pause(0.5)
        assert isinstance(app.screen, MainScreen) and app.player is not None
        assert NOTICE not in messages(app)  # still asking
        answer.set()
        await pilot.pause(0.3)
        assert NOTICE in messages(app)


def test_U_is_free_everywhere_else_and_in_the_help() -> None:
    screens = [main_screen.MainScreen, nowplaying.PlayerScreen, TampApp]
    screens += [getattr(m, n) for m in (menus, signin) for n in dir(m) if hasattr(getattr(m, n), "BINDINGS")]
    uses = [
        (screen.__name__, b.action)
        for screen in screens
        for b in getattr(screen, "BINDINGS", [])
        if isinstance(b, Binding) and "U" in b.key.split(",")
    ]
    assert uses == [("MainScreen", "app.install_update")]
    keys = {key for _, rows in menus.HELP for key, _ in rows}
    assert "U" in keys and any(k.startswith("tamp update") for k in keys)
