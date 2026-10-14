"""Apply/revert only ng_* tables with an online SQLite backup first."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutrigenomics.expansion import backup, connect, migrate, rollback

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("action", choices=["up", "down"])
parser.add_argument("--database", type=Path, required=True)
parser.add_argument("--backup", type=Path, required=True)
args = parser.parse_args()
if not args.database.is_file():
    parser.error("database must already exist; use import_public_expansion.py for a new sidecar")
backup(args.database, args.backup)
with connect(args.database) as db:
    (migrate if args.action == "up" else rollback)(db)
print("Extension migration completed; original tables preserved; backup:", args.backup)
