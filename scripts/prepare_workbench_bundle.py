"""Freeze owned source files and frontend build into a reviewed staging archive."""
import argparse
import tarfile
from pathlib import Path
from nutrigenomics.explorer_build import sha256

ROOT = Path(__file__).resolve().parents[1]

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    files = [*ROOT.glob('nutrigenomics/*.py'), *ROOT.glob('nutrigenomics/candidate_rules/*.py'), *ROOT.glob('scripts/*.py'), *ROOT.glob('tests/*.py'),
             *ROOT.glob('config/*.json'), *ROOT.glob('docs/WORKBENCH*'),
             ROOT/'docs/MDPI_DATA_SUBMISSION_PREPARATION_ko.md',ROOT/'pyproject.toml',ROOT/'uv.lock']
    for directory in ['frontend/explorer/src','frontend/explorer/scripts','frontend/explorer/dist']:
        files += [f for f in (ROOT/directory).rglob('*') if f.is_file()]
    for name in ['package.json','pnpm-lock.yaml','vite.config.ts','tsconfig.json','index.html','README.md']:
        files.append(ROOT/'frontend/explorer'/name)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(a.output,'w:gz') as out:
        for f in sorted(set(files)):
            if f.is_file(): out.add(f,arcname=f.relative_to(ROOT))
    print(sha256(a.output))
