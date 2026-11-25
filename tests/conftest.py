"""Every test runs against throwaway config, state and cache directories, set before tamp is
imported (its paths are fixed at import). Nothing here may read your Plex sign-in or touch the
queue and status of a tamp you have running. Nor may a test ask GitHub for releases or run uv:
one that tries fails loudly instead."""

import atexit
import os
import shutil
import tempfile

import pytest

_root = tempfile.mkdtemp(prefix="tamp-tests-")
atexit.register(shutil.rmtree, _root, ignore_errors=True)
for var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
    os.environ[var] = os.path.join(_root, var.lower())


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    from tamp import install, update

    def no_network(*args, **kwargs):
        raise AssertionError("a test tried to reach GitHub")

    monkeypatch.setattr(update, "urlopen", no_network)
    monkeypatch.setattr(install, "uv_executable", lambda: None)  # tests that run "uv" bring a fake
