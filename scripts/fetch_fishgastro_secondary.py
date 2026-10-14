"""One bounded primary publication referenced by the captured trial registry."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutrigenomics.expansion import fetch

ROOT = Path("research/2026-09-30/expansion/context-joins")
url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pubmed&retmode=xml&id=20032491"
target = ROOT / "FISHGASTRO-secondary-pubmed.xml"
if not target.exists():
    row = fetch(url, target, max_bytes=1_000_000)
    row["file"] = target.name
    (ROOT / "secondary-manifest.json").write_text(json.dumps([row], indent=2))
