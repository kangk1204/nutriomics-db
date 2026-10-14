from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path


def restore_download_flags(database: Path, snapshot: Path) -> dict[str, int]:
    database = database.expanduser().resolve()
    snapshot = snapshot.expanduser().resolve()
    if database == snapshot:
        raise ValueError("Live database and snapshot must be different files")
    if not database.is_file():
        raise FileNotFoundError(database)
    if not snapshot.is_file():
        raise FileNotFoundError(snapshot)

    with closing(sqlite3.connect(database)) as connection:
        connection.execute("ATTACH DATABASE ? AS prior_snapshot", (str(snapshot),))
        try:
            matched = connection.execute(
                """
                SELECT COUNT(*)
                FROM main.geo_series AS current
                JOIN prior_snapshot.geo_series AS prior USING (accession)
                """
            ).fetchone()[0]
            changed = connection.execute(
                """
                UPDATE main.geo_series
                SET assay_data_downloaded = (
                    SELECT prior.assay_data_downloaded
                    FROM prior_snapshot.geo_series AS prior
                    WHERE prior.accession = main.geo_series.accession
                )
                WHERE accession IN (
                    SELECT accession FROM prior_snapshot.geo_series
                )
                  AND assay_data_downloaded != (
                    SELECT prior.assay_data_downloaded
                    FROM prior_snapshot.geo_series AS prior
                    WHERE prior.accession = main.geo_series.accession
                )
                """
            ).rowcount
            connection.commit()
            return {"matched_series": matched, "restored_flags": changed}
        finally:
            connection.execute("DETACH DATABASE prior_snapshot")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Restore GEO assay-download flags from a verified SQLite snapshot"
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    args = parser.parse_args()
    print(restore_download_flags(args.database, args.snapshot))


if __name__ == "__main__":
    main()
