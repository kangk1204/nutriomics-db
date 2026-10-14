"""Replay every validated local stage without network or production writes."""

from __future__ import annotations

import contextlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutrigenomics.expansion import backup
from scripts.build_expansion_inventory import main as inventory
from scripts.import_public_expansion import replay
from scripts.import_public_outcomes import main as outcomes
from scripts.ingest_public_matrix import main as matrix

root = Path("research/2026-09-30/expansion")
database = root / "nutrition_evidence.sqlite3"
if database.exists():
    backup(
        database, root / ("nutrition_evidence.before-replay-" + str(time.time_ns()) + ".sqlite3")
    )
reports = root / "reports"
reports.mkdir(exist_ok=True)
with (reports / "replay.log").open("w", encoding="utf-8") as log, contextlib.redirect_stdout(log):
    replay(root / "snapshots", database, reports)
    matrix()
    outcomes()
    inventory()
report = json.loads((reports / "coverage.json").read_text())
print(
    json.dumps(
        {
            "tables": report["tables"],
            "evidence_layers": report["evidence_layers"],
            "validation_errors": report["validation_errors"],
            "processed_values": report["processed_matrix"]["total_values"],
        },
        indent=2,
    )
)
