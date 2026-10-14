"""Synthetic resource admission and ownership; no global/physical disk quota."""
from __future__ import annotations

import gc
import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, Lock

import anyio
import pytest
import test_analysis_download_integrity as integrity_fixtures
import test_analysis_download_lifecycle as lifecycle_fixtures
from nutrigenomics import analysis_downloads, download_budget

artifact = integrity_fixtures.artifact
snapshots = lifecycle_fixtures.snapshots
CONTENT = integrity_fixtures.CONTENT
MIB = 1024 * 1024


@pytest.fixture(autouse=True)
def budget(monkeypatch):
    instance = download_budget._TemporaryCopyBudget()
    monkeypatch.setattr(download_budget, "_PROCESS_BUDGET", instance)
    monkeypatch.setenv(download_budget._BUDGET_ENV, str(2 * MIB))
    yield instance
    assert instance.reserved_bytes == 0


def prepare(artifact):
    path, _, run = artifact
    return analysis_downloads.verified_analysis_response(path, path.name, run.output_manifest_json[0])


def finish(response):
    async def send(event):
        pass

    lifecycle_fixtures.dispatch(response, send)


def test_default_and_invalid_config_fail_closed_before_creation(artifact, snapshots, budget, monkeypatch):
    monkeypatch.delenv(download_budget._BUDGET_ENV)
    assert download_budget._configured_budget_bytes() == 256 * MIB
    invalid = ["", "0", "-1", "1.5", "1048575", str(8 * 1024 * MIB + 1), "bad", "9" * 100]
    for value in invalid:
        monkeypatch.setenv(download_budget._BUDGET_ENV, value)
        with integrity_fixtures.client_for(artifact) as client:
            response = client.get("/synthetic-download")
        assert response.status_code == 503
        assert response.json()["detail"] == "Analysis output unavailable"
        assert budget.reserved_bytes == 0
    assert snapshots == []


def test_file_larger_than_budget_rejected_without_copy(artifact, snapshots, budget, monkeypatch):
    path, session, run = artifact
    content = b"L" * (MIB + 1)
    path.write_bytes(content)
    run.output_manifest_json = [{"path": str(path), "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}]
    session.commit()
    monkeypatch.setenv(download_budget._BUDGET_ENV, str(MIB))
    with integrity_fixtures.client_for(artifact) as client:
        response = client.get("/synthetic-download")
    assert response.status_code == 503
    assert snapshots == [] and budget.reserved_bytes == 0


def test_live_snapshots_exhaust_then_return_capacity(artifact, snapshots, budget):
    first, second = prepare(artifact), prepare(artifact)
    assert budget.reserved_bytes == 2 * MIB
    with integrity_fixtures.client_for(artifact) as client:
        response = client.get("/synthetic-download")
    assert response.status_code == 503
    assert len(snapshots) == 2  # Rejection precedes mkstemp.
    finish(first)
    assert budget.reserved_bytes == MIB
    third = prepare(artifact)
    assert budget.reserved_bytes == 2 * MIB
    finish(second)
    finish(third)
    assert all(not path.exists() for path in snapshots)


def test_concurrent_thread_admission_never_oversubscribes(artifact, snapshots, budget):
    barrier = Barrier(8, timeout=5)
    path, _, run = artifact
    manifest = dict(run.output_manifest_json[0])  # Resolve ORM attributes on the owner thread.

    def request(_):
        barrier.wait()
        try:
            return analysis_downloads.verified_analysis_response(path, path.name, manifest)
        except download_budget.DownloadBudgetUnavailable:
            return None

    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(request, range(8)))
    responses = [response for response in results if response is not None]
    assert len(responses) == len(snapshots) == 2
    assert budget.reserved_bytes == 2 * MIB
    for response in responses:
        finish(response)
    assert all(not path.exists() for path in snapshots)


def test_zero_byte_files_still_reserve_a_minimum(tmp_path, snapshots, budget):
    path = tmp_path / "empty.tsv"
    path.write_bytes(b"")
    manifest = {"bytes": 0, "sha256": hashlib.sha256(b"").hexdigest()}
    first = analysis_downloads.verified_analysis_response(path, path.name, manifest)
    second = analysis_downloads.verified_analysis_response(path, path.name, manifest)
    with pytest.raises(download_budget.DownloadBudgetUnavailable):
        analysis_downloads.verified_analysis_response(path, path.name, manifest)
    assert budget.reserved_bytes == 2 * MIB
    finish(first)
    finish(second)
    assert all(not path.exists() for path in snapshots)


def test_non_tiny_copy_reserves_its_full_content_size(tmp_path, snapshots, budget):
    content = b"F" * (MIB + 123)
    path = tmp_path / "full-size.tsv"
    path.write_bytes(content)
    response = analysis_downloads.verified_analysis_response(path, path.name, lifecycle_fixtures.registered(content))
    assert budget.reserved_bytes == len(content)
    assert snapshots[0].stat().st_size == len(content)
    finish(response)


def test_cancelled_body_returns_credit_only_after_file_closes(artifact, snapshots, budget):
    response = prepare(artifact)
    assert budget.reserved_bytes == MIB

    async def run():
        async def receive():
            return {"type": "http.disconnect"}

        with anyio.CancelScope() as cancellation:
            async def send(event):
                if event["type"] == "http.response.body":
                    assert budget.reserved_bytes == MIB
                    cancellation.cancel()
                    await anyio.lowlevel.checkpoint()

            await response({"type": "http", "method": "GET", "headers": []}, receive, send)
        assert cancellation.cancel_called

    anyio.run(run)
    assert budget.reserved_bytes == 0
    assert all(not path.exists() for path in snapshots)


def test_send_error_returns_credit(artifact, snapshots, budget):
    response = prepare(artifact)

    async def send(event):
        if event["type"] == "http.response.body":
            raise BrokenPipeError("Synthetic disconnect")

    with pytest.raises(BrokenPipeError):
        lifecycle_fixtures.dispatch(response, send)
    assert budget.reserved_bytes == 0
    assert all(not path.exists() for path in snapshots)


def test_mkstemp_failure_returns_credit(artifact, snapshots, budget, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("Synthetic temp allocation failure")

    monkeypatch.setattr(analysis_downloads, "mkstemp", fail)
    with pytest.raises(OSError, match="allocation failure"):
        prepare(artifact)
    assert budget.reserved_bytes == 0 and snapshots == []


@pytest.mark.parametrize("failure", ["source_open", "descriptor_open", "copy_write", "response_init"])
def test_preparation_exit_paths_return_credit_and_close_handles(artifact, snapshots, budget, monkeypatch, failure):
    path, _, _ = artifact
    original_path_open = Path.open
    original_fdopen = os.fdopen

    class FailedWriter:
        def __init__(self, file):
            self.file = file

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.file.close()

        def write(self, content):
            self.file.write(content[:1])
            raise OSError("Synthetic copy_write")

    def source_open(self, *args, **kwargs):
        if self == path and args == ("rb",):
            raise OSError("Synthetic source_open")
        return original_path_open(self, *args, **kwargs)

    def descriptor_open(fd, mode):
        raise OSError("Synthetic descriptor_open")

    def copy_write(fd, mode):
        return FailedWriter(original_fdopen(fd, mode))

    def response_init(*args, **kwargs):
        raise OSError("Synthetic response_init")

    if failure == "source_open":
        monkeypatch.setattr(Path, "open", source_open)
    elif failure == "descriptor_open":
        monkeypatch.setattr(analysis_downloads.os, "fdopen", descriptor_open)
    elif failure == "copy_write":
        monkeypatch.setattr(analysis_downloads.os, "fdopen", copy_write)
    else:
        monkeypatch.setattr(analysis_downloads, "_VerifiedFileResponse", response_init)
    with pytest.raises(OSError, match=f"Synthetic {failure}"):
        prepare(artifact)
    assert budget.reserved_bytes == 0
    assert snapshots and all(not path.exists() for path in snapshots)


def test_checksum_failure_returns_credit(artifact, snapshots, budget):
    path, _, _ = artifact
    path.write_bytes(b"X" * len(CONTENT))
    with pytest.raises(ValueError, match="differs"):
        prepare(artifact)
    assert budget.reserved_bytes == 0
    assert snapshots and all(not path.exists() for path in snapshots)


def test_ownership_capture_failure_keeps_credit_and_does_not_delete(artifact, snapshots, budget, monkeypatch):
    original_stat = os.fstat
    original_reserve = analysis_downloads.reserve_temporary_copy
    identities, leases = [], []

    def failed_stat(fd):
        # The fixture retains actual creation identity for its own cleanup only.
        identities.append(original_stat(fd))
        raise OSError("Synthetic ownership capture failure")

    def reserve(amount):
        lease = original_reserve(amount)
        leases.append(lease)
        return lease

    with monkeypatch.context() as patch:
        patch.setattr(analysis_downloads.os, "fstat", failed_stat)
        patch.setattr(analysis_downloads, "reserve_temporary_copy", reserve)
        with pytest.raises(OSError, match="ownership capture"):
            prepare(artifact)
    assert snapshots[0].exists() and budget.reserved_bytes == MIB
    # No production auto-cleanup occurred. This test has proof of its own file's
    # creation identity and can explicitly release only that synthetic resource.
    owned = download_budget._OwnedSnapshot(snapshots[0], leases[0], identities[0])
    assert owned.cleanup() and budget.reserved_bytes == 0


def test_failed_unlink_retains_credit_then_owned_retry_releases(artifact, snapshots, budget, monkeypatch):
    response = prepare(artifact)
    snapshot = snapshots[0]
    original_unlink = Path.unlink

    def locked(self, *args, **kwargs):
        if self == snapshot:
            raise PermissionError("Synthetic locked snapshot")
        return original_unlink(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", locked)
        with pytest.raises(PermissionError, match="locked snapshot"):
            finish(response)
    assert snapshot.exists() and budget.reserved_bytes == MIB
    assert response._cleanup.alive  # Failed explicit cleanup did not disarm GC retry.
    response._snapshot.cleanup()
    assert not snapshot.exists() and budget.reserved_bytes == 0
    response._snapshot.cleanup()
    assert budget.reserved_bytes == 0


def test_cleanup_identity_guard_preserves_replacement_and_unrelated_files(artifact, snapshots, budget, tmp_path):
    response = prepare(artifact)
    snapshot = snapshots[0]
    original = snapshot.with_suffix(".original-owned")
    snapshot.replace(original)  # Keep old inode alive so a new one cannot be reused.
    snapshot.write_bytes(b"Synthetic replacement owned by this test, not the helper")
    unrelated = tmp_path / "nutrigenomics-verified-output-unrelated.snapshot"
    unrelated.write_bytes(b"preexisting synthetic sentinel")
    with pytest.raises(download_budget.DownloadBudgetUnavailable, match="ownership changed"):
        response._snapshot.cleanup()
    assert snapshot.read_bytes().startswith(b"Synthetic replacement")
    assert unrelated.read_bytes() == b"preexisting synthetic sentinel"
    assert budget.reserved_bytes == MIB
    # Only this fixture removes the replacement it created, then restores the
    # helper's original identity. Production cleanup never deletes that file.
    snapshot.unlink()
    original.replace(snapshot)
    response._snapshot.cleanup()
    assert budget.reserved_bytes == 0
    assert unrelated.exists()


def test_undispatched_finalizer_returns_credit(artifact, snapshots, budget):
    response = prepare(artifact)
    assert budget.reserved_bytes == MIB
    del response
    gc.collect()
    assert budget.reserved_bytes == 0
    assert all(not path.exists() for path in snapshots)


def test_concurrent_duplicate_cleanup_returns_credit_once(artifact, snapshots, budget):
    response = prepare(artifact)
    with ThreadPoolExecutor(max_workers=8) as workers:
        assert all(workers.map(lambda _: response._snapshot.cleanup(), range(8)))
    assert budget.reserved_bytes == 0
    assert all(not path.exists() for path in snapshots)


def test_lowered_config_blocks_new_admission_without_evicting_existing(budget, monkeypatch):
    first, second = budget.reserve(0), budget.reserve(0)
    monkeypatch.setenv(download_budget._BUDGET_ENV, str(MIB))
    with pytest.raises(download_budget.DownloadBudgetUnavailable):
        budget.reserve(0)
    assert budget.reserved_bytes == 2 * MIB
    first.release()
    with pytest.raises(download_budget.DownloadBudgetUnavailable):
        budget.reserve(0)
    second.release()
    final = budget.reserve(0)
    assert budget.reserved_bytes == MIB
    final.release()


def test_waiting_request_reads_reduced_limit_at_locked_admission(budget, monkeypatch):
    held = budget.reserve(0)
    gate, entering = Lock(), Event()
    gate.acquire()

    class ObservedLock:
        def __enter__(self):
            entering.set()
            gate.acquire()

        def __exit__(self, *args):
            gate.release()

    budget._lock = ObservedLock()
    rejected = False
    try:
        with ThreadPoolExecutor(max_workers=1) as worker:
            pending = worker.submit(budget.reserve, 0)
            try:
                assert entering.wait(5)
                monkeypatch.setenv(download_budget._BUDGET_ENV, str(MIB))
            finally:
                gate.release()
            try:
                additional = pending.result(timeout=5)
            except download_budget.DownloadBudgetUnavailable:
                rejected = True
            else:
                additional.release()
    finally:
        held.release()
    assert rejected


def test_inherited_owner_guard_does_not_delete_parent_copy(artifact, snapshots, budget, monkeypatch):
    response = prepare(artifact)
    owner = os.getpid()
    with monkeypatch.context() as patch:
        patch.setattr(download_budget.os, "getpid", lambda: owner + 1)
        assert not response._snapshot.cleanup()
        assert snapshots[0].exists()
        assert budget.reserved_bytes == MIB
    response._snapshot.cleanup()
    assert budget.reserved_bytes == 0


def test_independent_processes_each_have_separate_capacity(budget, monkeypatch):
    monkeypatch.setenv(download_budget._BUDGET_ENV, str(MIB))
    held = budget.reserve(0)  # Parent full; each independent child can still admit.
    module_parent = str(Path(download_budget.__file__).resolve().parent)
    project_parent = str(Path(analysis_downloads.__file__).resolve().parents[1])
    original_project = r"C:\Projects\00_NutriGenomics_auto"
    code = (
        "import json,sys; sys.path.insert(0,sys.argv[1]); import nutrigenomics; "
        "nutrigenomics.__path__.insert(0,sys.argv[2]); from nutrigenomics import download_budget as d; "
        "lease=d.reserve_temporary_copy(0); "
        "print(json.dumps({'pid':lease.owner_pid,'reserved':d._PROCESS_BUDGET.reserved_bytes})); lease.release()"
    )

    def child(_):
        result = subprocess.run([sys.executable, "-B", "-c", code, original_project, module_parent],
                                cwd=project_parent, capture_output=True, check=True, timeout=10)
        return json.loads(result.stdout)

    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(child, range(2)))
        assert len({row["pid"] for row in results}) == 2
        assert all(row["reserved"] == MIB for row in results)
        assert budget.reserved_bytes == MIB
    finally:
        held.release()
