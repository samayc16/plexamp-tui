"""`tamp` command line: the player, plus small commands for tmux and scripts."""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import TYPE_CHECKING

from tamp import __version__, config

if TYPE_CHECKING:
    from tamp.install import Install
    from tamp.update import Release


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tamp",
        description="A Plex music player for the terminal: play here, or control and follow Plexamp elsewhere.",
    )
    parser.add_argument("--version", action="version", version=f"tamp {__version__}")
    parser.add_argument("--demo", action="store_true", help="an offline demo library (no Plex needed)")
    parser.add_argument(
        "--private", action="store_true", help="report nothing to Plex: no history, no play counts"
    )
    parser.add_argument(
        "--hide-names", action="store_true", help="keep account, server and device names off screen"
    )
    sub = parser.add_subparsers(dest="command")
    status = sub.add_parser("status", help="what's playing, one line (for tmux)")
    status.add_argument("--json", action="store_true", help="everything as JSON")
    status.add_argument("--width", type=int, default=50)
    status.add_argument("--tmux", action="store_true", help="pad for a tmux status bar (nothing when idle)")
    for name, text in (
        ("toggle", "play / pause"),
        ("next", "next track"),
        ("prev", "previous track"),
        ("stop", "stop"),
    ):
        sub.add_parser(name, help=f"{text} in the running tamp")
    sub.add_parser("logout", help="forget your Plex sign-in")
    update = sub.add_parser("update", help="check for a newer release now, and install it")
    which = update.add_mutually_exclusive_group()
    which.add_argument(
        "--check",
        action="store_true",
        help="only say: exit 0 when up to date, 10 when a newer release is out",
    )
    which.add_argument("--to", metavar="vX.Y.Z", help="install this release, newer or older (to roll back)")
    args = parser.parse_args(argv)

    if args.command == "status":
        from tamp import status

        text = status.as_json() if args.json else status.line(args.width)
        if args.tmux and text:  # tmux reads #[…] and #(…) in status-right: a title mustn't
            text = text.replace("#", "##") + "  "
        print(text)
        return 0
    if args.command in ("toggle", "next", "prev", "stop"):
        from tamp.mpv import send

        try:
            from tamp.mpv import short_socket

            asyncio.run(send(short_socket(config.MPV_SOCKET), "script-message", "tamp", args.command))
        except OSError:
            print("tamp isn't running", file=sys.stderr)
            return 1
        return 0
    if args.command == "update":
        return _update(check_only=args.check, to=args.to)
    if args.command == "logout":
        config.save(config.signed_out(config.load()))
        print("signed out")
        return 0

    from tamp.app import TampApp

    app = TampApp(demo=args.demo, private=args.private, hide_names=args.hide_names)
    app.run()
    return app.return_code or 0


UPDATE_AVAILABLE = 10  # `tamp update --check`: a newer release is out


def _update(check_only: bool, to: str | None) -> int:
    from tamp import install, update
    from tamp.errors import describe

    here = install.detect()
    if here.kind == install.DEV:
        print(f"tamp {__version__} is a dev install (editable, or run from a checkout): update it with git")
        return 0
    if to is not None:
        release = update.Release.named(to)
        if release is None:
            print(f"tamp update --to: {to!r} isn't a release; give one like v0.2.0", file=sys.stderr)
            return 2
        return _install(here, release, done="Installed")
    try:
        release = update.check(force=True)
    except Exception as e:  # offline, rate-limited, GitHub down
        print(f"couldn't check for a newer tamp: {describe(e)}", file=sys.stderr)
        return 1
    if release is None or not update.is_newer(release.version):
        latest = f"the latest release is {release.version}" if release else "there's no release yet"
        print(f"tamp {__version__} is up to date ({latest})")
        return 0
    print(f"plexamp-tui {release.version} is out (you have {__version__}).")
    if here.kind != install.UV:
        print(f"tamp was installed with {here.kind}; to update, run:\n  {_command(here, release)}")
        return UPDATE_AVAILABLE
    if check_only:
        print("`tamp update` installs it.")
        return UPDATE_AVAILABLE
    return _install(here, release, done="Updated to")


def _install(here: Install, release: Release, done: str) -> int:
    from tamp import install

    if here.kind != install.UV:
        print(
            f"tamp was installed with {here.kind}; to install {release.tag}, run:\n  {_command(here, release)}"
        )
        return 1
    try:
        code = install.run(here, release)
    except install.UpdateFailed as e:
        print(f"couldn't install {release.tag}: {e}", file=sys.stderr)
        return 1
    if code != 0:
        print(f"couldn't install {release.tag} (uv said why, above)", file=sys.stderr)
        return 1
    print(f"{done} {release.version}: restart tamp if it's running.")
    return 0


def _command(here: Install, release: Release) -> str:
    import shlex

    from tamp import install

    return shlex.join(install.command(here, release))
