"""Bounded official full-text and rights probes for the clinical primary paper."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutrigenomics.expansion import fetch

ROOT = Path('research/2026-09-30/expansion/cohort-primary/GSE32357')


def main():
    manifest = ROOT / 'quantitative-source-manifest.json'
    if manifest.exists():
        raise ValueError('Saved probe exists; use existing source assessment')
    rows = []
    for name, url in [
        ('PMC3880862-oa-rights.xml', 'https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi?id=PMC3880862'),
        ('PMC3880862-BioC.xml', 'https://www.ncbi.nlm.nih.gov/research/bionlp/RESTful/pmcoa.cgi/BioC_xml/PMC3880862/unicode')]:
        try:
            row = fetch(url, ROOT / name, max_bytes=1_000_000)
            row['file'] = name
        except (OSError, ValueError) as error:
            row = {'file': name, 'url': url, 'error': str(error)}
        rows.append(row)
        manifest.write_text(json.dumps(rows, indent=2))
    print(json.dumps(rows, indent=2))


if __name__ == '__main__':
    main()
