"""Fetch a bounded list of candidates, not evidence or independent cohorts."""

import json
import urllib.request
from pathlib import Path

root = Path("research/2026-09-30/expansion/discovery")
ids = json.loads((root / "geo_trials.txt").read_text())["esearchresult"]["idlist"]
url = (
    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=gds&retmode=json&id="
    + ",".join(ids)
)
with urllib.request.urlopen(url, timeout=30) as response:
    raw = response.read(2_000_001)
if len(raw) > 2_000_000:
    raise ValueError("byte cap")
(root / "trial_summaries.json").write_bytes(raw)
result = json.loads(raw)["result"]
for identifier in ids:
    row = result[identifier]
    print(row.get("accession"), row.get("title"), row.get("n_samples"), row.get("pubmedids"))
    print(row.get("summary", "")[:1800])
