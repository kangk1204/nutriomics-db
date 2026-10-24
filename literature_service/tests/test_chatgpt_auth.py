"""Synthetic OAuth transactions: no real account or OpenAI HTTP calls."""

import base64
import concurrent.futures
import hashlib
import json
import os
from types import SimpleNamespace
import time
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from nutri_literature import chatgpt_auth as auth


class Response:
    def __init__(self, content, status=200):
        self.content = content
        self.status_code = status

    def json(self):
        return self.content

    def raise_for_status(self):
        if self.status_code != 200:
            raise requests.HTTPError("synthetic sensitive HTTP body")


@pytest.fixture
def signed(tmp_path):
    jwt = pytest.importorskip("jwt")
    rsa = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.rsa")
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    posts = []
    responses = []

    def post(url, **kwargs):
        posts.append((url, kwargs))
        if not responses:
            raise AssertionError("Unexpected OAuth request")
        return responses.pop(0)

    client = auth.ChatGPTAuth(tmp_path / "protected", session=SimpleNamespace(post=post))
    client._jwks = SimpleNamespace(get_signing_key_from_jwt=lambda token: SimpleNamespace(key=private.public_key()))

    def tokens(attempt, *, subject="account-a", client_id="oaiapp_synthetic_a", scopes=auth.SCOPES,
               nonce=None, expires=3600, signing_key=None, audience=None, issuer=auth.ISSUER):
        claims = {"sub": subject, "aud": audience or client_id, "iss": issuer,
                  "iat": int(time.time()), "exp": int(time.time()) + expires,
                  "nonce": attempt._nonce if nonce is None else nonce,
                  "email": "same-email@example.invalid", "name": "Synthetic researcher"}
        return {"access_token": "synthetic-access-" + subject,
                "refresh_token": "synthetic-refresh-" + subject,
                "id_token": jwt.encode(claims, signing_key or private, algorithm="RS256"),
                "scope": scopes, "expires_in": 3600, "token_type": "Bearer"}

    return SimpleNamespace(client=client, responses=responses, posts=posts, tokens=tokens, private=private)


def callback(attempt, client_id="oaiapp_synthetic_a", **changes):
    result = {"state": [attempt._state], "code": ["synthetic-code"], "client_id": [client_id]}
    result.update(changes)
    return result


def connect(signed, *, subject="account-a", client_id="oaiapp_synthetic_a", **token_args):
    attempt = signed.client.start_sign_in(open_browser=False)
    signed.responses.append(Response(signed.tokens(attempt, subject=subject, client_id=client_id, **token_args)))
    signed.client._complete(attempt, callback(attempt, client_id))
    return attempt.wait(1)


def test_new_account_uses_independent_client_pkce_and_stable_host(signed):
    first = signed.client.start_sign_in(open_browser=False)
    second = signed.client.start_sign_in(open_browser=False)
    try:
        a, b = [parse_qs(urlsplit(item.authorization_url).query) for item in (first, second)]
        assert a["client_id"] == ["dynamic_agent_client"]
        assert a["agent_name_hint"] == [auth.APP_NAME]
        assert "id_token_hint" not in a and "login_hint" not in a
        assert a["ext_agent_host_id"] == b["ext_agent_host_id"]
        assert a["ext_agent_host_id"][0].startswith("urn:uuid:")
        assert a["nonce"] != b["nonce"] and a["state"] != b["state"]
        expected = base64.urlsafe_b64encode(hashlib.sha256(first._verifier.encode()).digest()).rstrip(b"=").decode()
        assert a["code_challenge"] == [expected]
        assert a["code_challenge_method"] == ["S256"]
        assert first.redirect_uri.startswith("http://127.0.0.1:")
        assert first.redirect_uri.endswith("/auth/callback")
        assert "http" not in repr(first) and first._state not in repr(first)
        again = auth.ChatGPTAuth(signed.client.store_dir)
        with again._store.locked():
            assert again._store.read()["host_id"] == a["ext_agent_host_id"][0]
    finally:
        first.cancel()
        second.cancel()


def test_state_mismatch_cannot_exchange_and_valid_callback_remains_usable(signed):
    attempt = signed.client.start_sign_in(open_browser=False)
    with pytest.raises(auth.ChatGPTAuthError, match="state"):
        signed.client._complete(attempt, callback(attempt, state=["forged-state"]))
    assert not signed.posts and not attempt._done.is_set()
    signed.responses.append(Response(signed.tokens(attempt)))
    signed.client._complete(attempt, callback(attempt))
    assert attempt.wait(1)["signed_in"]


@pytest.mark.parametrize("changes", [{"nonce": "wrong"}, {"audience": "wrong"},
                                   {"issuer": "https://example.invalid"}, {"expires": -5}])
def test_rejects_invalid_signed_identity_claims(signed, changes):
    attempt = signed.client.start_sign_in(open_browser=False)
    signed.responses.append(Response(signed.tokens(attempt, **changes)))
    with pytest.raises(auth.ChatGPTAuthError, match="identity validation"):
        signed.client._complete(attempt, callback(attempt))
    assert signed.client.status()["accounts"] == []


def test_signature_is_verified_not_only_jwt_payload(signed):
    rsa = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.rsa")
    forged = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    attempt = signed.client.start_sign_in(open_browser=False)
    signed.responses.append(Response(signed.tokens(attempt, signing_key=forged)))
    with pytest.raises(auth.ChatGPTAuthError, match="identity validation"):
        signed.client._complete(attempt, callback(attempt))
    assert not signed.client.status()["accounts"]


def test_callback_missing_or_wrong_client_is_not_exchanged(signed):
    for value in (None, "dynamic_agent_client"):
        attempt = signed.client.start_sign_in(open_browser=False)
        with pytest.raises(auth.ChatGPTAuthError):
            signed.client._complete(attempt, callback(attempt, client_id=[value]))
    assert not signed.posts


def test_permission_denial_and_duplicate_callback_parameters_are_rejected(signed):
    for changes in ({"error": ["access_denied"]}, {"code": ["a", "b"]}):
        attempt = signed.client.start_sign_in(open_browser=False)
        with pytest.raises(auth.ChatGPTAuthError):
            signed.client._complete(attempt, callback(attempt, **changes))
    assert not signed.posts


def test_same_email_accounts_remain_separate_and_new_signin_has_no_inherited_hints(signed):
    first = connect(signed)
    pending = signed.client.start_sign_in(open_browser=False)
    query = parse_qs(urlsplit(pending.authorization_url).query)
    assert query["client_id"] == ["dynamic_agent_client"]
    assert "id_token_hint" not in query and "login_hint" not in query
    assert signed.client.status()["active_registration_id"] == first["registration_id"]
    signed.responses.append(Response(signed.tokens(pending, subject="account-b", client_id="oaiapp_synthetic_b")))
    signed.client._complete(pending, callback(pending, "oaiapp_synthetic_b"))
    second = pending.wait(1)
    assert first["registration_id"] != second["registration_id"]
    assert first["label"] != second["label"]
    assert len(signed.client.status()["accounts"]) == 2
    assert signed.client.access_token(first["registration_id"]) == "synthetic-access-account-a"
    assert signed.client.access_token(second["registration_id"]) == "synthetic-access-account-b"
    signed.client.select_account(first["registration_id"])
    assert signed.client.access_token() == "synthetic-access-account-a"


def test_returning_registration_checks_issued_client_and_subject(signed):
    saved = connect(signed)
    attempt = signed.client.start_sign_in(saved["registration_id"], open_browser=False)
    query = parse_qs(urlsplit(attempt.authorization_url).query)
    assert query["client_id"] == ["oaiapp_synthetic_a"] and "agent_name_hint" not in query
    assert "id_token_hint" in query
    signed.responses.append(Response(signed.tokens(attempt, subject="different-account")))
    with pytest.raises(auth.ChatGPTAuthError, match="differs"):
        signed.client._complete(attempt, callback(attempt))
    assert signed.client.access_token() == "synthetic-access-account-a"
    changed = signed.client.start_sign_in(saved["registration_id"], open_browser=False)
    with pytest.raises(auth.ChatGPTAuthError, match="selected account"):
        signed.client._complete(changed, callback(changed, "oaiapp_other"))
    assert len(signed.posts) == 2


def test_granted_scopes_control_plan_access_not_callback_scopes(signed):
    value = connect(signed, scopes="openid email")
    assert value["signed_in"] and not value["plan_usage_enabled"]
    with pytest.raises(auth.ChatGPTAuthError, match="has not granted"):
        signed.client.access_token()


def test_protected_storage_and_public_status_never_expose_credentials(signed):
    saved = connect(signed)
    public = json.dumps(signed.client.status())
    assert saved["email"] in public
    assert "synthetic-access" not in public and "synthetic-refresh" not in public
    assert "id_token" not in public and "client_id" not in public and "sub" not in public
    raw = signed.client._store.path.read_bytes()
    if os.name == "nt":
        assert raw.startswith(b"DPAPI1\n")
        assert b"synthetic-access" not in raw and b"same-email" not in raw
    else:
        assert signed.client._store.path.stat().st_mode & 0o077 == 0
        assert signed.client.store_dir.stat().st_mode & 0o077 == 0


def test_refresh_is_serialized_and_rotation_is_persisted(signed):
    saved = connect(signed)
    with signed.client._store.locked():
        data = signed.client._store.read()
        data["accounts"][saved["registration_id"]]["expires_at"] = 1
        signed.client._store.write(data)
    signed.responses.append(Response({"access_token": "synthetic-rotated", "refresh_token": "new-refresh",
                                      "expires_in": 3600, "token_type": "Bearer"}))
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda _: signed.client.access_token(), range(2))) == ["synthetic-rotated"] * 2
    assert len(signed.posts) == 2
    fields = signed.posts[-1][1]["data"]
    assert fields["grant_type"] == "refresh_token" and fields["client_id"] == "oaiapp_synthetic_a"
    assert "scope" not in fields
    fresh_instance = auth.ChatGPTAuth(signed.client.store_dir)
    assert fresh_instance.access_token() == "synthetic-rotated"
    with fresh_instance._store.locked():
        assert fresh_instance._store.read()["accounts"][saved["registration_id"]]["refresh_token"] == "new-refresh"


def test_refresh_failure_is_sanitized_and_preserves_other_account(signed):
    first = connect(signed)
    second = connect(signed, subject="account-b", client_id="oaiapp_synthetic_b")
    with signed.client._store.locked():
        data = signed.client._store.read()
        data["accounts"][first["registration_id"]]["expires_at"] = 1
        signed.client._store.write(data)
    signed.responses.append(Response({"error": "SECRET"}, status=400))
    with pytest.raises(auth.ChatGPTAuthError) as caught:
        signed.client.access_token(first["registration_id"])
    assert "SECRET" not in str(caught.value)
    assert signed.client.access_token(second["registration_id"]) == "synthetic-access-account-b"


def test_callback_is_local_sanitized_no_query_logging_and_one_time(signed, capsys):
    attempt = signed.client.start_sign_in(open_browser=False)
    signed.responses.append(Response(signed.tokens(attempt)))
    response = requests.get(attempt.redirect_uri, params={key: value[0] for key, value in callback(attempt).items()}, timeout=5)
    assert response.status_code == 200 and "connected" in response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert "synthetic" not in response.text and attempt._state not in response.text
    assert capsys.readouterr().err == ""
    with pytest.raises(auth.ChatGPTAuthError, match="no longer active"):
        signed.client._complete(attempt, callback(attempt))
    assert len(signed.posts) == 1


def test_signout_retains_registration_and_host_but_clears_tokens(signed):
    saved = connect(signed)
    signed.client._http.get = lambda *args, **kwargs: Response({"issuer": auth.ISSUER,
                                   "revocation_endpoint": auth.ISSUER + "/oauth/revoke"})
    signed.responses.append(Response(None))
    result = signed.client.sign_out()
    assert result["remote_revocation_confirmed"]
    fields = signed.posts[-1][1]["data"]
    assert fields["token_type_hint"] == "refresh_token" and fields["client_id"] == "oaiapp_synthetic_a"
    status = signed.client.status()
    assert status["active_registration_id"] is None and not status["accounts"][0]["signed_in"]
    with signed.client._store.locked():
        data = signed.client._store.read()
        assert data["host_id"].startswith("urn:uuid:")
        assert data["accounts"][saved["registration_id"]]["client_id"] == "oaiapp_synthetic_a"
        assert all(key not in data["accounts"][saved["registration_id"]]
                   for key in ("access_token", "refresh_token", "id_token"))


def test_untrusted_revocation_url_never_receives_tokens_and_remote_failure_reported(signed):
    connect(signed)
    signed.client._http.get = lambda *args, **kwargs: Response({"issuer": auth.ISSUER,
                                    "revocation_endpoint": "https://example.invalid/steal"})
    result = signed.client.sign_out()
    assert not result["remote_revocation_confirmed"] and len(signed.posts) == 1
    assert not signed.client.status()["accounts"][0]["signed_in"]


def test_missing_local_credentials_never_reads_codex_or_environment_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-use-api-key")
    monkeypatch.setenv("ACCESS_TOKEN", "do-not-use-token")
    directory = tmp_path / "isolated"
    client = auth.ChatGPTAuth(directory)
    assert client.status() == {"active_registration_id": None, "accounts": [], "pending_registration_ids": []}
    with pytest.raises(auth.ChatGPTAuthError, match="Connect a ChatGPT account"):
        client.access_token()
    assert client.store_dir == directory.resolve()


def test_expired_signin_cannot_exchange_and_callback_rejects_wrong_host(signed):
    attempt = signed.client.start_sign_in(open_browser=False)
    response = requests.get(attempt.redirect_uri, headers={"Host": "evil.invalid"}, timeout=5)
    assert response.status_code == 400 and not signed.posts
    attempt._expires = time.monotonic() - 1
    with pytest.raises(auth.ChatGPTAuthError, match="no longer active"):
        signed.client._complete(attempt, callback(attempt))
    attempt.cancel()


def test_plaintext_credential_file_is_rejected_on_windows(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows DPAPI boundary")
    directory = tmp_path / "protected"
    directory.mkdir()
    (directory / "accounts.protected").write_text('{"access_token": "synthetic"}')
    with pytest.raises(auth.ChatGPTAuthError, match="cannot be read"):
        auth.ChatGPTAuth(directory)


def test_issued_client_survives_failed_exchange_and_retries_without_claiming_identity(signed):
    existing = connect(signed)
    attempt = signed.client.start_sign_in(open_browser=False)
    signed.responses.append(Response({"error": "invalid_grant", "secret": "must-not-persist"}, status=400))
    with pytest.raises(auth.ChatGPTAuthError, match="rejected"):
        signed.client._complete(attempt, callback(attempt, "oaiapp_retry"))
    pending_id = attempt.pending_registration_id
    assert pending_id.startswith("pending:")
    status = signed.client.status()
    assert status["active_registration_id"] == existing["registration_id"]
    assert len(status["accounts"]) == 1 and status["pending_registration_ids"] == [pending_id]
    assert "oaiapp_retry" not in json.dumps(status)
    assert signed.client.access_token() == "synthetic-access-account-a"
    with pytest.raises(auth.ChatGPTAuthError, match="Connect a ChatGPT account"):
        signed.client.select_account(pending_id)
    with pytest.raises(auth.ChatGPTAuthError, match="Connect a ChatGPT account"):
        signed.client.access_token(pending_id)
    with signed.client._store.locked():
        pending = signed.client._store.read()["pending_registrations"][pending_id]
        assert set(pending) == {"client_id", "created_at"}
        assert pending["client_id"] == "oaiapp_retry"
    reloaded = auth.ChatGPTAuth(signed.client.store_dir, session=signed.client._http)
    reloaded._jwks = signed.client._jwks
    retry = reloaded.start_sign_in(pending_id, open_browser=False)
    query = parse_qs(urlsplit(retry.authorization_url).query)
    assert query["client_id"] == ["oaiapp_retry"]
    assert "agent_name_hint" not in query and "id_token_hint" not in query and "login_hint" not in query
    assert retry._nonce != attempt._nonce and retry._state != attempt._state
    signed.responses.append(Response(signed.tokens(retry, subject="account-b", client_id="oaiapp_retry")))
    # Returning OAuth is allowed to omit client_id; the pending issued value is reused.
    reloaded._complete(retry, {"state": [retry._state], "code": ["new-code"]})
    connected = retry.wait(1)
    assert connected["registration_id"] != pending_id
    assert len(reloaded.status()["accounts"]) == 2 and reloaded.status()["pending_registration_ids"] == []
    assert signed.posts[-1][1]["data"]["client_id"] == "oaiapp_retry"
    assert reloaded.access_token() == "synthetic-access-account-b"


def test_pending_retry_rejects_replaced_client_and_forged_identity_without_activation(signed):
    existing = connect(signed)
    attempt = signed.client.start_sign_in(open_browser=False)
    signed.responses.append(Response({}, status=400))
    with pytest.raises(auth.ChatGPTAuthError):
        signed.client._complete(attempt, callback(attempt, "oaiapp_retry"))
    pending_id = attempt.pending_registration_id
    retry = signed.client.start_sign_in(pending_id, open_browser=False)
    with pytest.raises(auth.ChatGPTAuthError, match="selected account"):
        signed.client._complete(retry, callback(retry, "oaiapp_replaced"))
    assert len(signed.posts) == 2
    retry = signed.client.start_sign_in(pending_id, open_browser=False)
    signed.responses.append(Response(signed.tokens(retry, client_id="oaiapp_retry", nonce="forged")))
    with pytest.raises(auth.ChatGPTAuthError, match="identity validation"):
        signed.client._complete(retry, callback(retry, "oaiapp_retry"))
    assert signed.client.status()["active_registration_id"] == existing["registration_id"]
    assert len(signed.client.status()["accounts"]) == 1
    assert signed.client.status()["pending_registration_ids"] == [pending_id]


def test_store_without_pending_extension_remains_compatible(signed):
    existing = connect(signed)
    with signed.client._store.locked():
        data = signed.client._store.read()
        data.pop("pending_registrations", None)
        signed.client._store.write(data)
    reloaded = auth.ChatGPTAuth(signed.client.store_dir)
    assert reloaded.status()["active_registration_id"] == existing["registration_id"]
    assert reloaded.status()["pending_registration_ids"] == []
    assert reloaded.access_token() == "synthetic-access-account-a"
