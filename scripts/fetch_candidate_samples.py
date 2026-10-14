"""Capture bounded GEO sample records and one processed matrix; no raw archives."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutrigenomics.expansion import fetch
ROOT = Path('research/2026-09-30/expansion/continuation')


def main():
    manifest = ROOT / 'candidate-manifest.json'
    if manifest.exists():
        raise ValueError('Replay existing candidate captures rather than refetch')
    rows = []
    urls = {f'{acc}-samples.txt': f'https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={acc}&targ=gsm&form=text&view=full'
            for acc in ['GSE323980','GSE15284','GSE46761']}
    urls['GSE15284_series_matrix.txt.gz'] = 'https://ftp.ncbi.nlm.nih.gov/geo/series/GSE15nnn/GSE15284/matrix/GSE15284_series_matrix.txt.gz'
    for name, url in urls.items():
        try:
            row = fetch(url, ROOT/name, max_bytes=10_000_000 if name.endswith('gz') else 3_000_000)
            row['file'] = name
            rows.append(row)
            print(name, row['bytes'])
        except (OSError, ValueError) as error:
            rows.append({'file': name, 'url': url, 'error': str(error)})
            print(name, error)
    manifest.write_text(json.dumps(rows, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
