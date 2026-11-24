"""Where blocking calls (the network, mostly) run, so that quitting never waits for them."""

from __future__ import annotations

import concurrent.futures
import threading


class DaemonExecutor(concurrent.futures.ThreadPoolExecutor):
    """asyncio's default executor (asyncio.to_thread), with each call on its own daemon thread.

    Python joins a ThreadPoolExecutor's threads at exit, and asyncio.run waits for them as it
    shuts down: one request to a server that had stopped answering could hold tamp open for a
    minute after you quit. Daemon threads are simply dropped. (asyncio only accepts a
    ThreadPoolExecutor, hence the subclass.)"""

    def __init__(self) -> None:
        super().__init__(max_workers=1)  # never starts a worker: submit() is replaced

    def submit(self, fn, /, *args, **kwargs) -> concurrent.futures.Future:
        future: concurrent.futures.Future = concurrent.futures.Future()

        def run() -> None:
            if not future.set_running_or_notify_cancel():
                return
            try:
                result = fn(*args, **kwargs)
            except BaseException as e:
                future.set_exception(e)
            else:
                future.set_result(result)

        threading.Thread(target=run, name="tamp-io", daemon=True).start()
        return future

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        """Nothing to wait for: the threads end with the process."""
