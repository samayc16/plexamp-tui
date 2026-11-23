"""`tamp` command line: the player, plus small commands for tmux and scripts."""

from __future__ import annotations

import argparse
import asyncio
import sys

from tamp import __version__, config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tamp",
        description="A Plex music player for the terminal: play here, or control and follow Plexamp elsewhere.",
    )
    parser.add_argument("--version", action="version", version=f"tamp {__version__}")
    parser.add_argument("--demo", action="store_true", help="an offline demo library (no Plex needed)")
    sub = parser.add_subparsers(dest="command")
    status = sub.add_parser("status", help="what's playing, one line (for tmux)")
    status.add_argument("--json", action="store_true", help="everything as JSON")
    status.add_argument("--width", type=int, default=50)
    for name, text in (("toggle", "play / pause"), ("next", "next track"), ("prev", "previous track"), ("stop", "stop")):
        sub.add_parser(name, help=f"{text} in the running tamp")
    sub.add_parser("logout", help="forget your Plex sign-in")
    args = parser.parse_args(argv)

    if args.command == "status":
        from tamp.player import status_json, status_line

        print(status_json() if args.json else status_line(args.width))
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
    if args.command == "logout":
        config.save(config.signed_out(config.load()))
        print("signed out")
        return 0

    from tamp.app import TampApp

    app = TampApp(demo=args.demo)
    app.run()
    return app.return_code or 0
