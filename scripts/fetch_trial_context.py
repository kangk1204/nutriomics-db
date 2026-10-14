"""Bounded registry, NCBI license metadata and biochemical identities."""
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from nutrigenomics.expansion import ALLOWED_HOSTS, fetch
ROOT=Path('research/2026-09-30/expansion/context-joins')
URLS={
    'FISHGASTRO-NCT00145015.json':'https://clinicaltrials.gov/api/v2/studies/NCT00145015',
    'fish-oil-OA-license.xml':'https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi?id=PMC9286410',
    'fish-oil-BioC.xml':'https://www.ncbi.nlm.nih.gov/research/bionlp/RESTful/pmcoa.cgi/BioC_xml/PMC9286410/unicode',
    'PubChem-EPA.json':'https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/eicosapentaenoic%20acid/property/Title,InChIKey,MolecularFormula,IUPACName/JSON',
    'PubChem-DHA.json':'https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/docosahexaenoic%20acid/property/Title,InChIKey,MolecularFormula,IUPACName/JSON',
}


def main():
    ALLOWED_HOSTS.add('clinicaltrials.gov')
    manifest=ROOT/'trial-context-manifest.json'
    if manifest.exists():raise ValueError('Replay existing captures')
    rows=[]
    for name,url in URLS.items():
        try:
            row=fetch(url,ROOT/name,max_bytes=2_000_000)
            row['file']=name;rows.append(row)
            print(name,row['bytes'])
        except (OSError,ValueError) as error:
            rows.append({'file':name,'url':url,'error':str(error)});print(name,error)
    manifest.write_text(json.dumps(rows,indent=2),encoding='utf-8')


if __name__=='__main__':main()
