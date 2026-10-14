"""Bounded primary-source verification of a pinned repository cohort lead."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutrigenomics.expansion import fetch

ROOT = Path('research/2026-09-30/expansion/cohort-primary/GSE32357')
ROOT.mkdir(parents=True, exist_ok=True)
rows = []
for name, url, bound in [
    ('GSE32357.soft', 'https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE32357&targ=self&form=text&view=full', 200_000),
    ('GSE32357_series_matrix.txt.gz', 'https://ftp.ncbi.nlm.nih.gov/geo/series/GSE32nnn/GSE32357/matrix/GSE32357_series_matrix.txt.gz', 4_000_000)]:
    target = ROOT / name
    if target.exists():
        raise ValueError('Capture already exists; use saved manifest')
    row = fetch(url, target, max_bytes=bound)
    row['file'] = name
    rows.append(row)
    (ROOT / 'manifest.json').write_text(json.dumps(rows, indent=2))
