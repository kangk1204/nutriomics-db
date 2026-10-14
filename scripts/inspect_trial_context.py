import xml.etree.ElementTree as ET
import json
from pathlib import Path
root=Path('research/2026-09-30/expansion/context-joins')
document=ET.fromstring((root/'fish-oil-BioC.xml').read_bytes())
for passage in document.findall('.//passage'):
    text=passage.findtext('text') or ''
    if any(word in text.lower() for word in ['gse273','2006-005743','same study','20 overweight','gene expression','1.7','biobank','supporting information','data availability']):
        print(text[:2200])
print('EPA/DHA identities',[json.loads((root/name).read_text()) for name in ['PubChem-EPA.json','PubChem-DHA.json']])
