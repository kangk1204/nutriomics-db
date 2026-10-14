"""Process-local admission for owned analysis-download temporary copies.

This is not a filesystem or multi-worker quota. It accounts only snapshots
created by this process; unknown/preexisting files are never reclaimed here.
"""
from __future__ import annotations

import os
from pathlib import Path
from threading import Lock

_BUDGET_ENV = "NUTRIGENOMICS_DOWNLOAD_TEMP_BUDGET_BYTES"
_DEFAULT_BUDGET_BYTES = 256 * 1024 * 1024
_MIN_RESERVATION_BYTES = 1024 * 1024
_MAX_BUDGET_BYTES = 8 * 1024 * 1024 * 1024


class DownloadBudgetUnavailable(OSError):
    """Fail-closed admission/cleanup error, handled as503 by the existing API."""


def _configured_budget_bytes() -> int:
    raw = os.environ.get(_BUDGET_ENV)
    if raw is None:
        return _DEFAULT_BUDGET_BYTES
    raw = raw.strip()
    if not raw or len(raw) > 10 or any(char not in "0123456789" for char in raw):
        raise DownloadBudgetUnavailable("Download temporary-copy budget is invalid")
    value = int(raw)
    if not _MIN_RESERVATION_BYTES <= value <= _MAX_BUDGET_BYTES:
        raise DownloadBudgetUnavailable("Download temporary-copy budget is invalid")
    return value


class _TemporaryCopyBudget:
    def __init__(self):
        self._lock = Lock()
        self._owner_pid = os.getpid()
        self._reserved_bytes = 0

    @property
    def reserved_bytes(self) -> int:
        with self._lock:
            return self._reserved_bytes

    def reserve(self, content_bytes: int) -> _CopyReservation:
        if os.getpid() != self._owner_pid:
            raise DownloadBudgetUnavailable("Inherited download budget cannot admit copies")
        amount = max(content_bytes, _MIN_RESERVATION_BYTES)
        with self._lock:
            limit = _configured_budget_bytes()
            if amount > limit - self._reserved_bytes:
                raise DownloadBudgetUnavailable("Download temporary-copy budget is exhausted")
            reservation = _CopyReservation(self, amount, self._owner_pid)
            self._reserved_bytes += amount
            return reservation

    def _release(self, reservation: _CopyReservation) -> bool:
        if os.getpid() != self._owner_pid:
            return False
        with self._lock:
            if not reservation._released:
                self._reserved_bytes -= reservation.amount
                reservation._released = True
            return True


class _CopyReservation:
    def __init__(self, budget: _TemporaryCopyBudget, amount: int, owner_pid: int):
        self._budget = budget
        self.amount = amount
        self.owner_pid = owner_pid
        self._released = False

    def release(self) -> bool:
        return self._budget._release(self)


class _OwnedSnapshot:
    """Delete only the exact created file, then return reservation once.

    Unlink/ownership errors retain the credit; releasing it while bytes remain
    would allow this accounting limit to be bypassed. No stale-file janitor.
    """

    def __init__(self, path: Path, reservation: _CopyReservation, stat_result: os.stat_result):
        if not stat_result.st_ino:
            raise DownloadBudgetUnavailable("Download snapshot ownership is unavailable")
        self.path = path
        self._reservation = reservation
        self._identity = (stat_result.st_dev, stat_result.st_ino)
        self._lock = Lock()
        self._done = False

    def cleanup(self) -> bool:
        if os.getpid() != self._reservation.owner_pid:
            return False  # A forked child must not delete its parent's snapshot.
        with self._lock:
            if self._done:
                return True
            try:
                current = self.path.lstat()
            except FileNotFoundError:
                pass
            else:
                if (current.st_dev, current.st_ino) != self._identity:
                    raise DownloadBudgetUnavailable("Download snapshot ownership changed")
                self.path.unlink(missing_ok=True)
            self._reservation.release()
            self._done = True
            return True


_PROCESS_BUDGET = _TemporaryCopyBudget()


def reserve_temporary_copy(content_bytes: int) -> _CopyReservation:
    return _PROCESS_BUDGET.reserve(content_bytes)


def _reset_after_fork() -> None:
    global _PROCESS_BUDGET
    # Inherited locks/credits are not shared accounting. Parent leases stay owned
    # by the parent PID; a new child gets a separate process-local ledger.
    _PROCESS_BUDGET = _TemporaryCopyBudget()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)
