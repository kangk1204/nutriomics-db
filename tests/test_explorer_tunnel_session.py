import hashlib
import json
import socket

import pytest

from scripts.run_explorer_tunnel import run, ssh_command


def test_strict_explicit_host_key_and_loopback_forward(tmp_path):
    known = tmp_path / "known hosts"
    known.write_text("public-test-host-key")
    sha = hashlib.sha256(known.read_bytes()).hexdigest()
    command = ssh_command(__file__, "research@100.110.101.17", 18122, 8067, known, sha)
    assert "127.0.0.1:18122:127.0.0.1:8067" in command
    assert "StrictHostKeyChecking=yes" in command
    assert "BatchMode=yes" in command
    with pytest.raises(ValueError, match="Pinned"):
        ssh_command(__file__, "research@100.110.101.17", 18122, 8067, known, "wrong-hash")
    with pytest.raises(ValueError, match="destination"):
        ssh_command(__file__, "-oProxyCommand=unexpected", 18122, 8067, known, sha)


def test_existing_listener_is_preserved(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        with pytest.raises(RuntimeError, match="existing listener"):
            run(["never-executed"], tmp_path, listener.getsockname()[1], "test")
        assert listener.fileno() >= 0
    assert json.loads((tmp_path / "status.json").read_text())["status"] == "failed"
    assert not (tmp_path / "session.lock").exists()


def test_stop_marker_prevents_process_start_and_cleans_lock(tmp_path):
    (tmp_path / "STOP").touch()
    run(["never-executed"], tmp_path, 18122, "test")
    assert json.loads((tmp_path / "status.json").read_text())["status"] == "stopped"
    assert not (tmp_path / "session.lock").exists()


def test_existing_session_lock_is_not_removed(tmp_path):
    lock = tmp_path / "session.lock"
    lock.write_text("other-session")
    with pytest.raises(FileExistsError):
        run(["never-executed"], tmp_path, 18122, "test")
    assert lock.read_text() == "other-session"
