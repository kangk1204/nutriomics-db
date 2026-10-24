"""Bounded shared page caches for explicitly verified immutable SQLite inputs.

The original files/identifiers and SQL results remain unchanged. Keepers retain
pages across short read connections; no whole-file RAM copy or mount is needed.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

MAX_TOTAL_MIB = 2048
_ACTIVE = None


def close():
    global _ACTIVE
    previous, _ACTIVE = _ACTIVE, None
    if previous:
        for row in previous.values():
            row['keeper'].close()


def initialize(inputs, verified_fingerprints):
    """Called only after startup source verification, before HTTP readiness."""
    global _ACTIVE
    from .source_replicas import closed_fingerprint, memory_available, RESERVE_BYTES
    if _ACTIVE is not None:
        raise ValueError('Read page cache is initialized once per process')
    if (not inputs or any(type(v) is not int or v <= 0 for v in inputs.values())
            or sum(inputs.values()) > MAX_TOTAL_MIB):
        raise ValueError('Read page cache must be positive and total at most 2048 MiB')
    total = sum(inputs.values()) * 1024**2
    if memory_available() - total < RESERVE_BYTES:
        raise ValueError('Read page cache would breach the 24 GiB reserve')
    active = {}
    try:
        for path, mib in inputs.items():
            path = Path(path).resolve(strict=True)
            before = closed_fingerprint(path)
            if before != verified_fingerprints.get(str(path)):
                raise ValueError('Input is not the same verified closed source')
            uri = path.as_uri() + '?mode=ro&immutable=1&cache=shared'
            keeper = sqlite3.connect(uri, uri=True, check_same_thread=False)
            active[str(path)] = dict(keeper=keeper, uri=uri, kib=mib*1024, fingerprint=before)
            keeper.execute('PRAGMA query_only=ON')
            keeper.execute('PRAGMA cache_size=-' + str(mib*1024))
            keeper.execute('SELECT count(*) FROM sqlite_master').fetchone()
            if closed_fingerprint(path) != before:
                raise ValueError('Input changed during page-cache initialization')
        _ACTIVE = active
        guard()
    except BaseException:
        for row in active.values():
            row['keeper'].close()
        _ACTIVE = None
        raise
    return dict(enabled=True, databases=len(active), total_limit_mib=sum(inputs.values()),
                source_files_modified=0, whole_file_copy=False,
                source_guard_before_response_cache=True,
                scope='SQLite page-cache limits; actual allocation is demand-driven')


def guard():
    if _ACTIVE is None:
        return
    from .source_replicas import closed_fingerprint, ReplicaUnavailable
    try:
        for path, row in _ACTIVE.items():
            if closed_fingerprint(path) != row['fingerprint']:
                raise ReplicaUnavailable('Verified page-cache source changed')
    except (OSError, ValueError) as exc:
        raise ReplicaUnavailable('Verified page-cache source is unavailable') from exc


def connection_settings(logical, physical):
    guard()
    row = (_ACTIVE or {}).get(str(Path(logical).resolve()))
    if row is None:
        return None
    if Path(physical).resolve() != Path(logical).resolve():
        from .source_replicas import ReplicaUnavailable
        raise ReplicaUnavailable('Shared page cache cannot silently switch physical replicas')
    return row['uri'], row['kib']
