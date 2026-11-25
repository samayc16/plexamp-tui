"""The update check and installing a release, with GitHub and uv faked: nothing here touches the
network or your uv tools."""

import json
import stat
import sys
from pathlib import Path
from urllib.error import HTTPError

import pytest

from tamp import __version__, config, install, update
from tamp.update import Release

DAY = 24 * 60 * 60
WHEEL_02 = (
    "https://github.com/samayc16/plexamp-tui/releases/download/v0.2.0/plexamp_tui-0.2.0-py3-none-any.whl"
)


@pytest.fixture(autouse=True)
def private_state(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "UPDATE_FILE", tmp_path / "update.json")


class GitHub:
    """Stands in for update._get: answers like the releases/latest endpoint and counts calls."""

    def __init__(self, tag: str = "v0.2.0", assets: tuple[str, ...] | None = None, **extra) -> None:
        names = assets if assets is not None else (f"plexamp_tui-{tag[1:]}-py3-none-any.whl",)
        self.body = {"tag_name": tag, "prerelease": False, "assets": [{"name": n} for n in names], **extra}
        self.calls: list[str] = []
        self.error: Exception | None = None

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        if self.error:
            raise self.error
        return json.dumps(self.body).encode()


@pytest.fixture
def github(monkeypatch) -> GitHub:
    fake = GitHub()
    monkeypatch.setattr(update, "_get", fake)
    return fake


# ── versions ──
@pytest.mark.parametrize(
    ("text", "version"),
    [
        ("0.2.0", (0, 2, 0)),
        ("v1.10.3", (1, 10, 3)),
        ("edge", None),
        ("1.2", None),
        ("1.2.3rc1", None),
        ("v01.2.3", None),
        (" 1.2.3", None),
        (None, None),
        (5, None),
    ],
)
def test_versions_are_parsed_strictly(text, version) -> None:
    assert update.parse_version(text) == version


def test_versions_compare_as_numbers_not_text() -> None:
    assert update.is_newer("0.10.0", than="0.9.0")
    assert update.is_newer("1.0.0", than="0.99.99")
    assert not update.is_newer("0.1.0", than="0.1.0")
    assert not update.is_newer("0.0.9", than="0.1.0")
    assert not update.is_newer("edge", than="0.1.0")
    assert not update.is_newer("0.2.0", than="0.2.0.dev1")  # can't tell: say nothing


def test_a_release_installs_from_its_wheel_or_else_its_tag() -> None:
    assert Release("0.2.0").source == WHEEL_02
    assert Release("0.2.0", wheel=False).source == "git+https://github.com/samayc16/plexamp-tui@v0.2.0"
    assert Release.named("v0.2.0") == Release("0.2.0")
    assert Release.named("latest") is None


# ── asking GitHub ──
def test_the_check_is_one_anonymous_get_with_a_3_second_timeout(monkeypatch) -> None:
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None: ...

        def read(self, limit: int) -> bytes:
            return b"{}"

    def urlopen(request, timeout, context):
        seen.update(request=request, timeout=timeout)
        return Response()

    monkeypatch.setattr(update, "urlopen", urlopen)
    update._get(update.LATEST_URL)
    request = seen["request"]
    assert request.full_url == "https://api.github.com/repos/samayc16/plexamp-tui/releases/latest"
    assert request.get_method() == "GET" and request.data is None
    assert request.header_items() == [("User-agent", f"plexamp-tui/{__version__}")]
    assert seen["timeout"] == 3


def test_latest_reads_the_release_and_whether_it_has_a_wheel(github) -> None:
    assert update.latest() == Release("0.2.0", wheel=True)
    github.body["assets"] = [{"name": "plexamp_tui-0.2.0.tar.gz"}]
    assert update.latest() == Release("0.2.0", wheel=False)
    assert github.calls == [update.LATEST_URL] * 2


@pytest.mark.parametrize("body", [{"tag_name": "edge"}, {"tag_name": "v0.3.0", "prerelease": True}, [], "x"])
def test_latest_ignores_anything_but_a_numbered_release(github, body) -> None:
    github.body = body
    assert update.latest() is None


def test_no_release_yet_is_not_an_error(github) -> None:
    github.error = HTTPError(update.LATEST_URL, 404, "Not Found", {}, None)
    assert update.latest() is None


# ── at most once a day ──
def test_the_check_asks_github_at_most_once_a_day(github) -> None:
    assert update.check(now=1000) == Release("0.2.0")
    github.body["tag_name"] = "v0.3.0"
    assert update.check(now=1000 + DAY - 1) == Release("0.2.0")  # remembered
    assert len(github.calls) == 1
    assert update.check(now=1000 + DAY) == Release("0.3.0", wheel=False)
    assert len(github.calls) == 2


def test_forcing_the_check_asks_now(github) -> None:
    update.check(now=1000)
    update.check(now=1001, force=True)
    assert len(github.calls) == 2


def test_a_failed_check_waits_a_day_too(github) -> None:
    github.error = OSError("offline")
    with pytest.raises(OSError):
        update.check(now=1000)
    github.error = None
    assert update.check(now=2000) is None  # nothing known yet, and no second request
    assert len(github.calls) == 1


def test_a_clock_that_went_back_checks_again(github) -> None:
    update.check(now=5 * DAY)
    update.check(now=DAY)
    assert len(github.calls) == 2


@pytest.mark.parametrize("text", ["{broken", "[]", '{"checked_at": "yesterday", "latest": 7}'])
def test_a_damaged_update_file_just_means_checking_again(github, text) -> None:
    update.UPDATE_FILE.write_text(text)
    assert update.check(now=1000) == Release("0.2.0")
    assert config.read_state(update.UPDATE_FILE)["latest"] == "0.2.0"


def test_update_file_keeps_a_newer_versions_keys(github) -> None:
    update.UPDATE_FILE.write_text(json.dumps({"skipped": "0.1.5", "channel": "stable"}))
    update.check(now=1000)
    update.announced(Release("0.2.0"))
    data = config.read_state(update.UPDATE_FILE)
    assert (data["skipped"], data["channel"]) == ("0.1.5", "stable")
    assert (data["checked_at"], data["latest"], data["notified"]) == (1000, "0.2.0", "0.2.0")


def test_an_older_update_file_loads(github) -> None:
    update.UPDATE_FILE.write_text(json.dumps({"checked_at": 1000, "latest": "0.2.0"}))  # no "wheel" yet
    assert update.check(now=1001) == Release("0.2.0", wheel=True)
    assert not github.calls


# ── telling you once ──
def test_each_new_version_is_announced_once() -> None:
    newer = Release("9.0.0")
    assert update.worth_announcing(newer)
    update.announced(newer)
    assert not update.worth_announcing(newer)
    assert update.worth_announcing(Release("9.1.0"))
    assert not update.worth_announcing(Release(__version__))
    assert not update.worth_announcing(None)


def test_the_check_can_be_turned_off() -> None:
    on = config.Config()
    assert on.check_updates is True
    assert update.enabled(on, env={})
    assert not update.enabled(config.Config(check_updates=False), env={})
    assert not update.enabled(on, env={"TAMP_NO_UPDATE_CHECK": "1"})
    assert update.enabled(on, env={"TAMP_NO_UPDATE_CHECK": "0"})


# ── how tamp was installed ──
def site_packages(root: Path) -> Path:
    module = root / "lib" / "python3.13" / "site-packages" / "tamp" / "__init__.py"
    module.parent.mkdir(parents=True)
    return module


def test_a_uv_tool_is_recognised_by_its_receipt(tmp_path) -> None:
    (tmp_path / "uv-receipt.toml").write_text("[tool]\n")
    assert install.kind_of(tmp_path, {}, site_packages(tmp_path)) == install.UV


def test_pipx_and_pip_installs(tmp_path) -> None:
    module = site_packages(tmp_path)
    assert install.kind_of(tmp_path, {}, module) == install.PIP
    (tmp_path / "pipx_metadata.json").write_text("{}")
    assert install.kind_of(tmp_path, {}, module) == install.PIPX


def test_an_editable_install_is_a_dev_install_even_as_a_uv_tool(tmp_path) -> None:
    (tmp_path / "uv-receipt.toml").write_text("[tool]\n")
    editable = {"url": "file:///src/tamp", "dir_info": {"editable": True}}
    assert install.kind_of(tmp_path, editable, site_packages(tmp_path)) == install.DEV
    source = tmp_path / "src" / "tamp" / "__init__.py"
    assert install.kind_of(tmp_path, {}, source) == install.DEV  # run from a checkout


CHECKOUT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(
    not Path(install.__file__).resolve().is_relative_to(CHECKOUT),
    reason="testing an installed wheel, not this checkout (CI does both)",
)
def test_this_checkout_is_a_dev_install() -> None:
    assert install.detect().kind == install.DEV


def test_the_command_for_each_kind_of_install() -> None:
    release = Release("0.2.0")
    prefix = Path("/env")
    assert install.command(install.Install(install.UV, prefix), release) == [
        "uv",
        "tool",
        "install",
        "--upgrade",
        WHEEL_02,
    ]
    assert install.command(install.Install(install.PIPX, prefix), release) == [
        "pipx",
        "install",
        "--force",
        WHEEL_02,
    ]
    pip = install.command(install.Install(install.PIP, prefix), release)
    assert pip == [sys.executable, "-m", "pip", "install", "--upgrade", WHEEL_02]


# ── running uv (a fake one) ──
@pytest.fixture
def fake_uv(tmp_path, monkeypatch):
    """A stand-in uv that records its arguments and UV_TOOL_DIR, prints FAKE_UV_SAYS, and exits
    with FAKE_UV_EXIT."""
    log = tmp_path / "uv.log"
    uv = tmp_path / "bin" / "uv"
    uv.parent.mkdir()
    uv.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > "{log}"\n'
        f'printf "UV_TOOL_DIR=%s\\n" "$UV_TOOL_DIR" >> "{log}"\n'
        'printf "%b" "${FAKE_UV_SAYS:-}"\n'
        'exit "${FAKE_UV_EXIT:-0}"\n'
    )
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(install, "uv_executable", lambda: str(uv))
    return log


def tool(tmp_path) -> install.Install:
    return install.Install(install.UV, tmp_path / "tools" / "plexamp-tui")


def test_uv_installs_into_the_tool_directory_tamp_runs_from(tmp_path, fake_uv) -> None:
    assert install.run(tool(tmp_path), Release("0.2.0")) == 0
    args = fake_uv.read_text().splitlines()
    assert args == ["tool", "install", "--upgrade", WHEEL_02, f"UV_TOOL_DIR={tmp_path / 'tools'}"]


async def test_installing_from_the_app_keeps_uv_off_the_screen(tmp_path, fake_uv, capfd, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_UV_SAYS", "Installed 1 executable: tamp\\n")
    await install.run_quietly(tool(tmp_path), Release("0.2.0"))
    assert capfd.readouterr() == ("", "")
    assert WHEEL_02 in fake_uv.read_text()


async def test_a_failed_install_says_why_without_a_token(tmp_path, fake_uv, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_UV_EXIT", "2")
    monkeypatch.setenv(
        "FAKE_UV_SAYS",
        "Resolved 18 packages\\nerror: Failed to download\\n  cause: 404 (?X-Plex-Token=abc123)\\n",
    )
    with pytest.raises(install.UpdateFailed) as failed:
        await install.run_quietly(tool(tmp_path), Release("0.2.0"))
    message = str(failed.value)
    assert "error: Failed to download" in message and "404" in message
    assert "abc123" not in message


async def test_without_uv_it_says_what_to_run(tmp_path) -> None:
    with pytest.raises(install.UpdateFailed, match="uv isn't on your PATH.*uv tool install --upgrade"):
        await install.run_quietly(tool(tmp_path), Release("0.2.0"))


def test_preloading_imports_every_tamp_module_but_main() -> None:
    install.preload()
    assert {"tamp.update", "tamp.ui.menus", "tamp.demo"} <= set(sys.modules)
    assert "tamp.__main__" not in sys.modules


def test_the_suite_never_reaches_github() -> None:
    with pytest.raises(AssertionError, match="reach GitHub"):
        update._get(update.LATEST_URL)
