from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("NUTRIGENOMICS_DATA_DIR", PROJECT_ROOT / "data")).expanduser()
RESEARCH_DIR = Path(
    os.environ.get("NUTRIGENOMICS_RESEARCH_DIR", PROJECT_ROOT / "research" / "2026-09-29")
).expanduser()
DEFAULT_DATABASE = DATA_DIR / "db" / "nutrigenomics.sqlite3"
DATABASE_URL = os.environ.get("NUTRIGENOMICS_DATABASE_URL", f"sqlite:///{DEFAULT_DATABASE}")

