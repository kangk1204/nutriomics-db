"""At most three fixed processed sample files, not complete cohorts or raw reads."""
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from nutrigenomics.expansion import fetch
from scripts.import_continuation import records
ROOT=Path('research/2026-09-30/expansion/continuation')


def main():
    manifest=ROOT/'subset-manifest.json'
    if manifest.exists():raise ValueError('Existing subset preserved')
    rows=[]
    for acc,limit in [('GSE323980',2),('GSE46761',1)]:
        samples=records((ROOT/(acc+'-samples.txt')).read_text())[:limit]
        for sample in samples:
            urls=[v for k,values in sample['fields'].items() if k.startswith('supplementary_file') for v in values]
            url=urls[0].replace('ftp://','https://',1)
            name=url.rsplit('/',1)[1]
            assert name.startswith(sample['accession']+'_') and name.endswith('.gz')
            row=fetch(url,ROOT/name,max_bytes=2_000_000)
            row.update(file=name,study=acc,accession=sample['accession'])
            rows.append(row)
            print(name,row['bytes'])
    manifest.write_text(json.dumps(rows,indent=2),encoding='utf-8')


if __name__=='__main__':main()
