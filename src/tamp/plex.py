"""Talking to plex.tv: sign-in (OAuth link + plex.tv/link code), Home users, servers, players.

Every function here blocks on the network; the UI calls them through asyncio.to_thread.
"""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from urllib.parse import urlencode
from xml.etree import ElementTree

import plexapi
import requests
from plexapi.myplex import MyPlexAccount, MyPlexResource
from plexapi.server import PlexServer

from tamp import PRODUCT, __version__
from tamp.models import Device

PLEX_TV = "https://plex.tv"
TIMEOUT = 10


def headers(client_id: str, token: str = "") -> dict[str, str]:
    """The X-Plex-* headers that identify tamp. The device name is the OS, not the hostname."""
    system = {"Darwin": "macOS"}.get(platform.system(), platform.system())
    h = {
        "Accept": "application/json",
        "X-Plex-Product": PRODUCT,
        "X-Plex-Version": __version__,
        "X-Plex-Client-Identifier": client_id,
        "X-Plex-Platform": system,
        "X-Plex-Device": system,
        "X-Plex-Device-Name": f"{PRODUCT} ({system})",
        "X-Plex-Provides": "controller",
    }
    if token:
        h["X-Plex-Token"] = token
    return h


def configure_plexapi(client_id: str) -> None:
    """plexapi sends its own BASE_HEADERS on every request; make them ours. Auto-reload is off:
    reading an attribute a listing didn't include would otherwise cost a request per item."""
    os.environ["PLEXAPI_PLEXAPI_AUTORELOAD"] = "false"
    plexapi.BASE_HEADERS.update({k: v for k, v in headers(client_id).items() if k.startswith("X-Plex")})
    plexapi.X_PLEX_IDENTIFIER = client_id


# ── Sign-in ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Login:
    """Two PINs polled together: a long one behind the browser link (and QR code), and a
    four-character one for typing in at plex.tv/link on another device."""

    oauth_id: int
    oauth_code: str
    link_id: int
    link_code: str
    url: str
    short_url: str  # the same sign-in, fewer bytes: a smaller QR code


def start_login(client_id: str) -> Login:
    h = headers(client_id)
    oauth = requests.post(f"{PLEX_TV}/api/v2/pins", headers=h, data={"strong": "true"}, timeout=TIMEOUT)
    link = requests.post(f"{PLEX_TV}/api/v2/pins", headers=h, timeout=TIMEOUT)
    oauth.raise_for_status()
    link.raise_for_status()
    o, k = oauth.json(), link.json()
    params = {
        "clientID": client_id,
        "code": o["code"],
        "context[device][product]": PRODUCT,
        "context[device][platform]": h["X-Plex-Platform"],
        "context[device][device]": h["X-Plex-Device"],
    }
    url = "https://app.plex.tv/auth#?" + urlencode(params)
    short = "https://app.plex.tv/auth#?" + urlencode(
        {k: params[k] for k in ("clientID", "code", "context[device][product]")}
    )
    return Login(
        oauth_id=o["id"], oauth_code=o["code"], link_id=k["id"], link_code=k["code"], url=url, short_url=short
    )


def poll_login(client_id: str, login: Login) -> str | None:
    """The account token once either PIN has been approved, else None."""
    for pin_id in (login.oauth_id, login.link_id):
        r = requests.get(f"{PLEX_TV}/api/v2/pins/{pin_id}", headers=headers(client_id), timeout=TIMEOUT)
        if r.ok and (token := r.json().get("authToken")):
            return token
    return None


# ── Home users ────────────────────────────────────────────────────────
@dataclass(frozen=True)
class HomeUser:
    id: str
    uuid: str
    title: str
    admin: bool
    protected: bool  # has a PIN
    restricted: bool  # managed user


def home_users(client_id: str, token: str) -> list[HomeUser]:
    """Everyone in your Plex Home (just you if there's no Home), admin first."""
    r = requests.get(
        f"{PLEX_TV}/api/home/users",
        headers={**headers(client_id, token), "Accept": "application/xml"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    users = [
        HomeUser(
            id=u.get("id", ""),
            uuid=u.get("uuid", ""),
            title=u.get("title") or u.get("username") or "?",
            admin=u.get("admin") == "1",
            protected=u.get("protected") == "1",
            restricted=u.get("restricted") == "1",
        )
        for u in ElementTree.fromstring(r.content).iter("User")
    ]
    return sorted(users, key=lambda u: (not u.admin, u.title.lower()))


class WrongPin(Exception):
    pass


def switch_user(client_id: str, token: str, user: HomeUser, pin: str = "") -> str:
    """A token acting as `user` (the PIN is checked by plex.tv)."""
    params = {"pin": pin} if pin else {}
    r = requests.post(
        f"{PLEX_TV}/api/home/users/{user.id}/switch",
        headers={**headers(client_id, token), "Accept": "application/xml"},
        params=params,
        timeout=TIMEOUT,
    )
    if r.status_code in (401, 403):
        raise WrongPin("wrong PIN")
    r.raise_for_status()
    return ElementTree.fromstring(r.content).get("authenticationToken") or ""


# ── Servers and players ───────────────────────────────────────────────
def account(token: str) -> MyPlexAccount:
    return MyPlexAccount(token=token, timeout=TIMEOUT)


def servers(token: str) -> list[MyPlexResource]:
    found = [r for r in account(token).resources() if "server" in (r.provides or "")]
    return sorted(found, key=lambda r: (not r.owned, not r.presence, r.name.lower()))


def connect(resource: MyPlexResource) -> PlexServer:
    return resource.connect(timeout=TIMEOUT)


def music_sections(server: PlexServer) -> list:
    return [s for s in server.library.sections() if s.type == "artist"]


# ── Remote control ────────────────────────────────────────────────────
LOCAL_PLAYER = "http://127.0.0.1:32500"  # Plexamp on this computer, when its Remote Control is on


@dataclass(frozen=True)
class PlayerInfo:
    device: Device
    urls: tuple[str, ...]  # where to reach its Companion API, most likely first
    trusted: bool  # addresses from plex.tv for your account (safe to send the token to)


def players(token: str) -> list[PlayerInfo]:
    """Plex/Plexamp players registered on your account, with their LAN addresses."""
    found = []
    for r in account(token).resources():
        provides = r.provides or ""
        if "player" not in provides or "server" in provides:
            continue
        urls = tuple(c.uri for c in sorted(r.connections, key=lambda c: (not c.local,)) if not c.relay)
        device = Device(id=r.clientIdentifier, name=r.name, product=r.product or "")
        found.append(PlayerInfo(device, urls, trusted=True))
    return found


def gdm_players(timeout: float = 1.5) -> dict[str, PlayerInfo]:
    """Players that answer Plex's LAN discovery broadcast (GDM)."""
    from plexapi.gdm import GDM

    found: dict[str, PlayerInfo] = {}
    try:
        gdm = GDM()
        gdm.scan(scan_for_clients=True, timeout=timeout)
    except Exception:
        return found
    for entry in gdm.entries:
        data, (host, _port) = entry.get("data", {}), entry.get("from", ("", 0))
        ident, port = data.get("Resource-Identifier"), data.get("Port")
        if ident and port and host:
            device = Device(id=ident, name=data.get("Name", "player"), product=data.get("Product", ""))
            found[ident] = PlayerInfo(device, (f"http://{host}:{port}",), trusted=False)
    return found


def _companion(url: str, device_id: str, client_id: str, token: str, trusted: bool):
    """A Companion client for `url` if the player there is `device_id`. The token only goes to
    addresses plex.tv gave us for your devices, or to a player that answered without it."""
    from tamp.companion import Companion

    probe = Companion(url, device_id, client_id, token if trusted else "", timeout=2)
    if probe.identity() != device_id:
        return None
    return Companion(url, device_id, client_id, token)


def list_players(client_id: str, token: str, server: PlexServer) -> list[Device]:
    """Everything you could play on or follow: Plexamp here, your account's players, players
    on the LAN, and whatever is playing on the server right now (your phone, while it plays)."""
    found: dict[str, Device] = {}
    account_players = players(token)
    known = {p.device.id for p in account_players}
    local = next(
        (p for p in account_players if _companion(LOCAL_PLAYER, p.device.id, client_id, token, True)), None
    )
    if local is not None:
        found[local.device.id] = Device(
            local.device.id, f"{local.device.name} (this computer)", local.device.product
        )
    for p in account_players:
        found.setdefault(p.device.id, p.device)
    for ident, p in gdm_players().items():
        if ident in known or ident not in found:
            found.setdefault(ident, p.device)
    for item in server.sessions():
        if item.type == "track" and item.player is not None:
            pl = item.player
            name = (
                found[pl.machineIdentifier].name
                if pl.machineIdentifier in found
                else (pl.title or pl.product or "player")
            )
            found[pl.machineIdentifier] = Device(
                pl.machineIdentifier, name, f"{pl.product or ''} · playing now".strip(" ·")
            )
    found.pop(plexapi.X_PLEX_IDENTIFIER, None)  # tamp itself
    return list(found.values())


def output_for(device_id: str, client_id: str, token: str, server: PlexServer, library):
    """A RemoteOutput for a player: controllable if any address reaches its Companion API
    (this machine, its plex.tv addresses, or the LAN broadcast), otherwise follow-only."""
    from tamp.output import RemoteOutput

    account_players = {p.device.id: p for p in players(token)}
    info = account_players.get(device_id)
    routes: list[tuple[str, bool]] = [(LOCAL_PLAYER, True)]
    if info is not None:
        routes += [(url, True) for url in info.urls]
    lan = gdm_players().get(device_id)
    if lan is not None:
        routes += [(url, False) for url in lan.urls]
    companion = None
    for url, trusted in routes:
        if (companion := _companion(url, device_id, client_id, token, trusted)) is not None:
            break
    device = info.device if info else (lan.device if lan else Device(id=device_id, name="player"))
    return RemoteOutput(device, server, library, companion)
