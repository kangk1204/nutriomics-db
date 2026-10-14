from __future__ import annotations

import sqlite3

import pytest

from scripts.backup_sqlite import backup_database


def test_backup_database_creates_integrity_checked_snapshot(tmp_path) -> None:
    source = tmp_path / "source.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, label TEXT NOT NULL)")
        connection.execute("INSERT INTO records(label) VALUES ('preserved')")
    destination = tmp_path / "backups" / "snapshot.sqlite3"

    result = backup_database(source, destination)

    assert result == destination.resolve()
    with sqlite3.connect(destination) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT label FROM records").fetchone() == ("preserved",)
    with pytest.raises(FileExistsError):
        backup_database(source, destination)
