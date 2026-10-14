"""Fixed, bounded official-source probes; no agreement acceptance or bulk fetch."""
import hashlib
import json
import shutil
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path('research/2026-09-30/expansion/continuation')
URLS = {
    'uniprot-license.json': 'https://rest.uniprot.org/configure/help/license',
    'uniprot-Q02809.json': 'https://rest.uniprot.org/uniprotkb/Q02809.json',
    'uniprot-Q9P2J5.json': 'https://rest.uniprot.org/uniprotkb/Q9P2J5.json',
    'GSE323980-supp.html': 'https://ftp.ncbi.nlm.nih.gov/geo/series/GSE323nnn/GSE323980/suppl/',
    'GSE15284-matrix.html': 'https://ftp.ncbi.nlm.nih.gov/geo/series/GSE15nnn/GSE15284/matrix/',
    'GSE46761-supp.html': 'https://ftp.ncbi.nlm.nih.gov/geo/series/GSE46nnn/GSE46761/suppl/',
}


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(ROOT).free < 1_010_000_000:
        raise ValueError('storage reserve')
    records = []
    for name, url in URLS.items():
        target = ROOT / name
        record = {'file': name, 'url': url}
        if target.exists():
            raw = target.read_bytes()
            record['capture_status'] = 'existing_snapshot_preserved'
        else:
            try:
                with urllib.request.urlopen(url, timeout=20) as response:
                    raw = response.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise ValueError('1 MB cap')
                target.write_bytes(raw)
                record['capture_status'] = 'captured'
                record['retrieved_at'] = datetime.now(timezone.utc).isoformat()
            except (OSError, ValueError) as error:
                record['error'] = str(error)
                records.append(record)
                continue
        record.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        records.append(record)
        if name.endswith('html') or 'license' in name:
            print(name, raw.decode()[:12000])
        else:
            data = json.loads(raw)
            print(name, json.dumps({'accession': data.get('primaryAccession'),
                  'comments': data.get('comments'), 'genes': data.get('genes')})[:16000])
    manifest = ROOT / 'probe-manifest.json'
    if not manifest.exists():
        manifest.write_text(json.dumps(records, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
