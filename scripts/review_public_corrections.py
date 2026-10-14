"""Capture two known errata using official PubMed metadata for manual review."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import json

from nutrigenomics.expansion import fetch

root = Path("research/2026-09-30/expansion/corrections")
root.mkdir(parents=True, exist_ok=True)
urls = {
    "hall-errata.xml": "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pubmed&id=31269427,33027677&retmode=xml",
    "hall-2019-correction.html": "https://pmc.ncbi.nlm.nih.gov/articles/PMC7959109/",
}
results = []
for name, url in urls.items():
    try:
        results.append(fetch(url, root / name, 2_000_000))
        print(name, (root / name).read_text()[:1000])
    except (OSError, ValueError) as error:
        print(name, str(error))
(root / "manifest.json").write_text(json.dumps(results, indent=2))
