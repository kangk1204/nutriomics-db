"""Bounded read resources and caches; never cache an unavailable scientific result."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from .explorer_context import configured_path
from .settings import DATA_DIR, PROJECT_ROOT
from .source_replicas import ReplicaUnavailable, physical_read_path
from .source_replicas import guard as replica_guard


def fingerprint(path):
    path = Path(path).resolve()
    values = [str(path)]
    for p in (path, Path(str(path) + "-wal")):
        try:
            s = p.stat()
            values.append(None if p != path and s.st_size < 32 else (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns))
        except FileNotFoundError:
            values.append(None)
    return tuple(values)


class QueryUnavailable(Exception):
    def __init__(self, status="source_unavailable"):
        self.status = status
        super().__init__(status)


class Budget:
    def __init__(self, seconds):
        self.deadline = time.monotonic() + seconds
        self.cancelled = threading.Event()
        self.disconnected = False
        self.connections = set()
        self.lock = threading.Lock()
        self.sql_seconds = 0.0
        self.sql_connections = 0
        self.sql_execution_seconds = 0.0
        self.statements = {}
        self.memo = {}
        self.paths = {}

    def expired(self):
        return self.cancelled.is_set() or time.monotonic() >= self.deadline

    def cancel(self, disconnected=False):
        self.disconnected = self.disconnected or disconnected
        self.cancelled.set()
        with self.lock:
            for db in self.connections:
                try:
                    db.interrupt()
                except sqlite3.Error:
                    pass


CURRENT = ContextVar("explorer_query_budget", default=None)


class ProfileCursor(sqlite3.Cursor):
    def timed(self, call, *args):
        started = time.monotonic()
        try:
            return call(*args)
        finally:
            b = CURRENT.get()
            if b:
                elapsed = time.monotonic() - started
                b.sql_execution_seconds += elapsed
                item = b.statements.setdefault(getattr(self, "statement_key", "setup"), {"calls": 0, "seconds": 0.0})
                item["calls"] += 1
                item["seconds"] += elapsed

    def execute(self, sql, parameters=()):
        self.statement_key = hashlib.sha256(sql.encode()).hexdigest()[:16]
        return self.timed(super().execute, sql, parameters)

    def fetchone(self):
        return self.timed(super().fetchone)

    def fetchall(self):
        return self.timed(super().fetchall)

    def __iter__(self):
        return self

    def __next__(self):
        return self.timed(super().__next__)


class ProfileConnection(sqlite3.Connection):
    def execute(self, sql, parameters=()):
        return self.cursor(factory=ProfileCursor).execute(sql, parameters)


def logical_connection_path(db):
    """Scientific/cache identity stays permanent; PRAGMA still reports reality."""
    declared = getattr(db, 'logical_source_path', None)
    if declared is not None:
        return declared
    return next((row[2] for row in db.execute('PRAGMA database_list') if row[1] == 'main'), '')


@contextmanager
def read_connection(path):
    budget = CURRENT.get()
    original = str(path)
    path = Path(path).resolve() if budget is None else budget.paths.setdefault(original, Path(path).resolve())
    if budget and budget.expired():
        raise QueryUnavailable("query_timeout")
    started = time.monotonic()
    db = None
    try:
        physical = physical_read_path(path).resolve()
        immutable = '&immutable=1' if physical != path else ''
        db = sqlite3.connect(physical.as_uri() + "?mode=ro" + immutable, uri=True, timeout=2, factory=ProfileConnection)
        db.logical_source_path = str(path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA cache_size=-65536")
        if budget:
            db.set_progress_handler(lambda: int(budget.expired()), 1000)
            with budget.lock:
                budget.connections.add(db)
                budget.sql_connections += 1
        yield db
    except ReplicaUnavailable as exc:
        raise QueryUnavailable(exc.status) from exc
    except sqlite3.OperationalError as exc:
        raise QueryUnavailable("query_timeout" if budget and budget.expired() else "source_unavailable") from exc
    finally:
        if db is not None:
            if budget:
                with budget.lock:
                    budget.connections.discard(db)
                    budget.sql_seconds += time.monotonic() - started
            db.close()


@dataclass(frozen=True)
class FlightFailure:
    """An in-flight failure delivered to waiters, never inserted in the cache."""
    status: str


class ByteCache:
    """One byte budget shared by HTTP and effect previews; futures cross event loops."""
    def __init__(self, max_bytes=512 * 1024 * 1024):
        self.max_bytes = max_bytes
        self.items = OrderedDict()
        self.bytes = 0
        self.lock = threading.Lock()
        self.flights = {}

    def get(self, key):
        with self.lock:
            item = self.items.get(key)
            if item is not None:
                self.items.move_to_end(key)
            return item

    def put(self, key, value):
        if len(value) > self.max_bytes:
            return
        with self.lock:
            old = self.items.pop(key, None)
            self.bytes -= len(old) if old is not None else 0
            self.items[key] = value
            self.bytes += len(value)
            while self.bytes > self.max_bytes:
                self.bytes -= len(self.items.popitem(last=False)[1])

    def clear(self):
        with self.lock:
            self.items.clear()
            self.bytes = 0

    def claim(self, key):
        with self.lock:
            if key in self.flights:
                return self.flights[key], False
            future = Future()
            self.flights[key] = future
            return future, True

    def finish(self, key, value=None, *, failure=None):
        with self.lock:
            future = self.flights.pop(key, None)
            if future and not future.done():
                future.set_result(FlightFailure(failure) if failure is not None else value)


CACHE = ByteCache()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def selector_key(contrast):
    return hashlib.sha256(canonical([contrast.get(k) for k in ("series_accession", "contrast", "analysis_version")]).encode()).hexdigest()


def indexed_source(db, core_path):
    row = db.execute("SELECT value_json FROM meta WHERE key='performance'").fetchone()
    if not row:
        return False
    meta = json.loads(row[0])
    release = db.execute("SELECT value_json FROM meta WHERE key='release_id'").fetchone()
    return release is not None and meta.get("indexed_for_release") == json.loads(release[0]) and meta.get("core_fingerprint") == json.loads(canonical(fingerprint(core_path)))


def has_index(db, table):
    row = db.execute("SELECT value_json FROM meta WHERE key='performance'").fetchone()
    release = db.execute("SELECT value_json FROM meta WHERE key='release_id'").fetchone()
    if not release:
        return False
    ready = row is not None and json.loads(row[0]).get("indexed_for_release") == json.loads(release[0])
    if not ready and table in {"perf_adj", "perf_exposure_match", "perf_search"}:
        graph = db.execute("SELECT value_json FROM meta WHERE key='graph_performance'").fetchone()
        ready = graph is not None and json.loads(graph[0]).get("indexed_for_release") == json.loads(release[0])
    if not ready:
        return False
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone())


def source_paths():
    paths = [
        configured_path("NUTRIGENOMICS_EXPLORER_DATABASE", DATA_DIR / "explorer/current/explorer.sqlite"),
        configured_path("NUTRIGENOMICS_EXPLORER_CORE_DATABASE", DATA_DIR / "db/nutrigenomics.sqlite3"),
        configured_path("NUTRIGENOMICS_EXPLORER_RIGHTS", PROJECT_ROOT / "config/explorer_export_rights.json"),
    ]
    if os.environ.get('NUTRIGENOMICS_EXPLORER_EVIDENCE_INDEX'):
        paths.append(Path(os.environ['NUTRIGENOMICS_EXPLORER_EVIDENCE_INDEX']))
    if os.environ.get('NUTRIGENOMICS_PUBLIC_OMICS_INDEX'):
        ordered = Path(os.environ['NUTRIGENOMICS_PUBLIC_OMICS_INDEX'])
        paths.extend((ordered, ordered.with_suffix('.json')))
    return paths


def healthy(value):
    if isinstance(value, dict):
        if value.get("effects_status", "available") != "available":
            return False
        if value.get("source_effects_status", "available") != "available":
            return False
        if value.get("status") in {"source_unavailable", "query_timeout", "source_mismatch", "query_failed"}:
            return False
        return all(healthy(v) for v in value.values())
    if isinstance(value, list):
        return all(healthy(v) for v in value)
    return True


def append_profile(path, entry):
    try:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(canonical(entry) + "\n")
    except OSError:
        pass


class ExplorerReadMiddleware:
    """Cache complete successful JSON; bound concurrent misses and cancel SQL."""
    def __init__(self, app, include_public=False):
        self.app = app
        self.slots = threading.BoundedSemaphore(4)
        self.include_public = include_public

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not path.startswith(
            "/api/v1/" if self.include_public else "/api/v1/explorer/"
        ):
            return await self.app(scope, receive, send)
        # Manifest-authorized binary files must stream through FileResponse.
        # JSON collection would buffer an entire SQLite/Parquet download and
        # attempt UTF-8 decoding.  The outer replica guard and file route retain
        # their source identity and permission checks; the JSON index is cached.
        if self.include_public and path.startswith("/api/v1/public/files/"):
            return await self.app(scope, receive, send)
        export = path.endswith("/export")
        budget = Budget(45 if export else 10)
        token = CURRENT.set(budget)
        timer = threading.Timer(max(0, budget.deadline - time.monotonic()), budget.cancel)
        timer.daemon = True
        timer.start()
        acquired = False
        leader = False
        key = None
        watcher = None
        flight_failure = "query_failed"
        started = time.monotonic()
        try:
            # This precedes response-cache lookup, including an existing hit.
            replica_guard()
            body = bytearray()
            while True:
                event = await asyncio.wait_for(receive(), max(0.001, budget.deadline - time.monotonic()))
                if event["type"] == "http.disconnect":
                    budget.cancel(disconnected=True)
                    return
                body.extend(event.get("body", b""))
                if len(body) > 4 * 1024 * 1024:
                    return await self.error(send, 413, "request_too_large")
                if not event.get("more_body"):
                    break
            paths = source_paths()
            for p in paths:
                budget.paths[str(p)] = p.resolve()
            identity = tuple(fingerprint(p) for p in paths)
            cacheable = not export and (scope["method"] == "GET" or path.endswith("/match"))
            key = ("http", identity, scope["method"], path, scope.get("query_string", b""), bytes(body))
            if cacheable:
                cached = CACHE.get(key)
                if cached is not None:
                    replica_guard()
                    return await self.respond(send, cached, "hit", started, budget)
                future, leader = CACHE.claim(key)
                if not leader:
                    result = await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), max(0.001, budget.deadline - time.monotonic()))
                    if isinstance(result, FlightFailure):
                        raise QueryUnavailable(result.status)
                    if result is None:
                        raise QueryUnavailable("query_failed")
                    replica_guard()
                    return await self.respond(send, result, "shared", started, budget)
            while not budget.expired():
                acquired = self.slots.acquire(blocking=False)
                if acquired:
                    break
                await asyncio.sleep(0.01)
            if not acquired or budget.expired():
                raise QueryUnavailable("query_timeout")
            queue = asyncio.Queue()
            queue.put_nowait({"type": "http.request", "body": bytes(body), "more_body": False})

            async def watch():
                while True:
                    event = await receive()
                    if event["type"] == "http.disconnect":
                        budget.cancel(disconnected=True)
                        queue.put_nowait(event)
                        return

            watcher = asyncio.create_task(watch())
            messages = []

            async def capture(event):
                messages.append(event)

            await self.app(scope, queue.get, capture)
            if budget.disconnected:
                flight_failure = "query_cancelled"
                return
            if budget.expired():
                raise QueryUnavailable("query_timeout")
            head = next(m for m in messages if m["type"] == "http.response.start")
            content = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
            replica_guard()
            if identity != tuple(fingerprint(p) for p in paths):
                raise QueryUnavailable("source_changed")
            if (self.include_public and scope["method"] == "POST"
                    and path == "/api/v1/meals/analyze"):
                # Meals never enter the response cache or a shared flight.
                # Preserve the exact JSON bytes rather than wrapping a possibly
                # very large body in another JSON string and decoding it again.
                # Cancellation and source checks above still precede headers.
                headers = list(head.get("headers", [])) + [
                    (b"x-explorer-cache", b"miss"),
                    (b"server-timing", f"total;dur={(time.monotonic()-started)*1000:.3f}, sql;dur={budget.sql_execution_seconds*1000:.3f}, db_scope;dur={budget.sql_seconds*1000:.3f}".encode()),
                ]
                await send({"type": "http.response.start", "status": head["status"], "headers": headers})
                await send({"type": "http.response.body", "body": content})
                return
            result = canonical({"status": head["status"], "headers": [[k.decode("latin1"), v.decode("latin1")] for k, v in head.get("headers", [])], "body": content.decode("utf-8")}).encode()
            safe = False
            if cacheable and head["status"] == 200:
                try:
                    safe = healthy(json.loads(content))
                except (ValueError, UnicodeError):
                    pass
            if safe:
                CACHE.put(key, result)
            if leader:
                CACHE.finish(key, result)
                leader = False
            await self.respond(send, result, "miss", started, budget)
        except (TimeoutError, QueryUnavailable, ReplicaUnavailable) as exc:
            flight_failure = getattr(exc, "status", "query_timeout")
            budget.cancel()
            if not budget.disconnected:
                await self.error(send, 503, flight_failure)
        finally:
            if watcher:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            timer.cancel()
            if acquired:
                self.slots.release()
            if leader:
                CACHE.finish(key, failure=flight_failure)
            profile = os.environ.get("NUTRIGENOMICS_EXPLORER_PROFILE")
            if profile:
                # Server-local diagnostics, never parameters or scientific payloads.
                entry = {"unix": time.time(), "path": path, "seconds": time.monotonic()-started,
                         "sql_seconds": budget.sql_execution_seconds, "connections": budget.sql_connections,
                         "statements": budget.statements, "cancelled": budget.cancelled.is_set()}
                await asyncio.to_thread(append_profile, profile, entry)
            CURRENT.reset(token)

    @staticmethod
    async def error(send, status, code):
        data = canonical({"detail": {"code": code, "message": "조회할 수 없습니다. 근거 부재나 결과 0건을 의미하지 않습니다."}}).encode()
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json; charset=utf-8"), (b"retry-after", b"2")]})
        await send({"type": "http.response.body", "body": data})

    @staticmethod
    async def respond(send, cached, cache_status, started, budget):
        result = json.loads(cached)
        headers = [(k.encode("latin1"), v.encode("latin1")) for k, v in result["headers"]]
        headers += [(b"x-explorer-cache", cache_status.encode()), (b"server-timing", f"total;dur={(time.monotonic()-started)*1000:.3f}, sql;dur={budget.sql_execution_seconds*1000:.3f}, db_scope;dur={budget.sql_seconds*1000:.3f}".encode())]
        await send({"type": "http.response.start", "status": result["status"], "headers": headers})
        await send({"type": "http.response.body", "body": result["body"].encode("utf-8")})
