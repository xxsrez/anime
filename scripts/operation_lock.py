"""Cross-process locking for operations that mutate or snapshot the Anime DB."""

import fcntl
from pathlib import Path
import time


DEFAULT_LOCK_TIMEOUT_SECONDS = 30.0


class OperationLockError(RuntimeError):
    pass


def default_lock_path(db_path):
    # Relative, absolute and symlink aliases of the same SQLite file must
    # converge on one inter-process lock.
    canonical_db_path = Path(db_path).expanduser().resolve()
    return Path(f"{canonical_db_path}.operation.lock")


class DatabaseOperationLock:
    def __init__(
        self,
        db_path,
        *,
        path=None,
        wait=False,
        timeout=DEFAULT_LOCK_TIMEOUT_SECONDS,
        poll_interval=0.1,
        operation="database operation",
    ):
        self.path = Path(path) if path else default_lock_path(db_path)
        self.wait = wait
        self.timeout = None if timeout is None else max(0.0, float(timeout))
        self.poll_interval = max(0.01, float(poll_interval))
        self.operation = operation
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        try:
            deadline = None
            if self.wait and self.timeout is not None:
                deadline = time.monotonic() + self.timeout
            while True:
                try:
                    fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if not self.wait or (deadline is not None and time.monotonic() >= deadline):
                        waited = (
                            f" after {self.timeout:g}s"
                            if self.wait and self.timeout is not None
                            else ""
                        )
                        raise OperationLockError(
                            f"another database operation is already running{waited}: {self.path}"
                        ) from exc
                    remaining = self.poll_interval
                    if deadline is not None:
                        remaining = min(remaining, max(0.0, deadline - time.monotonic()))
                    time.sleep(remaining)
            self.handle.seek(0)
            self.handle.truncate()
            self.handle.write(self.operation + "\n")
            self.handle.flush()
            return self
        except BaseException:
            self.handle.close()
            self.handle = None
            raise

    def __exit__(self, _exc_type, _exc, _tb):
        if self.handle is not None:
            fcntl.flock(self.handle, fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None
