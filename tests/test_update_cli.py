"""`tamp update`: check now, install a newer release with uv, or roll back with --to. GitHub
and uv are faked."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tamp import __version__, install, update
from tamp.cli import main
from tamp.update import Release

NEWER, CURRENT = "0.2.0", __version__


@pytest.fixture(autouse=True)
def private_state(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "UPDATE_FILE", tmp_path / "update.json")


@pytest.fixture
def github(monkeypatch) -> dict:
    """GitHub's latest release is `answer["tag"]`; `answer["calls"]` counts the requests."""
    answer = {"tag": f"v{NEWER}", "calls": 0, "error": None}

    def get(url: str) -> bytes:
        answer["calls"] += 1
        if answer["error"]:
            raise answer["error"]
        tag = answer["tag"]
        return json.dumps(
            {"tag_name": tag, "assets": [{"name": f"plexamp_tui-{tag[1:]}-py3-none-any.whl"}]}
        ).encode()

    monkeypatch.setattr(update, "_get", get)
    return answer


def installed_as(monkeypatch, kind: str, uv_exit: int = 0) -> list[Release]:
    """Pretend tamp is installed `kind`; returns what uv was asked to install."""
    monkeypatch.setattr(install, "detect", lambda: install.Install(kind, Path("/tools/plexamp-tui")))
    installed: list[Release] = []

    def run(here: install.Install, release: Release) -> int:
        installed.append(release)
        return uv_exit

    monkeypatch.setattr(install, "run", run)
    return installed


def test_check_reports_a_newer_release_with_exit_10(github, monkeypatch, capsys) -> None:
    installed = installed_as(monkeypatch, install.UV)
    assert main(["update", "--check"]) == 10
    out = capsys.readouterr().out
    assert f"plexamp-tui {NEWER} is out (you have {CURRENT})" in out and "tamp update" in out
    assert not installed


def test_check_says_up_to_date_with_exit_0(github, monkeypatch, capsys) -> None:
    installed_as(monkeypatch, install.UV)
    github["tag"] = f"v{CURRENT}"
    assert main(["update", "--check"]) == 0
    assert "up to date" in capsys.readouterr().out


def test_update_ignores_the_days_cache(github, monkeypatch) -> None:
    installed_as(monkeypatch, install.UV)
    update.UPDATE_FILE.write_text(json.dumps({"checked_at": time.time(), "latest": CURRENT}))
    assert main(["update", "--check"]) == 10
    assert github["calls"] == 1


def test_update_installs_the_newer_release_with_uv(github, monkeypatch, capsys) -> None:
    installed = installed_as(monkeypatch, install.UV)
    assert main(["update"]) == 0
    assert installed == [Release(NEWER)]
    assert f"Updated to {NEWER}" in capsys.readouterr().out


def test_update_does_nothing_when_up_to_date(github, monkeypatch) -> None:
    installed = installed_as(monkeypatch, install.UV)
    github["tag"] = f"v{CURRENT}"
    assert main(["update"]) == 0
    assert not installed


def test_to_installs_that_release_which_is_how_you_roll_back(github, monkeypatch, capsys) -> None:
    installed = installed_as(monkeypatch, install.UV)
    assert main(["update", "--to", "v0.0.9"]) == 0
    assert installed == [Release("0.0.9")]
    assert github["calls"] == 0  # the wheel's URL follows from the version
    assert "Installed 0.0.9" in capsys.readouterr().out


@pytest.mark.parametrize("bad", ["latest", "edge", "0.2", "v0.2.0rc1", "0.2.0; rm -rf ~"])
def test_to_wants_a_release_version(github, monkeypatch, capsys, bad) -> None:
    installed = installed_as(monkeypatch, install.UV)
    assert main(["update", "--to", bad]) == 2
    assert not installed and "v0.2.0" in capsys.readouterr().err


def test_check_and_to_dont_go_together(monkeypatch) -> None:
    installed_as(monkeypatch, install.UV)
    with pytest.raises(SystemExit):
        main(["update", "--check", "--to", "v0.2.0"])


@pytest.mark.parametrize("argv", [["update"], ["update", "--check"], ["update", "--to", "v0.2.0"]])
def test_a_dev_install_is_left_alone(github, monkeypatch, capsys, argv) -> None:
    installed = installed_as(monkeypatch, install.DEV)
    assert main(argv) == 0
    assert "dev install" in capsys.readouterr().out
    assert not installed and github["calls"] == 0


def test_pipx_gets_the_command_to_run(github, monkeypatch, capsys) -> None:
    installed = installed_as(monkeypatch, install.PIPX)
    assert main(["update"]) == 10
    assert not installed
    assert "pipx install --force https://github.com/samayc16/plexamp-tui/releases/download/v0.2.0/" in (
        capsys.readouterr().out
    )


def test_pip_gets_the_command_for_to(github, monkeypatch, capsys) -> None:
    installed = installed_as(monkeypatch, install.PIP)
    assert main(["update", "--to", "v0.0.9"]) == 1
    assert not installed and "pip install --upgrade" in capsys.readouterr().out


def test_a_failed_check_says_why(github, monkeypatch, capsys) -> None:
    installed = installed_as(monkeypatch, install.UV)
    github["error"] = OSError("network is unreachable")
    assert main(["update"]) == 1
    assert "network is unreachable" in capsys.readouterr().err
    assert not installed


def test_a_failed_install_exits_1(github, monkeypatch) -> None:
    installed_as(monkeypatch, install.UV, uv_exit=2)
    assert main(["update"]) == 1


def test_without_uv_it_says_what_to_run(github, monkeypatch, capsys) -> None:
    monkeypatch.setattr(install, "detect", lambda: install.Install(install.UV, Path("/tools/plexamp-tui")))
    assert main(["update"]) == 1  # conftest takes uv off the PATH
    assert "uv tool install --upgrade https://" in capsys.readouterr().err


def test_status_and_the_control_commands_never_check(monkeypatch) -> None:
    def check(*args, **kwargs):
        raise AssertionError("checked for updates")

    monkeypatch.setattr(update, "check", check)
    for argv in (["status"], ["status", "--json"], ["status", "--tmux"], ["toggle"], ["next"]):
        main(argv)


def test_tamp_status_doesnt_even_import_the_update_check(tmp_path) -> None:
    code = "import sys; from tamp.cli import main; main(['status']); print('tamp.update' in sys.modules)"
    env = {**os.environ, "XDG_STATE_HOME": str(tmp_path)}
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
    assert out.stdout.strip().endswith("False")
