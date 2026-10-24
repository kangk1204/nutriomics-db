"""Authentication regression cases; accounts and credentials are synthetic."""

import json

import pytest

from nutri_literature import auth
from nutri_literature.db import connect, initialize
from nutri_literature.settings import Settings


@pytest.fixture
def settings(tmp_path):
    config = tmp_path / "topics.json"
    config.write_text(json.dumps({"topics": []}), encoding="utf-8")
    settings = Settings(tmp_path / "runtime", config)
    initialize(settings)
    return settings


def test_individual_account_hash_and_password_verification(settings):
    auth.create_account(settings, "synthetic_reviewer", "Synthetic password 123")
    assert auth.authenticate(settings, "synthetic_reviewer", "Synthetic password 123")
    assert not auth.authenticate(settings, "synthetic_reviewer", "Wrong password 123")
    assert not auth.authenticate(settings, "missing_reviewer", "Synthetic password 123")
    with connect(settings) as con:
        password_hash = con.execute("SELECT password_hash FROM accounts").fetchone()[0]
    assert password_hash.startswith("scrypt$")
    assert "Synthetic password" not in password_hash
    assert auth.hash_password("Synthetic password 123") != password_hash
    with pytest.raises(ValueError):
        auth.create_account(settings, "synthetic_reviewer", "New password 123")
    with pytest.raises(ValueError):
        auth.create_account(settings, "other", "short")
    with pytest.raises(ValueError):
        auth.create_account(settings, "bad account", "Synthetic password 123")


def test_sessions_are_hashed_expiring_and_revocable(settings):
    auth.create_account(settings, "synthetic_reviewer", "Synthetic password 123")
    session = auth.create_session(settings, "synthetic_reviewer")
    token = session["access_token"]
    assert auth.verify_token(settings, token) == "synthetic_reviewer"
    with connect(settings) as con:
        stored = con.execute("SELECT token_hash FROM sessions").fetchone()[0]
        assert stored == auth.token_hash(token) and stored != token
        con.execute("UPDATE sessions SET expires_at='2025-01-01T00:00:00+00:00'")
    assert auth.verify_token(settings, token) is None
    session = auth.create_session(settings, "synthetic_reviewer")
    auth.revoke_token(settings, session["access_token"])
    assert auth.verify_token(settings, session["access_token"]) is None
    assert auth.verify_token(settings, "forged-token") is None


def test_disabling_an_account_immediately_revokes_access(settings):
    auth.create_account(settings, "synthetic_reviewer", "Synthetic password 123")
    token = auth.create_session(settings, "synthetic_reviewer")["access_token"]
    with connect(settings) as con:
        con.execute("UPDATE accounts SET enabled=0 WHERE username='synthetic_reviewer'")
    assert not auth.authenticate(settings, "synthetic_reviewer", "Synthetic password 123")
    assert auth.verify_token(settings, token) is None
    with pytest.raises(ValueError):
        auth.create_session(settings, "synthetic_reviewer")


def test_password_reset_changes_credentials_and_revokes_all_previous_sessions(settings):
    auth.create_account(settings, "synthetic_reviewer", "Synthetic password 123")
    previous = [auth.create_session(settings, "synthetic_reviewer")["access_token"] for _ in range(2)]
    auth.reset_password(settings, "synthetic_reviewer", "Replacement password 456")
    assert not auth.authenticate(settings, "synthetic_reviewer", "Synthetic password 123")
    assert auth.authenticate(settings, "synthetic_reviewer", "Replacement password 456")
    assert all(auth.verify_token(settings, token) is None for token in previous)
    with connect(settings) as con:
        assert con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    current = auth.create_session(settings, "synthetic_reviewer")["access_token"]
    assert auth.verify_token(settings, current) == "synthetic_reviewer"
    with pytest.raises(ValueError):
        auth.reset_password(settings, "missing_reviewer", "Replacement password 456")
