"""Package only v7 additions and explicit integration edits, never secrets/DB."""
from pathlib import Path
import hashlib
import json
import tarfile
import shutil

ROOT = Path(__file__).resolve().parents[1]
names = sorted({*ROOT.glob('nutri_literature/*_v7.py'), *ROOT.glob('scripts/*v7*.py'),
                *ROOT.glob('tests/*v7*.py'), *ROOT.glob('docs/*v7*.md')})
for relative in ('nutri_literature/api.py', 'nutri_literature/pipeline.py','ui/app.py'):
    names.append(ROOT / relative)
target = ROOT / 'dist/source-v7-current.tar.gz'
if target.exists():
    previous=hashlib.sha256(target.read_bytes()).hexdigest()
    archived=target.with_name('source-v7-'+previous[:12]+'.tar.gz')
    if not archived.exists():shutil.copy2(target,archived)
    old_manifest=target.with_suffix('.manifest.json')
    if old_manifest.exists() and not archived.with_suffix('.manifest.json').exists():
        shutil.copy2(old_manifest,archived.with_suffix('.manifest.json'))
with tarfile.open(target, 'w:gz') as archive:
    for name in names:
        archive.add(name, arcname=str(name.relative_to(ROOT)).replace('\\', '/'))
manifest = {'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
            'files': {str(p.relative_to(ROOT)).replace('\\', '/'): hashlib.sha256(p.read_bytes()).hexdigest() for p in names}}
target.with_suffix('.manifest.json').write_text(json.dumps(manifest, sort_keys=True))
print(json.dumps({'path': str(target), 'files': len(names), 'sha256': manifest['sha256']}))
