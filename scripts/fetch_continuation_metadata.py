"""Capture fixed sample metadata, chemical ontology records, and licensing text."""
import sys
import json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutrigenomics.expansion import ALLOWED_HOSTS, fetch

ROOT = Path('research/2026-09-30/expansion/continuation')
URLS = {
    'uniprot-LICENSE': 'https://ftp.uniprot.org/pub/databases/uniprot/current_release/LICENSE',
    'GSE323980-filelist.txt': 'https://ftp.ncbi.nlm.nih.gov/geo/series/GSE323nnn/GSE323980/suppl/filelist.txt',
    'GSE46761-filelist.txt': 'https://ftp.ncbi.nlm.nih.gov/geo/series/GSE46nnn/GSE46761/suppl/filelist.txt',
    **{f'{acc}-samples.soft': f'https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={acc}&targ=gsm&form=text&view=full'
       for acc in ['GSE323980', 'GSE15284', 'GSE46761']},
    **{f'CHEBI-{cid}.json': f'https://www.ebi.ac.uk/ols4/api/ontologies/chebi/terms?iri=http%3A%2F%2Fpurl.obolibrary.org%2Fobo%2FCHEBI_{cid}'
       for cid in ['38290', '57427']},
}


def main():
    ALLOWED_HOSTS.add('ftp.uniprot.org')
    manifest = ROOT / 'metadata-manifest.json'
    if manifest.exists():
        raise ValueError('Existing captures preserved; replay rather than refetch')
    rows = []
    for name, url in URLS.items():
        try:
            row = fetch(url, ROOT / name, max_bytes=1_000_000)
            row['file'] = name
            rows.append(row)
            print(name, row['bytes'])
        except (OSError, ValueError) as error:
            rows.append({'file': name, 'url': url, 'error': str(error)})
            print(name, error)
    manifest.write_text(json.dumps(rows, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
