"""Individual local reviewer accounts and revocable, expiring opaque sessions."""

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import re
import secrets

from .db import connect, now

SESSION_HOURS = 8
_SALT_BYTES = 16
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 16384, 8, 1
_DUMMY_HASH = None


def hash_password(password: str) -> str:
    if not isinstance(password, str) or not 12 <= len(password) <= 1024:
        raise ValueError("비밀번호는 12~1024자로 설정하세요.")
    salt = secrets.token_bytes(_SALT_BYTES)
    result = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=64
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${result.hex()}"


def check_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        if algorithm != "scrypt" or (int(n), int(r), int(p)) != (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P):
            return False
        if len(password) > 1024:
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=64
        )
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, TypeError, AttributeError):
        return False


def create_account(settings, username: str, password: str):
    """Create only; resetting credentials is an explicit, separate operation."""
    if not re.fullmatch(r"[\w.-]{3,64}", username, flags=re.UNICODE):
        raise ValueError("계정 이름은 3~64자의 문자, 숫자, 점, 밑줄, 하이픈을 사용하세요.")
    password_hash = hash_password(password)
    with connect(settings, durable=True) as con:
        if con.execute("SELECT 1 FROM accounts WHERE username=?", (username,)).fetchone():
            raise ValueError("이미 있는 계정입니다.")
        con.execute(
            "INSERT INTO accounts(username,password_hash,enabled,created_at) VALUES(?,?,1,?)",
            (username, password_hash, now()),
        )
    return username


def authenticate(settings, username: str, password: str) -> bool:
    global _DUMMY_HASH
    with connect(settings) as con:
        row = con.execute(
            "SELECT password_hash,enabled FROM accounts WHERE username=?", (username,)
        ).fetchone()
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_urlsafe(32))
    valid = check_password(password, row["password_hash"] if row else _DUMMY_HASH)
    return bool(valid and row and row["enabled"])


def reset_password(settings, username: str, password: str):
    encoded = hash_password(password)
    with connect(settings, durable=True) as con:
        if not con.execute("SELECT 1 FROM accounts WHERE username=?", (username,)).fetchone():
            raise ValueError("계정이 없습니다.")
        con.execute("UPDATE accounts SET password_hash=? WHERE username=?", (encoded, username))
        con.execute("DELETE FROM sessions WHERE username=?", (username,))


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(settings, username: str):
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)).isoformat()
    with connect(settings) as con:
        account = con.execute("SELECT enabled FROM accounts WHERE username=?", (username,)).fetchone()
        if not account or not account["enabled"]:
            raise ValueError("활성 계정이 없습니다.")
        con.execute("DELETE FROM sessions WHERE expires_at <= ?", (now(),))
        con.execute("INSERT INTO sessions VALUES(?,?,?)", (token_hash(token), username, expires_at))
    return {"access_token": token, "token_type": "bearer", "expires_at": expires_at, "username": username}


def verify_token(settings, token: str):
    if not token or len(token) > 256:
        return None
    with connect(settings) as con:
        row = con.execute(
            "SELECT s.username,s.expires_at FROM sessions s JOIN accounts a "
            "ON a.username=s.username WHERE s.token_hash=? AND a.enabled=1",
            (token_hash(token),),
        ).fetchone()
    if row is None:
        return None
    try:
        expires = datetime.fromisoformat(row["expires_at"])
        if expires.tzinfo is None or expires <= datetime.now(timezone.utc):
            return None
    except ValueError:
        return None
    return row["username"]


def revoke_token(settings, token: str):
    with connect(settings) as con:
        con.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(token),))
