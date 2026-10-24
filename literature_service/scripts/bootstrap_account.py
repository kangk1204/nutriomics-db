"""Create one owner account; keep generated password in owner-only runtime file."""

import os
import secrets
from nutri_literature.settings import Settings
from nutri_literature.db import connect, dumps, initialize
from nutri_literature.auth import create_account

settings = Settings.from_env()
initialize(settings)
with connect(settings) as con:
    exists = con.execute("SELECT 1 FROM accounts WHERE username=?", ("kangk",)).fetchone()
if not exists:
    password = secrets.token_urlsafe(24)
    create_account(settings, "kangk", password)
    private = settings.home / "private"
    private.mkdir(exist_ok=True)
    if os.name != "nt":
        private.chmod(0o700)
    path = private / "reviewer-credentials.json"
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(
            dumps(
                {
                    "username": "kangk",
                    "password": password,
                    "usage": "개인 검수 계정. CLI reset-password kangk로 비밀번호를 변경할 수 있습니다.",
                }
            )
        )
    print("Created reviewer account; credentials in runtime/private/reviewer-credentials.json")
else:
    print("Reviewer account already exists; unchanged")
