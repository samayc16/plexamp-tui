"""Every test runs against throwaway config, state and cache directories, set before tamp is
imported (its paths are fixed at import). Nothing here may read your Plex sign-in or touch the
queue and status of a tamp you have running."""

import atexit
import os
import shutil
import tempfile

_root = tempfile.mkdtemp(prefix="tamp-tests-")
atexit.register(shutil.rmtree, _root, ignore_errors=True)
for var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
    os.environ[var] = os.path.join(_root, var.lower())
