"""Inspect a few official mechanism endpoints and licenses, no bulk ingestion."""

import json
import urllib.request
from pathlib import Path

root = Path("research/2026-09-30/expansion/mechanisms")
root.mkdir(parents=True, exist_ok=True)
urls = {
    "chembl-molecule.json": "https://www.ebi.ac.uk/chembl/api/data/molecule/CHEMBL196.json",
    "chembl-license.txt": "https://ftp.ebi.ac.uk/pub/databases/chembl/ChEMBLdb/latest/LICENSE",
    "chembl-about.html": "https://www.ebi.ac.uk/chembl/about",
    "chembl-license-doc.html": "https://chembl.gitbook.io/chembl-interface-documentation/about/licenses",
}
for name, url in urls.items():
    try:
        with urllib.request.urlopen(url, timeout=25) as response:
            raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError("byte cap")
        (root / name).write_bytes(raw)
        if name.endswith(".json"):
            print(json.loads(raw).get("pref_name"), json.loads(raw).get("molecule_structures"))
        else:
            print(name, raw.decode()[:1500])
    except (OSError, ValueError) as error:
        print(name, error)
