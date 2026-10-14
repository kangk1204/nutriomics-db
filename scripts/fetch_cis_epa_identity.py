"""Resolve the biological EPA structure by explicit CAS, rejecting name ambiguity."""
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from nutrigenomics.expansion import fetch
root=Path('research/2026-09-30/expansion/context-joins')
name='PubChem-cis-EPA.json'
path=root/name
manifest=root/'cis-EPA-manifest.json'
if manifest.exists():raise ValueError('Existing identity snapshot preserved')
row=fetch('https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/10417-94-4/property/Title,InChIKey,MolecularFormula,IUPACName/JSON',path,max_bytes=100_000)
data=json.loads(path.read_text())['PropertyTable']['Properties']
if len(data)!=1 or '(5Z,8Z,11Z,14Z,17Z)' not in data[0]['IUPACName']:
    raise ValueError('Expected biological all-cis EPA identity')
row['file']=name;manifest.write_text(json.dumps(row,indent=2),encoding='utf-8')
print(data)
