"""Rehearse immutable wrapper/code/release restore on an isolated loopback listener.

Copies the two supplied service wrappers into the operator output and overrides
only their ports and receipt/profile destinations. Production/staging processes,
original wrappers, releases and shared pointers are never modified or signalled.
Each probe retains the wrapper's full startup checksum verification.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import socket
import subprocess
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from nutrigenomics.source_updates import atomic_json
from scripts.benchmark_explorer_performance import digest

PROTECTED_PORTS = {8040, 8044, 8058, 8059}


def probe_wrapper(text, port, profile, receipt):
    if port in PROTECTED_PORTS or not 1024 <= port <= 65535:
        raise ValueError("Probe port overlaps a protected listener or is outside the permitted range")
    matches = re.findall(r"\bport=(8040|8044|8058|8059)\b", text)
    if not matches or "scripts.serve_verified_release" not in text:
        raise ValueError("Unrecognized verified service wrapper")
    updated = re.sub(r"\bport=(?:8040|8044|8058|8059)\b", "port=" + str(port), text)
    updated, profiles = re.subn(r"(?m)^export NUTRIGENOMICS_EXPLORER_PROFILE=.*$",
        "export NUTRIGENOMICS_EXPLORER_PROFILE=" + shlex.quote(str(profile)), updated)
    updated, receipts = re.subn(r"--receipt\s+\S+", "--receipt " + shlex.quote(str(receipt)), updated)
    if profiles != 1 or receipts != 1 or re.search(r"\bport=(?:8040|8044|8058|8059)\b", updated):
        raise ValueError("Probe wrapper did not replace exactly its profile/receipt and all protected bindings")
    return updated


def process_identity(pid):
    root = Path("/proc") / str(pid)
    try:
        suffix = (root / "stat").read_text().rsplit(") ", 1)[1].split()
        return {"pid": pid, "start_ticks": suffix[19], "command_sha256": hashlib.sha256((root / "cmdline").read_bytes()).hexdigest()}
    except (OSError, IndexError):
        return {"pid": pid, "unavailable": True}


def get_json(url, timeout):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.load(response)


def verify(scope, current_wrapper, previous_wrapper, current_release, previous_release, output,
           protected_pids=(), readiness_timeout=900):
    if scope not in {"internal", "public"}:
        raise ValueError("Unknown service scope")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    wrappers = {"current": Path(current_wrapper), "previous": Path(previous_wrapper)}
    releases = {"current": Path(current_release), "previous": Path(previous_release)}
    manifests = {key: json.loads((value / "manifest.json").read_text(encoding="utf-8")) for key, value in releases.items()}
    original_hashes = {key: hashlib.sha256(value.read_bytes()).hexdigest() for key, value in wrappers.items()}
    before = [process_identity(pid) for pid in protected_pids]
    report = {"verified_at": datetime.now(UTC).isoformat(), "scope": scope, "protected_listeners": sorted(PROTECTED_PORTS),
        "protected_processes_before": before, "original_wrapper_sha256": original_hashes, "steps": [], "passed": False,
        "method": "Three separate loopback probe processes retain original full startup checksums; existing listeners and release pointers unchanged"}
    atomic_json(output / "restore-validation.json", report)
    try:
        for label, version in (("current_before", "current"), ("previous_code_and_release", "previous"), ("current_restored", "current")):
            with socket.socket() as reserve:
                reserve.bind(("127.0.0.1", 0))
                port = reserve.getsockname()[1]
            if port in PROTECTED_PORTS:
                raise ValueError("Ephemeral probe unexpectedly overlaps a protected port")
            startup = output / (label + "-startup.json")
            wrapper = output / (label + ".sh")
            wrapper.write_text(probe_wrapper(wrappers[version].read_text(), port,
                output / (label + ".profile.jsonl"), startup), encoding="utf-8")
            step = {"step": label, "port": port, "original_wrapper_sha256": original_hashes[version],
                    "probe_wrapper_sha256": hashlib.sha256(wrapper.read_bytes()).hexdigest(),
                    "expected_release_id": manifests[version]["release_id"]}
            report["steps"].append(step)
            started = time.monotonic()
            with (output / (label + ".http.log")).open("xb") as log:
                arguments = ["bash", str(wrapper), scope] + (["staging"] if version == "current" else [])
                job = subprocess.Popen(arguments, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
            step["probe_pid"] = job.pid
            try:
                if job.pid in protected_pids:
                    raise ValueError("Probe identity overlaps a protected process")
                base = "http://127.0.0.1:" + str(port)
                while time.monotonic() - started < readiness_timeout:
                    if job.poll() is not None:
                        raise RuntimeError("Own probe process exited before readiness; inspect its scoped log")
                    try:
                        meta = get_json(base + "/api/v1/explorer/meta", 2)
                        break
                    except OSError:
                        time.sleep(.25)
                else:
                    raise TimeoutError("Probe startup checksum/readiness exceeded the recorded operator wait budget")
                step["startup_and_readiness_seconds"] = time.monotonic() - started
                receipt = json.loads(startup.read_text())
                if meta.get("release_id") != manifests[version]["release_id"] or receipt["database_sha256"] != manifests[version]["database_sha256"]:
                    raise ValueError("Probe release or full startup checksum mismatch")
                graph = get_json(base + "/api/v1/explorer/graph?seed_id=NCBIGene%3A348", 15)
                if graph.get("release_id") != meta["release_id"] or not graph.get("nodes"):
                    raise ValueError("Probe graph release differs or has no seed")
                step.update(release_id=meta["release_id"], startup_checksum=receipt, startup_receipt_sha256=hashlib.sha256(startup.read_bytes()).hexdigest(),
                    graph_sha256=digest(graph), nodes=len(graph["nodes"]), edges=len(graph["edges"]), passed=True)
            finally:
                job.terminate()
                try:
                    job.wait(timeout=10)
                    step["forced_probe_kill"] = False
                except subprocess.TimeoutExpired:
                    job.kill();job.wait(timeout=10)
                    step["forced_probe_kill"] = True
                step["probe_returncode_after_shutdown"] = job.returncode
                atomic_json(output / "restore-validation.json", report)
        report["restored_graph_identical"] = report["steps"][0]["graph_sha256"] == report["steps"][2]["graph_sha256"]
        report["passed"] = report["restored_graph_identical"] and all(row["passed"] for row in report["steps"])
    except Exception as error:
        report.update(error_type=type(error).__name__, error=str(error))
    report["protected_processes_after"] = [process_identity(pid) for pid in protected_pids]
    report["protected_processes_unchanged"] = report["protected_processes_after"] == before
    report["original_wrappers_unchanged"] = all(hashlib.sha256(value.read_bytes()).hexdigest() == original_hashes[key] for key, value in wrappers.items())
    report["passed"] = report["passed"] and report["protected_processes_unchanged"] and report["original_wrappers_unchanged"]
    atomic_json(output / "restore-validation.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", choices=("internal", "public"), required=True)
    for name in ("current-wrapper", "previous-wrapper", "current-release", "previous-release", "output"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--protected-pid", action="append", type=int, default=[])
    args = parser.parse_args()
    result = verify(args.scope, args.current_wrapper, args.previous_wrapper, args.current_release, args.previous_release, args.output, args.protected_pid)
    print(json.dumps({"passed": result["passed"], "steps": len(result["steps"]), "error": result.get("error")}))
    raise SystemExit(0 if result["passed"] else 1)
