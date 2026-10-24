"""One billable personal-account batch across desktop CLI/UI processes."""
from contextlib import contextmanager
import os
from pathlib import Path


class BatchBusy(RuntimeError):
    pass


@contextmanager
def batch_lock(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "batch.lock"
    if path.is_symlink():
        raise BatchBusy("Invalid batch lock")
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "r+b", buffering=0) as stream:
        if not path.stat().st_size:
            stream.write(b"0")
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise BatchBusy("Another ChatGPT comparison is already running") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def batch_busy(directory):
    try:
        with batch_lock(directory):
            return False
    except BatchBusy:
        return True
