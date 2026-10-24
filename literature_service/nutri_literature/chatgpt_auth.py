"""Official, independent ChatGPT plan OAuth for the local literature companion.

No Codex credentials, browser cookies, API keys, or environment tokens are read.
References: https://developers.openai.com/siwc/token-sharing-open-source/sign-in
and https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
import ctypes
from dataclasses import dataclass, field
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit
import uuid
import webbrowser

import requests

ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = ISSUER + "/api/accounts/authorize"
TOKEN_URL = ISSUER + "/api/accounts/oauth/token"
JWKS_URL = ISSUER + "/.well-known/jwks.json"
DISCOVERY_URL = ISSUER + "/.well-known/openid-configuration"
RESOURCE = "https://api.openai.com/v1"
DIRECT_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = "openid profile email offline_access resource.invoke " + DIRECT_SCOPE
APP_NAME = "Nutriomics Literature"


class ChatGPTAuthError(RuntimeError):
    """Safe to display: never includes HTTP bodies, callback URLs, or tokens."""


def default_store_dir() -> Path:
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA")
        if not root:
            raise ChatGPTAuthError("LOCALAPPDATA is unavailable; choose protected local storage.")
        return Path(root) / "NutriomicsLiterature" / "ChatGPT"
    return Path.home() / ".local" / "share" / "nutriomics-literature" / "chatgpt"


def _dpapi(data: bytes, *, decrypt: bool) -> bytes:
    """Encrypt to this Windows user. UI is forbidden; no machine-wide key."""
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    destination = Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        operation = crypt32.CryptUnprotectData
        operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                              ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        arguments = (ctypes.byref(source), None, None, None, None, 1, ctypes.byref(destination))
    else:
        operation = crypt32.CryptProtectData
        operation.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p,
                              ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        arguments = (ctypes.byref(source), "Nutriomics Literature ChatGPT", None,
                     None, None, 1, ctypes.byref(destination))
    operation.restype = wintypes.BOOL
    if not operation(*arguments):
        raise ChatGPTAuthError("Windows protected credential storage failed.")
    try:
        return ctypes.string_at(destination.pbData, destination.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(destination.pbData, ctypes.c_void_p))


class _Store:
    def __init__(self, directory: Path):
        self.directory = directory.expanduser().resolve()
        if self.directory.is_symlink():
            raise ChatGPTAuthError("Credential storage must be a local directory.")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            if self.directory.stat().st_uid != os.getuid():
                raise ChatGPTAuthError("Credential directory belongs to a different user.")
            self.directory.chmod(0o700)
        self.path = self.directory / "accounts.protected"
        self.lock_path = self.directory / "accounts.lock"
        self._mutex = threading.RLock()

    @contextmanager
    def locked(self, timeout: float = 45):
        """Serialize read-refresh-replace across threads and independent processes."""
        with self._mutex:
            if self.lock_path.is_symlink():
                raise ChatGPTAuthError("Invalid credential lock.")
            descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            with os.fdopen(descriptor, "r+b", buffering=0) as handle:
                if not self.lock_path.stat().st_size:
                    handle.write(b"0")
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        handle.seek(0)
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise ChatGPTAuthError("Credential store is busy; retry shortly.") from None
                        time.sleep(0.05)
                try:
                    yield
                finally:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def read(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "host_id": "urn:uuid:" + str(uuid.uuid4()),
                    "active_registration_id": None, "accounts": {}}
        try:
            if self.path.is_symlink():
                raise ValueError("symlink")
            if os.name != "nt":
                stat = self.path.stat()
                if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
                    raise ValueError("permissions")
            content = self.path.read_bytes()
            if os.name == "nt":
                if not content.startswith(b"DPAPI1\n"):
                    raise ValueError("protection")
                content = _dpapi(content[7:], decrypt=True)
            data = json.loads(content)
            if data.get("version") != 1 or not isinstance(data.get("accounts"), dict):
                raise ValueError("schema")
            if not isinstance(data.get("pending_registrations", {}), dict):
                raise ValueError("pending registration schema")
            if not str(data.get("host_id", "")).startswith("urn:uuid:"):
                raise ValueError("host identity")
            return data
        except Exception:
            raise ChatGPTAuthError("Protected ChatGPT credential storage cannot be read.") from None

    def write(self, data: dict) -> None:
        content = json.dumps(data, ensure_ascii=False).encode("utf-8")
        if os.name == "nt":
            content = b"DPAPI1\n" + _dpapi(content, decrypt=False)
        descriptor, temporary = tempfile.mkstemp(prefix=".accounts-", dir=self.directory)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


@dataclass(repr=False)
class SignInAttempt:
    """Short-lived local transaction. Never serialize or log this object."""

    authorization_url: str = field(repr=False)
    redirect_uri: str
    _state: str = field(repr=False)
    _nonce: str = field(repr=False)
    _verifier: str = field(repr=False)
    _registration_id: str | None = None
    _client_id: str | None = field(default=None, repr=False)
    _subject: str | None = field(default=None, repr=False)
    _expires: float = field(default=0, repr=False)
    _done: threading.Event = field(default_factory=threading.Event, repr=False)
    _server: HTTPServer | None = field(default=None, repr=False)
    _result: dict | None = field(default=None, repr=False)
    _error: str | None = field(default=None, repr=False)
    _pending_id: str | None = field(default=None, repr=False)

    def __repr__(self):
        return f"SignInAttempt(completed={self._done.is_set()})"

    @property
    def pending_registration_id(self) -> str | None:
        """Safe retry selector; never a verified identity or authorization credential."""
        return self._pending_id

    def wait(self, timeout: float | None = None) -> dict:
        if not self._done.wait(timeout):
            raise ChatGPTAuthError("ChatGPT sign-in is still waiting for the browser.")
        if self._error:
            raise ChatGPTAuthError(self._error)
        return dict(self._result or {})

    def cancel(self) -> None:
        self._error = "ChatGPT sign-in was cancelled."
        self._done.set()


class ChatGPTAuth:
    def __init__(self, store_dir: Path | str | None = None, *, session=None):
        self._store = _Store(Path(store_dir) if store_dir is not None else default_store_dir())
        self._http = session or requests.Session()
        self._jwks = None
        with self._store.locked():
            state = self._store.read()
            self._store.write(state)  # Persist host identity before first sign-in.

    @property
    def store_dir(self) -> Path:
        return self._store.directory

    def status(self) -> dict:
        with self._store.locked():
            data = self._store.read()
        return {"active_registration_id": data["active_registration_id"],
                "accounts": [self._public(key, value) for key, value in data["accounts"].items()],
                "pending_registration_ids": sorted(data.get("pending_registrations", {}))}

    @staticmethod
    def _public(registration_id: str, record: dict) -> dict:
        return {"registration_id": registration_id, "label": record["label"],
                "email": record.get("email"), "name": record.get("name"),
                "signed_in": bool(record.get("access_token")),
                "plan_usage_enabled": DIRECT_SCOPE in record.get("scopes", []),
                "expires_at": record.get("expires_at")}

    def select_account(self, registration_id: str) -> dict:
        with self._store.locked():
            data = self._store.read()
            record = self._account(data, registration_id)
            if not record.get("access_token"):
                raise ChatGPTAuthError("Sign in to the selected ChatGPT account first.")
            data["active_registration_id"] = registration_id
            self._store.write(data)
        return self._public(registration_id, record)

    @staticmethod
    def _account(data: dict, registration_id: str | None) -> dict:
        key = registration_id or data["active_registration_id"]
        if not key or key not in data["accounts"]:
            raise ChatGPTAuthError("Connect a ChatGPT account in this app first.")
        return data["accounts"][key]

    def start_sign_in(self, registration_id: str | None = None, *, open_browser: bool = True,
                      timeout: float = 600) -> SignInAttempt:
        """None adds a new account. A pending: ID explicitly retries issued registration.

        Pending records contain no trusted identity or credentials and cannot be
        selected for inference. Every retry requires fresh OAuth and verified JWT.
        """
        if not 1 <= timeout <= 1800:
            raise ValueError("Sign-in timeout must be between 1 and 1800 seconds.")
        with self._store.locked():
            data = self._store.read()
            is_pending = bool(registration_id and registration_id.startswith("pending:"))
            if is_pending:
                pending = data.get("pending_registrations", {}).get(registration_id)
                if not pending:
                    raise ChatGPTAuthError("The pending ChatGPT registration is unavailable. Start a new sign-in.")
                record = dict(pending)
            else:
                record = dict(self._account(data, registration_id)) if registration_id else {}
        attempt = SignInAttempt("", "", secrets.token_urlsafe(32), secrets.token_urlsafe(32),
                                secrets.token_urlsafe(64), None if is_pending else registration_id,
                                record.get("client_id"), record.get("sub"), time.monotonic() + timeout)
        attempt._pending_id = registration_id if is_pending else None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass  # The callback query contains authorization credentials.

            def do_GET(self):
                parsed = urlsplit(self.path)
                valid_host = self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"
                code, message = 400, "ChatGPT sign-in could not be completed. Return to the application."
                if parsed.path == "/auth/callback" and valid_host:
                    try:
                        owner._complete(attempt, parse_qs(parsed.query, keep_blank_values=True))
                        code, message = 200, "ChatGPT account connected. You may close this tab."
                    except ChatGPTAuthError:
                        pass
                content = message.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

        try:
            server = HTTPServer(("127.0.0.1", 0), Handler)
        except OSError:
            raise ChatGPTAuthError("Cannot start the local ChatGPT callback listener.") from None
        server.timeout = 0.25
        attempt._server = server
        attempt.redirect_uri = f"http://127.0.0.1:{server.server_port}/auth/callback"
        query = {"client_id": record.get("client_id") or "dynamic_agent_client",
                 "ext_agent_host_id": data["host_id"], "response_type": "code",
                 "redirect_uri": attempt.redirect_uri, "scope": SCOPES, "resource": RESOURCE,
                 "state": attempt._state, "nonce": attempt._nonce, "code_challenge_method": "S256",
                 "code_challenge": base64.urlsafe_b64encode(
                     hashlib.sha256(attempt._verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")}
        if not record:
            query["agent_name_hint"] = APP_NAME
        else:
            if record.get("id_token"):
                query["id_token_hint"] = record["id_token"]
            if record.get("email"):
                query["login_hint"] = record["email"]
        attempt.authorization_url = AUTHORIZE_URL + "?" + urlencode(query)

        def listen():
            try:
                while not attempt._done.is_set() and time.monotonic() < attempt._expires:
                    server.handle_request()
                if not attempt._done.is_set():
                    attempt._error = "ChatGPT sign-in expired. Start a new sign-in."
                    attempt._done.set()
            finally:
                server.server_close()

        threading.Thread(target=listen, name="chatgpt-oauth-loopback", daemon=True).start()
        if open_browser:
            try:
                webbrowser.open(attempt.authorization_url, new=1, autoraise=True)
            except Exception:
                attempt.cancel()
                raise ChatGPTAuthError("The sign-in browser could not be opened.") from None
        return attempt

    def _post_token(self, fields: dict) -> dict:
        try:
            response = self._http.post(TOKEN_URL, data=fields, timeout=30, allow_redirects=False)
            if response.status_code != 200:
                raise ChatGPTAuthError("ChatGPT authorization was rejected; sign in again.")
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError("schema")
            return result
        except ChatGPTAuthError:
            raise
        except Exception:
            raise ChatGPTAuthError("ChatGPT authorization could not be completed. Retry sign-in.") from None

    def _verify_id_token(self, encoded: str, client_id: str, nonce: str | None = None) -> dict:
        try:
            import jwt
            if self._jwks is None:
                self._jwks = jwt.PyJWKClient(JWKS_URL, timeout=15)
            key = self._jwks.get_signing_key_from_jwt(encoded)
            claims = jwt.decode(encoded, key.key, algorithms=["RS256", "ES256"], audience=client_id,
                                issuer=ISSUER, options={"require": ["exp", "iat", "iss", "aud", "sub"]})
            if not isinstance(claims["sub"], str) or not claims["sub"]:
                raise ValueError("subject")
            if nonce is not None and not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
                raise ValueError("nonce")
            if "azp" in claims and claims["azp"] != client_id:
                raise ValueError("authorized party")
            return claims
        except ImportError:
            raise ChatGPTAuthError("Install requirements-chatgpt.txt before connecting ChatGPT.") from None
        except Exception:
            raise ChatGPTAuthError("ChatGPT account identity validation failed; sign in again.") from None

    @staticmethod
    def _credential_fields(tokens: dict, *, old: dict | None = None) -> dict:
        old = old or {}
        if not isinstance(tokens.get("access_token"), str) or not tokens["access_token"]:
            raise ChatGPTAuthError("ChatGPT did not provide an access token.")
        if str(tokens.get("token_type", "Bearer")).lower() != "bearer":
            raise ChatGPTAuthError("ChatGPT returned an unsupported token type.")
        try:
            expires_in = int(tokens["expires_in"])
            if not 0 < expires_in <= 31_536_000:
                raise ValueError("expiry")
        except (KeyError, TypeError, ValueError):
            raise ChatGPTAuthError("ChatGPT returned invalid token expiration metadata.") from None
        scopes = tokens.get("scope")
        if scopes is None and old:
            scopes = " ".join(old.get("scopes", []))
        if not isinstance(scopes, str):
            raise ChatGPTAuthError("ChatGPT did not confirm the granted permissions.")
        return {"access_token": tokens["access_token"],
                "refresh_token": tokens.get("refresh_token", old.get("refresh_token")),
                "id_token": tokens.get("id_token", old.get("id_token")),
                "scopes": scopes.split(), "expires_at": time.time() + expires_in}

    def _complete(self, attempt: SignInAttempt, query: dict[str, list[str]]) -> None:
        if attempt._done.is_set() or time.monotonic() >= attempt._expires:
            raise ChatGPTAuthError("The ChatGPT sign-in transaction is no longer active.")
        values = query.get("state", [])
        if len(values) != 1 or not secrets.compare_digest(values[0], attempt._state):
            raise ChatGPTAuthError("ChatGPT sign-in state did not match.")
        try:
            if any(len(value) != 1 for value in query.values()):
                raise ChatGPTAuthError("ChatGPT returned an ambiguous authorization callback.")
            if "error" in query:
                raise ChatGPTAuthError("ChatGPT sign-in or plan-use permission was declined.")
            client_id = query.get("client_id", [attempt._client_id])[0]
            if not isinstance(client_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{4,256}", client_id):
                raise ChatGPTAuthError("ChatGPT registration did not provide an issued client ID.")
            if client_id == "dynamic_agent_client" or (
                attempt._client_id is not None and client_id != attempt._client_id
            ):
                raise ChatGPTAuthError("ChatGPT registration did not match the selected account.")
            code = query.get("code", [""])[0]
            if not code or len(code) > 8192:
                raise ChatGPTAuthError("ChatGPT did not provide a valid authorization code.")
            if attempt._registration_id is None:
                # Registration precedes token exchange. Keep its issued client
                # for invalid_grant/network recovery without claiming identity,
                # activating an account, or retaining callback/code/PKCE data.
                with self._store.locked():
                    data = self._store.read()
                    pending = data.setdefault("pending_registrations", {})
                    pending_id = attempt._pending_id or "pending:" + str(uuid.uuid4())
                    prior = pending.get(pending_id)
                    if prior and prior["client_id"] != client_id:
                        raise ChatGPTAuthError("The pending ChatGPT registration changed.")
                    pending[pending_id] = {"client_id": client_id,
                                           "created_at": prior["created_at"] if prior else time.time()}
                    self._store.write(data)
                attempt._pending_id, attempt._client_id = pending_id, client_id
            tokens = self._post_token({"grant_type": "authorization_code", "client_id": client_id,
                                       "code": code, "code_verifier": attempt._verifier,
                                       "redirect_uri": attempt.redirect_uri, "resource": RESOURCE})
            claims = self._verify_id_token(tokens.get("id_token", ""), client_id, attempt._nonce)
            if attempt._subject is not None and claims["sub"] != attempt._subject:
                raise ChatGPTAuthError("The signed-in ChatGPT account differs from the selected registration.")
            credentials = self._credential_fields(tokens)
            with self._store.locked():
                data = self._store.read()
                key = attempt._registration_id
                if key:
                    prior = self._account(data, key)
                    if prior["sub"] != claims["sub"] or prior["client_id"] != client_id:
                        raise ChatGPTAuthError("The saved ChatGPT account registration changed.")
                else:
                    key = next((key for key, value in data["accounts"].items()
                                if value["sub"] == claims["sub"] and value["client_id"] == client_id), None)
                    key = key or str(uuid.uuid4())
                record = {"client_id": client_id, "sub": claims["sub"],
                          "issuer": ISSUER, "email": claims.get("email"), "name": claims.get("name"),
                          "label": (claims.get("email") or claims.get("name") or "ChatGPT account")
                                   + " · " + key[:8], **credentials}
                data["accounts"][key] = record
                data["active_registration_id"] = key
                if attempt._pending_id:
                    data.get("pending_registrations", {}).pop(attempt._pending_id, None)
                self._store.write(data)
                attempt._result = self._public(key, record)
        except ChatGPTAuthError as error:
            attempt._error = str(error)
            raise
        except Exception:
            attempt._error = "ChatGPT sign-in could not be completed. Start a new sign-in."
            raise ChatGPTAuthError(attempt._error) from None
        finally:
            attempt._done.set()

    def access_token(self, registration_id: str | None = None) -> str:
        """Internal bearer credential; callers must never return it through UI/API/logs."""
        with self._store.locked():
            data = self._store.read()
            record = self._account(data, registration_id)
            if DIRECT_SCOPE not in record.get("scopes", []):
                raise ChatGPTAuthError("This ChatGPT account has not granted plan usage to this app.")
            if not record.get("access_token"):
                raise ChatGPTAuthError("Sign in to this ChatGPT account again.")
            if record.get("expires_at", 0) > time.time() + 90:
                return record["access_token"]
            if not record.get("refresh_token"):
                raise ChatGPTAuthError("ChatGPT authorization expired; sign in again.")
            tokens = self._post_token({"grant_type": "refresh_token", "client_id": record["client_id"],
                                       "refresh_token": record["refresh_token"], "resource": RESOURCE})
            if tokens.get("id_token"):
                claims = self._verify_id_token(tokens["id_token"], record["client_id"])
                if claims["sub"] != record["sub"]:
                    raise ChatGPTAuthError("Refreshed ChatGPT credentials did not match this account.")
            fields = self._credential_fields(tokens, old=record)
            if DIRECT_SCOPE not in fields["scopes"]:
                raise ChatGPTAuthError("ChatGPT plan-use permission is no longer enabled.")
            record.update(fields)
            self._store.write(data)
            return record["access_token"]

    def sign_out(self, registration_id: str | None = None, *, revoke: bool = True) -> dict:
        confirmed = False
        with self._store.locked():
            data = self._store.read()
            key = registration_id or data["active_registration_id"]
            record = self._account(data, key)
            if revoke and record.get("refresh_token"):
                try:
                    discovery = self._http.get(DISCOVERY_URL, timeout=15, allow_redirects=False)
                    discovery.raise_for_status()
                    metadata = discovery.json()
                    endpoint = metadata["revocation_endpoint"]
                    location = urlsplit(endpoint)
                    if (metadata.get("issuer") != ISSUER or location.scheme != "https"
                            or location.hostname != "auth.openai.com" or location.username or location.password):
                        raise ValueError("untrusted revocation endpoint")
                    response = self._http.post(endpoint, data={"token": record["refresh_token"],
                                              "token_type_hint": "refresh_token", "client_id": record["client_id"]},
                                              timeout=15, allow_redirects=False)
                    confirmed = response.status_code == 200
                except Exception:
                    confirmed = False
            for name in ("access_token", "refresh_token", "id_token"):
                record.pop(name, None)
            record["expires_at"] = None
            if data["active_registration_id"] == key:
                data["active_registration_id"] = None
            self._store.write(data)
        return {"signed_out": True, "remote_revocation_confirmed": confirmed,
                "registration_id": key}
