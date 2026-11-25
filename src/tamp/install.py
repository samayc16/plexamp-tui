"""How this tamp was installed, and how to put another release in its place."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import pkgutil
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

import tamp
from tamp.errors import redact
from tamp.update import Release

UV, PIPX, PIP, DEV = "uv", "pipx", "pip", "dev"
DISTRIBUTION = "plexamp-tui"
INSTALL_TIMEOUT_S = 5 * 60
_ERROR_LINES = 4
_ERROR_CHARS = 400


class UpdateFailed(Exception):
    """Installing a release didn't work; the message says why, in uv's words."""


@dataclass(frozen=True)
class Install:
    kind: str  # UV, PIPX, PIP, or DEV: editable or run from a checkout, never updated from here
    prefix: Path  # the environment tamp runs in


def detect() -> Install:
    prefix = Path(sys.prefix)
    return Install(kind_of(prefix, _direct_url(), Path(tamp.__file__)), prefix)


def kind_of(prefix: Path, direct_url: dict, module: Path) -> str:
    """`direct_url` is PEP 610's record of where the install came from; `module` is tamp's
    __init__.py, which only an editable install or a checkout keeps outside site-packages."""
    dir_info = direct_url.get("dir_info")
    editable = isinstance(dir_info, dict) and dir_info.get("editable") is True
    if editable or not {"site-packages", "dist-packages"} & set(module.parts):
        return DEV
    if (prefix / "uv-receipt.toml").is_file():
        return UV
    if (prefix / "pipx_metadata.json").is_file():
        return PIPX
    return PIP


def _direct_url() -> dict:
    try:
        text = metadata.distribution(DISTRIBUTION).read_text("direct_url.json")
        data = json.loads(text) if text else {}
    except (metadata.PackageNotFoundError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def command(install: Install, release: Release) -> list[str]:
    """What installs `release` in place of this tamp. uv's form also installs fresh, does
    nothing new when it's already there, and rolls back to an older release."""
    if install.kind == PIPX:
        return ["pipx", "install", "--force", release.source]
    if install.kind == PIP:
        return [sys.executable, "-m", "pip", "install", "--upgrade", release.source]
    return ["uv", "tool", "install", "--upgrade", release.source]


def uv_executable() -> str | None:
    return shutil.which("uv")


def _uv(install: Install, release: Release) -> tuple[list[str], dict[str, str]]:
    """uv's command line, and an environment that points it at the tool directory this tamp
    runs from, even when this shell doesn't set UV_TOOL_DIR."""
    uv = uv_executable()
    if uv is None:
        raise UpdateFailed(f"uv isn't on your PATH: run {shlex.join(command(install, release))}")
    env = {**os.environ, "UV_TOOL_DIR": str(install.prefix.parent)}
    return [uv, *command(install, release)[1:]], env


def run(install: Install, release: Release) -> int:
    """For `tamp update`: uv's progress and errors go straight to the terminal."""
    argv, env = _uv(install, release)
    return subprocess.run(argv, env=env, check=False).returncode


async def run_quietly(install: Install, release: Release) -> None:
    """For the app, whose screen uv mustn't write on. Raises UpdateFailed with the end of what
    uv said."""
    argv, env = _uv(install, release)
    preload()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), INSTALL_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise UpdateFailed(f"uv was still going after {INSTALL_TIMEOUT_S // 60} minutes") from None
    if proc.returncode != 0:
        raise UpdateFailed(
            _last_words(output.decode(errors="replace")) or f"uv exited with {proc.returncode}"
        )


def _last_words(output: str) -> str:
    lines = [line.rstrip() for line in output.splitlines() if line.strip()]
    return redact("\n".join(lines[-_ERROR_LINES:]))[-_ERROR_CHARS:]


def preload() -> None:
    """Import every tamp module now. uv replaces the files under the running app, and a module
    first imported after that would be the new version's, among the old ones."""
    for module in pkgutil.walk_packages(tamp.__path__, "tamp."):
        if module.name != "tamp.__main__":  # running it would start a second tamp
            importlib.import_module(module.name)
