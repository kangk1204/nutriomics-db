from __future__ import annotations

import sqlite3

from scripts.restore_geo_download_flags import restore_download_flags


def _create_series_database(path, flags: dict[str, bool]) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE geo_series (accession TEXT PRIMARY KEY, assay_data_downloaded INTEGER NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO geo_series(accession, assay_data_downloaded) VALUES (?, ?)",
            [(accession, int(flag)) for accession, flag in flags.items()],
        )


def test_restore_download_flags_from_snapshot(tmp_path) -> None:
    database = tmp_path / "current.sqlite3"
    snapshot = tmp_path / "snapshot.sqlite3"
    _create_series_database(database, {"GSE1": False, "GSE2": False, "GSE3": True})
    _create_series_database(snapshot, {"GSE1": True, "GSE2": False, "GSE3": True})

    result = restore_download_flags(database, snapshot)

    assert result == {"matched_series": 3, "restored_flags": 1}
    with sqlite3.connect(database) as connection:
        rows = dict(connection.execute("SELECT accession, assay_data_downloaded FROM geo_series"))
    assert rows == {"GSE1": 1, "GSE2": 0, "GSE3": 1}
