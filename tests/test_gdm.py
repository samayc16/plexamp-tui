"""LAN discovery (GDM) really runs: plexapi's GDM.scan takes no timeout, and a TypeError
there used to be swallowed, so no player was ever found by the broadcast."""

import plexapi.gdm

from tamp import plex


class FakeGDM:
    def __init__(self) -> None:
        self.entries: list[dict] = []

    def scan(self, scan_for_clients: bool = False) -> None:  # plexapi's exact signature
        assert scan_for_clients
        self.entries = [
            {
                "data": {
                    "Resource-Identifier": "phone-1",
                    "Port": "32500",
                    "Name": "iPhone",
                    "Product": "Plexamp",
                },
                "from": ("192.168.1.23", 32414),
            },
            {"data": {"Name": "no identifier"}, "from": ("192.168.1.99", 32414)},
        ]


def test_gdm_finds_players(monkeypatch) -> None:
    monkeypatch.setattr(plexapi.gdm, "GDM", FakeGDM)
    found = plex.gdm_players()
    assert list(found) == ["phone-1"]
    assert found["phone-1"].urls == ("http://192.168.1.23:32500",)
    assert found["phone-1"].device.name == "iPhone"
