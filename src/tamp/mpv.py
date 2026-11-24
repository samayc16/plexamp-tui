"""One background mpv process for audio, driven over its JSON IPC socket (asyncio).

Request headers (the Plex token) are set over the socket, never on the command line, so they
don't show up in `ps`."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import itertools
import json
import os
import re
import shutil
import signal
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

OBSERVED = ("time-pos", "duration", "pause", "playlist-pos", "volume", "idle-active", "eof-reached")


class MpvError(RuntimeError):
    pass


class AlreadyRunning(MpvError):
    """Another tamp's mpv holds the socket."""


def parse_version(text: object) -> tuple[int, int] | None:
    """(major, minor) from mpv's `mpv-version` ("mpv 0.37.0", "mpv v0.38.0-458-g1234abcd")."""
    found = re.search(r"(\d+)\.(\d+)", text) if isinstance(text, str) else None
    return (int(found[1]), int(found[2])) if found else None


def short_socket(path: Path) -> Path:
    """Unix socket paths are capped at ~104 bytes (macOS): fall back to a short temp path."""
    if len(str(path)) < 100:
        return path
    digest = hashlib.sha1(str(path).encode()).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"tamp-{os.getuid()}" / f"{digest}.sock"


def private_dir(path: Path) -> None:
    """Create `path` as 0700 and refuse one that someone else owns or others can write to
    (a shared /tmp must not let another user plant our socket or pidfile)."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise MpvError(f"{path} isn't a directory owned by you")
    if info.st_mode & 0o077:
        os.chmod(path, 0o700)


class Mpv:
    def __init__(
        self, socket: Path, on_event: Callable[[dict], None], extra_args: tuple[str, ...] = ()
    ) -> None:
        self.socket = short_socket(socket)
        self.extra_args = extra_args
        self.on_event = on_event
        self._process: asyncio.subprocess.Process | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._ids = itertools.count(1)
        self.version: tuple[int, int] | None = None  # known once started
        self._owner = False  # the socket and pidfile are ours to remove (we started this mpv)

    @property
    def running(self) -> bool:
        return self._writer is not None

    @staticmethod
    def available() -> bool:
        return shutil.which("mpv") is not None

    async def start(self) -> None:
        if self.running:
            return
        if not self.available():
            raise MpvError("mpv isn't installed (brew install mpv / apt install mpv)")
        private_dir(self.socket.parent)
        if await self._socket_alive() and not self._reap_orphan():
            raise AlreadyRunning("another tamp is already running (its mpv socket is in use)")
        with contextlib.suppress(FileNotFoundError):
            self.socket.unlink()
        self._owner = True
        self._process = await asyncio.create_subprocess_exec(
            "mpv",
            "--idle=yes",
            "--no-video",
            "--no-terminal",
            "--no-config",  # a personal mpv.conf (shaders, scripts) shouldn't change music playback
            "--audio-display=no",
            "--gapless-audio=weak",
            "--prefetch-playlist=yes",
            "--cache=yes",
            "--demuxer-max-bytes=64MiB",
            "--volume-max=100",
            *self.extra_args,
            f"--input-ipc-server={self.socket}",  # last, so nothing above can move the socket
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        for _ in range(100):  # up to 5 s for the socket to appear
            if (code := self._process.returncode) is not None:  # it quit: a bad option, most likely
                hint = ': check "mpv_args" in config.json' if self.extra_args else ""
                await self.quit()
                raise MpvError(f"mpv exited at start (code {code}){hint}")
            if self.socket.exists():
                try:
                    reader, self._writer = await asyncio.open_unix_connection(str(self.socket))
                    break
                except OSError:
                    pass
            await asyncio.sleep(0.05)
        else:
            await self.quit()
            raise MpvError("mpv started but its IPC socket never appeared")
        self._pidfile.write_text(json.dumps({"mpv": self._process.pid, "owner": os.getpid()}))
        self._reader_task = asyncio.create_task(self._read(reader))
        for n, name in enumerate(OBSERVED, start=1):
            await self.command("observe_property", n, name)
        with contextlib.suppress(MpvError):
            self.version = parse_version(await self.get("mpv-version"))

    @property
    def _pidfile(self) -> Path:
        return self.socket.with_suffix(".pid")

    def _reap_orphan(self) -> bool:
        """An mpv whose tamp died (closed terminal, killed pane): stop it and take over."""
        try:
            if os.lstat(self._pidfile).st_uid != os.getuid():
                return False  # not ours: never act on it
            pids = json.loads(self._pidfile.read_text())
            owner, mpv = int(pids["owner"]), int(pids["mpv"])
        except (OSError, ValueError, KeyError, TypeError):
            return False
        if _alive(owner):
            return False
        with contextlib.suppress(OSError):
            os.kill(mpv, signal.SIGTERM)
        with contextlib.suppress(FileNotFoundError):
            self.socket.unlink()
        return True

    async def _socket_alive(self) -> bool:
        if not self.socket.exists():
            return False
        try:
            _, writer = await asyncio.open_unix_connection(str(self.socket))
        except OSError:
            return False
        writer.close()
        return True

    async def _read(self, reader: asyncio.StreamReader) -> None:
        while line := await reader.readline():
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "request_id" in msg and (fut := self._pending.pop(msg["request_id"], None)):
                if not fut.done():
                    fut.set_result(msg)
            elif "event" in msg:
                try:
                    self.on_event(msg)
                except Exception:  # a bug in a handler must not stop us reading mpv
                    pass
        # mpv went away (crash, killed): forget it so start() launches a new one
        self._writer = None
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(MpvError("mpv exited"))
        self._pending.clear()
        with contextlib.suppress(Exception):
            self.on_event({"event": "shutdown"})

    async def command(self, *args: Any) -> Any:
        if self._writer is None:
            raise MpvError("mpv isn't running")
        rid = next(self._ids)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        self._writer.write((json.dumps({"command": list(args), "request_id": rid}) + "\n").encode())
        await self._writer.drain()
        reply = await asyncio.wait_for(fut, timeout=10)
        if reply.get("error") not in ("success", None):
            raise MpvError(f"{args[0]}: {reply['error']}")
        return reply.get("data")

    async def set(self, prop: str, value: Any) -> None:
        await self.command("set_property", prop, value)

    async def get(self, prop: str) -> Any:
        return await self.command("get_property", prop)

    async def quit(self) -> None:
        if self._writer is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.command("quit"), timeout=2)
            self._writer.close()
            self._writer = None
        if self._process and self._process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._process.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._process.wait(), timeout=3)
        if self._reader_task:
            self._reader_task.cancel()
        if self._owner:  # never another tamp's: it would lose its socket and its pidfile
            self._owner = False
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.socket)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self._pidfile)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def send(socket: Path, *args: Any) -> Any:
    """One command to a running tamp's mpv (used by `tamp toggle|next|prev` from tmux keys)."""
    reader, writer = await asyncio.open_unix_connection(str(socket))
    try:
        writer.write((json.dumps({"command": list(args), "request_id": 1}) + "\n").encode())
        await writer.drain()
        while line := await asyncio.wait_for(reader.readline(), timeout=3):
            msg = json.loads(line)
            if msg.get("request_id") == 1:
                return msg.get("data")
    finally:
        writer.close()
