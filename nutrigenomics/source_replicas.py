"""Guarded whole-file read replicas; permanent sources retain their identity.

The map is a serving optimization, never a scientific release or a fallback.
Every source and replica is checked before cache lookup and before response.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import time
from pathlib import Path

from .source_updates import atomic_json

ENV = 'NUTRIOMICS_SOURCE_REPLICA_MAP'
SCOPE_ENV = 'NUTRIOMICS_SOURCE_REPLICA_SCOPE'
MAX_BYTES = 42 * 1024**3
RESERVE_BYTES = 24 * 1024**3
_ACTIVE = None
ROLES = {'internal': {'internal_release', 'internal_core', 'internal_evidence'},
         'public': {'public_release', 'public_evidence', 'public_omics'}}


class ReplicaUnavailable(Exception):
    status = 'source_changed'


def closed_fingerprint(path):
    path = Path(path).resolve()
    value = path.stat()
    wal = Path(str(path) + '-wal')
    if wal.exists() and wal.stat().st_size:
        raise ReplicaUnavailable('Nonempty WAL invalidates the immutable replica')
    return [str(path), [value.st_dev, value.st_ino, value.st_size,
                         value.st_mtime_ns, value.st_ctime_ns], None]


def file_sha(path, check=None):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while block := stream.read(4 * 1024**2):
            digest.update(block)
            if check:
                check()
    return digest.hexdigest()


def memory_available():
    for line in Path('/proc/meminfo').read_text().splitlines():
        if line.startswith('MemAvailable:'):
            return int(line.split()[1]) * 1024
    raise ValueError('Linux MemAvailable is required for RAM serving')


def cgroup_budget():
    path = Path('/sys/fs/cgroup') / Path('/proc/self/cgroup').read_text().strip().split(':')[-1].lstrip('/')
    result = {}
    for name in ('memory.max', 'memory.high', 'memory.current', 'memory.events'):
        if (path / name).exists():
            result[name] = (path / name).read_text().strip()
    return result


def rss_bytes():
    try:
        return int(Path('/proc/self/statm').read_text().split()[1]) * os.sysconf('SC_PAGE_SIZE')
    except (OSError, ValueError, AttributeError):
        return None


def _directory_bytes(root):
    return sum(p.stat().st_size for p in Path(root).rglob('*') if p.is_file())


def commit_snapshot():
    meminfo = {line.split(':')[0]: int(line.split()[1])*1024
               for line in Path('/proc/meminfo').read_text().splitlines()
               if line.startswith(('Shmem:', 'SwapFree:', 'CommitLimit:', 'Committed_AS:'))}
    overcommit = {key: Path('/proc/sys/vm', key).read_text().strip()
                  for key in ('overcommit_memory', 'overcommit_ratio')}
    return meminfo, overcommit


def resource_check(root, *, pending_bytes=0):
    """Hard project cap and host/cgroup reserve, also checked during streaming."""
    used = _directory_bytes(root)
    available = memory_available()
    cgroup = cgroup_budget()
    meminfo, overcommit = commit_snapshot()
    current = int(cgroup.get('memory.current', 0))
    for name in ('memory.max', 'memory.high'):
        limit = cgroup.get(name, 'max')
        if limit != 'max':
            available = min(available, max(0, int(limit) - current))
    if used + pending_bytes > MAX_BYTES:
        raise ValueError('RAM replica files exceed the 42 GiB project cap')
    if available - pending_bytes < RESERVE_BYTES:
        raise ValueError('RAM copy would breach the 24 GiB available-memory reserve')
    if shutil.disk_usage(root).free < pending_bytes:
        raise ValueError('Insufficient tmpfs capacity')
    if (overcommit['overcommit_memory'] == '2'
            and meminfo['CommitLimit'] - meminfo['Committed_AS'] < pending_bytes):
        raise ValueError('Strict overcommit policy cannot admit the planned RAM allocation')
    return {'project_bytes': used, 'memory_available_bytes': available,
            'tmpfs_available_bytes': shutil.disk_usage(root).free, 'cgroup': cgroup,
            'rss_bytes': rss_bytes(),
            'meminfo': meminfo, 'overcommit': overcommit,
            'pressure': {kind: Path(f'/proc/pressure/{kind}').read_text().strip()
                         for kind in ('memory', 'io')}}


def _private_directory(path):
    path = Path(path).resolve()
    value = path.stat()
    if not path.is_dir() or (os.name != 'nt' and (
            value.st_uid != os.getuid() or stat.S_IMODE(value.st_mode) != 0o700)):
        raise ValueError('Replica project directories must be owned by the operator and mode 0700')
    return path


def _tmpfs_directory(path):
    path = _private_directory(path)
    mounts = []
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        left, right = line.split(' - ', 1)
        mount = Path(left.split()[4].replace('\\040', ' ')).resolve()
        if path == mount or mount in path.parents:
            mounts.append((len(mount.parts), right.split()[0]))
    if not mounts or max(mounts)[1] != 'tmpfs':
        raise ValueError('Whole-file RAM replicas require an existing tmpfs; no mount settings are changed')


def copy_plan(plan, ram_root, receipt_directory):
    """Sequential immutable file copies; failures retain explicit .partial evidence.

    Source SHA before/after, streamed copy SHA, and independent replica SHA must
    all agree. No SQLite table is transformed and no source file is written.
    """
    ram_root, receipt_directory = Path(ram_root), Path(receipt_directory)
    ram_root.mkdir(mode=0o700, parents=False, exist_ok=False)
    _tmpfs_directory(ram_root)
    receipt_directory.mkdir(parents=True, exist_ok=False)
    items = plan.get('files', [])
    if not items or {r.get('scope') for r in items} - {'internal', 'public'}:
        raise ValueError('A nonempty, explicitly scoped replica plan is required')
    if any(r.get('role') not in ROLES[r['scope']] for r in items):
        raise ValueError('Replica roles must match their private/public scope')
    sources = [str(Path(r['source']).resolve()) for r in items]
    if len(set(sources)) != len(sources):
        raise ValueError('Duplicate logical source in replica plan')
    total = sum(Path(r['source']).stat().st_size for r in items)
    resource_check(ram_root, pending_bytes=total)
    started, cpu_started = time.monotonic(), time.process_time()
    report = {'schema': 1, 'complete': False, 'scientific_transformations': 0,
              'cap_bytes': MAX_BYTES, 'reserve_bytes': RESERVE_BYTES,
              'ram_root': str(ram_root.resolve()), 'entries': [],
              'resources_before': resource_check(ram_root)}
    def check():
        if time.monotonic() - started > 3600:
            raise TimeoutError('RAM copy exceeded the separate 3600-second operator budget')
        current = resource_check(ram_root)
        available = current.get('memory_available_bytes')
        resident = current.get('rss_bytes')
        if available is not None:
            report['minimum_available_memory_bytes'] = min(report.get('minimum_available_memory_bytes', available), available)
        if resident is not None:
            report['peak_sampled_rss_bytes'] = max(report.get('peak_sampled_rss_bytes', resident), resident)
        report['peak_project_file_bytes'] = max(report.get('peak_project_file_bytes', 0), current.get('project_bytes', 0))
    atomic_json(receipt_directory / 'copy.json', report)
    try:
        for item in items:
            scope = item['scope']
            source = Path(item['source']).resolve()
            name = item['name']
            if Path(name).name != name or name in ('', '.', '..'):
                raise ValueError('Replica name must be a single file name')
            directory = ram_root / scope
            directory.mkdir(mode=0o700, exist_ok=True)
            _private_directory(directory)
            target = directory / name
            partial = target.with_name(target.name + '.partial')
            expected = item['sha256']
            before = closed_fingerprint(source)
            resource_check(ram_root, pending_bytes=source.stat().st_size)
            step_started, step_cpu = time.monotonic(), time.process_time()
            source_sha_before = file_sha(source, check)
            if source_sha_before != expected or closed_fingerprint(source) != before:
                raise ReplicaUnavailable('Permanent source failed its pre-copy SHA/fingerprint')
            digest = hashlib.sha256()
            with source.open('rb') as src, partial.open('xb') as dst:
                os.chmod(partial, 0o600)
                while block := src.read(4 * 1024**2):
                    dst.write(block)
                    digest.update(block)
                    check()
                dst.flush()
                os.fsync(dst.fileno())
            source_sha_after = file_sha(source, check)
            replica_sha = file_sha(partial, check)
            if (source_sha_after != expected or digest.hexdigest() != expected
                    or replica_sha != expected or closed_fingerprint(source) != before):
                raise ReplicaUnavailable('Permanent source/copy changed during verification')
            partial.rename(target)
            os.chmod(target, 0o400)
            companions = []
            for companion in item.get('companions', []):
                p = Path(companion).resolve()
                companions.append({'path': str(p), 'fingerprint': closed_fingerprint(p), 'sha256': file_sha(p)})
            report['entries'].append({'name': name, 'scope': scope,
                'role': item['role'],
                'source': str(source), 'replica': str(target.resolve()),
                'sha256': expected, 'bytes': source.stat().st_size,
                'source_fingerprint': before, 'replica_fingerprint': closed_fingerprint(target),
                'source_sha256_before': source_sha_before, 'source_sha256_after': source_sha_after,
                'replica_sha256': replica_sha, 'copy_stream_sha256': digest.hexdigest(),
                'seconds': time.monotonic() - step_started,
                'cpu_seconds': time.process_time()-step_cpu, 'companions': companions})
            atomic_json(receipt_directory / 'copy.json', report)
        maps = {}
        for scope in sorted({r['scope'] for r in report['entries']}):
            mapping = {'schema': 1, 'scope': scope, 'release_id': plan['releases'][scope],
                       'complete': True, 'ram_root': str(ram_root.resolve()),
                       'entries': [r for r in report['entries'] if r['scope'] == scope],
                       'identity': 'Logical permanent source paths remain canonical; verified whole-file replicas only replace physical reads'}
            path = receipt_directory / f'{scope}-map.json'
            atomic_json(path, mapping)
            os.chmod(path, 0o600)
            maps[scope] = {'path': str(path), 'sha256': file_sha(path)}
        report.update(complete=True, maps=maps, elapsed_seconds=time.monotonic()-started,
                      cpu_seconds=time.process_time()-cpu_started,
                      resources_after=resource_check(ram_root))
        atomic_json(receipt_directory / 'copy.json', report)
        return report
    except BaseException as exc:
        report.update(error=repr(exc), elapsed_seconds=time.monotonic()-started,
                      cpu_seconds=time.process_time()-cpu_started)
        atomic_json(receipt_directory / 'copy.json', report)
        raise


def initialize(application, release_id):
    """Pin and independently hash this scope's replicas before opening HTTP."""
    global _ACTIVE
    configured = os.environ.get(ENV)
    if not configured:
        _ACTIVE = None
        return {'enabled': False}
    _ACTIVE = None
    path = Path(configured).resolve()
    before = closed_fingerprint(path)
    value = json.loads(path.read_text(encoding='utf-8'))
    if (value.get('schema') != 1 or value.get('complete') is not True
            or value.get('scope') != application or value.get('release_id') != release_id
            or os.environ.get(SCOPE_ENV) != application or not value.get('entries')):
        raise ReplicaUnavailable('Replica map scope/release/readiness mismatch')
    root = _private_directory(value['ram_root'])
    started = time.monotonic()
    entries = {}
    for row in value['entries']:
        source, replica = Path(row['source']).resolve(), Path(row['replica']).resolve()
        if (row.get('scope') != application or row.get('role') not in ROLES[application]
                or root / application not in replica.parents):
            raise ReplicaUnavailable('Cross-scope replica mapping is forbidden')
        _private_directory(replica.parent)
        if os.name != 'nt' and stat.S_IMODE(replica.stat().st_mode) != 0o400:
            raise ReplicaUnavailable('RAM source replicas must be mode 0400')
        if (closed_fingerprint(source) != row['source_fingerprint']
                or closed_fingerprint(replica) != row['replica_fingerprint']
                or file_sha(replica) != row['sha256']):
            raise ReplicaUnavailable('Replica source identity/checksum mismatch')
        if str(source) in entries:
            raise ReplicaUnavailable('Duplicate logical source mapping')
        entries[str(source)] = row
    active = {'map': str(path), 'map_fingerprint': before, 'map_sha256': file_sha(path),
              'scope': application, 'release_id': release_id, 'entries': entries}
    _check(active)
    _ACTIVE = active
    return {'enabled': True, 'scope': application, 'release_id': release_id,
            'map': str(path), 'map_sha256': active['map_sha256'],
            'whole_file_replicas': len(entries), 'bytes': sum(r['bytes'] for r in entries.values()),
            'verification_seconds': time.monotonic()-started, 'scientific_transformations': 0,
            'source_freshness_checked_before_response_cache': True}


def _check(active):
    if closed_fingerprint(active['map']) != active['map_fingerprint']:
        raise ReplicaUnavailable('Replica receipt changed; restart verification required')
    for row in active['entries'].values():
        if (closed_fingerprint(row['source']) != row['source_fingerprint']
                or closed_fingerprint(row['replica']) != row['replica_fingerprint']):
            raise ReplicaUnavailable('Permanent source or disposable replica changed')
        for companion in row.get('companions', []):
            if closed_fingerprint(companion['path']) != companion['fingerprint']:
                raise ReplicaUnavailable('Source derivative receipt changed')


def guard():
    if not os.environ.get(ENV):
        if _ACTIVE is not None:
            raise ReplicaUnavailable('An active replica map cannot be silently disabled')
        return
    if _ACTIVE is None or str(Path(os.environ[ENV]).resolve()) != _ACTIVE['map']:
        raise ReplicaUnavailable('Configured RAM replicas have not passed readiness')
    if os.environ.get(SCOPE_ENV) != _ACTIVE['scope']:
        raise ReplicaUnavailable('Active replica scope changed')
    try:
        _check(_ACTIVE)
    except (OSError, ValueError) as exc:
        raise ReplicaUnavailable('Source/replica freshness cannot be established') from exc


def physical_read_path(path):
    """Keep all unlisted files and all public/private identities separate."""
    guard()
    if not os.environ.get(ENV):
        return Path(path)
    logical = str(Path(path).resolve())
    row = _ACTIVE['entries'].get(logical)
    if row is None and _ACTIVE['scope'] == 'public' and Path(path).suffix in {'.db', '.sqlite', '.sqlite3'}:
        raise ReplicaUnavailable('Public replica service cannot open an unlisted database')
    return Path(row['replica']) if row else Path(path)


class ReplicaGuardMiddleware:
    """Protect established meal/study routes as well as the Explorer cache."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get('type') != 'http' or not scope.get('path', '').startswith('/api/v1/'):
            return await self.app(scope, receive, send)
        started = False
        async def checked_send(message):
            nonlocal started
            if message['type'] == 'http.response.start':
                guard()
                started = True
            await send(message)
        try:
            guard()
            await self.app(scope, receive, checked_send)
        except ReplicaUnavailable:
            if started:
                raise  # Never append an error body to a partial scientific response.
            data = json.dumps({'detail': {'code': 'source_changed',
                'message': 'Permanent source or read replica changed; unavailable does not mean zero evidence.'}}).encode()
            await send({'type':'http.response.start','status':503,
                'headers':[(b'content-type',b'application/json'),(b'retry-after',b'2')]})
            await send({'type':'http.response.body','body':data})
