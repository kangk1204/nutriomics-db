"""Keep one approved loopback SSH forward connected. No global settings change.

Create STOP in the state directory to stop this session and its own SSH child.
The state directory is exclusive; an occupied local port is never reclaimed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import time
import urllib.request
from pathlib import Path


def ssh_command(ssh, host, local_port, remote_port, known_hosts, expected_hash):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+@[A-Za-z0-9_.:-]+", host):
        raise ValueError("Invalid explicit SSH destination")
    if not all(isinstance(p, int) and 1 <= p <= 65535 for p in (local_port, remote_port)):
        raise ValueError("Invalid TCP port")
    known_hosts = Path(known_hosts).resolve(strict=True)
    if hashlib.sha256(known_hosts.read_bytes()).hexdigest() != expected_hash:
        raise ValueError("Pinned known-hosts file changed")
    return [str(Path(ssh).resolve(strict=True)), "-N", "-o", "BatchMode=yes", "-o",
            "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=" + str(known_hosts),
            "-o", "ConnectTimeout=10", "-o", "ExitOnForwardFailure=yes", "-o",
            "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2", "-L",
            f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}", host]


def occupied(port):
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) == 0


def health(port, expected_release):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as response:
        payload = json.load(response)
    if payload.get("status") != "ok" or payload.get("release_id") != expected_release:
        raise ValueError("Forward answered with an unexpected release or health status")


def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def run(command, directory, local_port, expected_release, *, max_attempts=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / "session.lock"
    # A stale lock is deliberately not removed automatically: do not compete
    # with another user's running tunnel or infer process ownership from a PID.
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.write(descriptor, str(os.getpid()).encode())
    os.close(descriptor)
    stop = directory / "STOP"
    state = {"supervisor_pid": os.getpid(), "bind": "127.0.0.1", "local_port": local_port,
             "command": command, "expected_release": expected_release, "attempt": 0}
    child = None
    try:
        with (directory / "ssh.log").open("ab") as log:
            while not stop.exists():
                if occupied(local_port):
                    raise RuntimeError("Local port belongs to an existing listener; nothing was stopped")
                state.update(attempt=state["attempt"] + 1, status="connecting", ssh_pid=None)
                atomic_json(directory / "status.json", state)
                child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                state["ssh_pid"] = child.pid
                atomic_json(directory / "status.json", state)
                started = time.monotonic()
                while child.poll() is None and not stop.exists():
                    try:
                        health(local_port, expected_release)
                        state.update(status="ready", last_success_unix=time.time(), error=None)
                    except (OSError, ValueError) as error:
                        state.update(status="connecting" if time.monotonic() - started < 15 else "unavailable", error=str(error))
                    atomic_json(directory / "status.json", state)
                    for _ in range(10):
                        if stop.exists() or child.poll() is not None:
                            break
                        time.sleep(.2)
                if stop.exists():
                    break
                state.update(status="reconnecting", last_exit_code=child.returncode, ssh_pid=None)
                atomic_json(directory / "status.json", state)
                if max_attempts is not None and state["attempt"] >= max_attempts:
                    break
                for _ in range(min(150, 5 * 2 ** min(state["attempt"] - 1, 5))):
                    if stop.exists():
                        break
                    time.sleep(.2)
    except BaseException as error:
        state.update(status="failed", error=str(error))
        atomic_json(directory / "status.json", state)
        raise
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        if state.get("status") != "failed":
            state.update(status="stopped", ssh_pid=None)
            atomic_json(directory / "status.json", state)
        lock.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--local-port", type=int, default=18122)
    parser.add_argument("--remote-port", type=int, required=True)
    parser.add_argument("--known-hosts", type=Path, required=True)
    parser.add_argument("--known-hosts-sha256", required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--state-directory", type=Path, required=True)
    args = parser.parse_args()
    command = ssh_command(args.ssh, args.host, args.local_port, args.remote_port,
                          args.known_hosts, args.known_hosts_sha256)
    run(command, args.state_directory, args.local_port, args.release_id)


if __name__ == "__main__":
    main()
