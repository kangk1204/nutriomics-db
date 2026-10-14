from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path


def backup_database(source: Path, destination: Path) -> Path:
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if source == destination:
        raise ValueError("Source and backup destination must be different files")
    if not source.is_file():
        raise FileNotFoundError(source)
    if destination.exists():
        raise FileExistsError(destination)

    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f"{destination.name}.partial")
    if partial.exists():
        raise FileExistsError(partial)
    try:
        with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as original, closing(
            sqlite3.connect(partial)
        ) as snapshot:
            original.backup(snapshot)
            integrity = snapshot.execute("PRAGMA integrity_check").fetchone()
            if integrity != ("ok",):
                raise RuntimeError(f"SQLite backup integrity check failed: {integrity}")
        partial.replace(destination)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description="Create and integrity-check an online SQLite backup")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    result = backup_database(args.source, args.destination)
    print(f"SQLite backup created and integrity-checked: {result} ({result.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
