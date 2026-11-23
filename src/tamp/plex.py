"""Talking to plex.tv: sign-in (OAuth link + plex.tv/link code), Home users, servers, players.

Every function here blocks on the network; the UI calls them through asyncio.to_thread.
"""

from __future__ import annotations

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
    """plexapi sends its own BASE_HEADERS on every request; make them ours."""
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


def players(token: str) -> list[tuple[Device, MyPlexResource]]:
    """Plex/Plexamp players on this account that can be remote-controlled."""
    found = []
    for r in account(token).resources():
        provides = r.provides or ""
        if "player" in provides and "server" not in provides:
            found.append((Device(id=r.clientIdentifier, name=r.name, product=r.product or ""), r))
    return sorted(found, key=lambda pair: pair[0].name.lower())


def music_sections(server: PlexServer) -> list:
    return [s for s in server.library.sections() if s.type == "artist"]


# ── Remote control ────────────────────────────────────────────────────
LOCAL_PLAYER = "http://127.0.0.1:32500"  # Plexamp on this computer, when remote control is on


def local_player(token: str):
    """Plexamp (or another Plex player) on this machine that accepts remote control, or None."""
    from plexapi.client import PlexClient

    try:
        return PlexClient(baseurl=LOCAL_PLAYER, token=token, timeout=2)
    except Exception:
        return None


def list_players(token: str, server: PlexServer) -> list[Device]:
    """Every player we could show or control: Plexamp here, players on the account, and
    whatever is playing on the server right now (your phone shows up while it plays)."""
    found: dict[str, Device] = {}
    if (client := local_player(token)) is not None:
        found[client.machineIdentifier] = Device(
            id=client.machineIdentifier, name=f"{client.title} (this computer)", product=client.product or ""
        )
    for device, _ in players(token):
        found.setdefault(device.id, device)
    for item in server.sessions():
        if item.type != "track" or item.player is None:
            continue
        p = item.player
        found[p.machineIdentifier] = Device(
            id=p.machineIdentifier,
            name=p.title or p.product or "player",
            product=f"{p.product or ''} · playing now".strip(" ·"),
        )
    found.pop(plexapi.X_PLEX_IDENTIFIER, None)  # tamp itself
    return list(found.values())


def output_for(device_id: str, token: str, server: PlexServer, library):
    """A RemoteOutput for a player: with a client if any route to it accepts commands
    (directly on this machine, via its plex.tv connections, or proxied by the server),
    otherwise follow-only."""
    from tamp.output import RemoteOutput

    devices = {d.id: d for d in list_players(token, server)}
    device = devices.get(device_id, Device(id=device_id, name="player"))
    client = None
    local = local_player(token)
    if local is not None and local.machineIdentifier == device_id:
        client = local
    if client is None:
        resource = next((r for _, r in players(token) if r.clientIdentifier == device_id), None)
        if resource is not None:
            try:
                client = resource.connect(timeout=3)
            except Exception:
                client = None
    if client is None:
        try:
            client = next(c for c in server.clients() if c.machineIdentifier == device_id)
            client.proxyThroughServer(True, server)
        except Exception:
            client = None
    return RemoteOutput(device, server, library, client)
