"""One GPU lease for Audio Studio processes sharing the same data directory."""
from __future__ import annotations

import contextlib
import errno
import os
import threading
import time

from . import runtime


class Cancelled(Exception):
    """A queued or running song was cancelled."""


@contextlib.contextmanager
def file_lease(path, cancel: threading.Event | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            def lock():
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

            def unlock():
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def lock():
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

            def unlock():
                fcntl.flock(handle, fcntl.LOCK_UN)

        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            try:
                lock()
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
            if cancel is None:
                time.sleep(0.2)
            else:
                cancel.wait(0.2)
        try:
            yield
        finally:
            unlock()


def lease(cancel: threading.Event | None = None):
    return file_lease(runtime.data_home() / ".gpu.lock", cancel)
