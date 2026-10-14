"""Fixed bounded searches for existing study publications and source definitions."""
import json
import sys
import urllib.parse
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from nutrigenomics.expansion import fetch
ROOT=Path('research/2026-09-30/expansion/context-joins')
QUERIES={
    'FISHGASTRO': 'FISHGASTRO',
    'fish-oil-fenofibrate': 'Bragt[Author] AND (fenofibrate OR fish oil)',
    'ketogenic-sleep': '"Diet Composition Shapes Sleep"',
}


def main():
    ROOT.mkdir(parents=True,exist_ok=True)
    manifest=ROOT/'discovery-manifest.json'
    if manifest.exists():raise ValueError('Preserve existing snapshots; replay offline')
    rows=[]
    for name,query in QUERIES.items():
        url='https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&retmode=json&retmax=8&term='+urllib.parse.quote(query)
        row=fetch(url,ROOT/(name+'-search.json'),max_bytes=500_000)
        row['file']=name+'-search.json';rows.append(row)
        ids=json.loads((ROOT/row['file']).read_text())['esearchresult']['idlist']
        print(name,ids)
        if ids:
            url='https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pubmed&retmode=xml&id='+','.join(ids)
            row=fetch(url,ROOT/(name+'-pubmed.xml'),max_bytes=1_000_000)
            row['file']=name+'-pubmed.xml';rows.append(row)
    manifest.write_text(json.dumps(rows,indent=2),encoding='utf-8')


if __name__=='__main__':main()
