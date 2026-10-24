"""Create an isolated checkout/environment; never alter an earlier engine."""
import hashlib
import json
from pathlib import Path
import shutil
import sysconfig
import tarfile
import venv


def main():
    root = Path('/home/keunsoo/projects/nutriomics-literature-v7-development')
    old = root.with_name('nutriomics-literature-v6-development')
    root.mkdir(exist_ok=True)
    frozen = {}
    for directory in ('nutri_literature', 'scripts', 'tests', 'config', 'ui'):
        for p in (old / directory).rglob('*'):
            if not p.is_file() or '__pycache__' in p.parts:
                continue
            target = root / p.relative_to(old)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copy2(p, target)
            if directory == 'nutri_literature':
                frozen[str(p.relative_to(old))] = hashlib.sha256(p.read_bytes()).hexdigest()
    for name in ('pyproject.toml', 'requirements-v6.lock'):
        if not (root / name).exists():
            shutil.copy2(old / name, root / name)
    runtime = root / 'runtime'
    runtime.mkdir(exist_ok=True)
    for name, checksum in [
        ('v6-refinement-results-20261015.tar.gz', '3bd4230cb7bc3d767550c3bd1966e559a589eadf8b46cb4a5acee4a2421e8592'),
        ('v6-automatic-upgrade-20261015-complete.tar.gz', '8bba4b4d15e4aa11cd5cb50ade6d8b15565f4f024ec14888cb28f1f7a65a02db')]:
        archive = root/name if (root/name).exists() else old/'dist'/name
        assert hashlib.sha256(archive.read_bytes()).hexdigest() == checksum
        with tarfile.open(archive) as bundle:
            for member in bundle:
                if not member.isfile():
                    continue
                target = root / member.name
                if not target.resolve().is_relative_to(root.resolve()):
                    raise ValueError('Unsafe recovery member')
                if target.exists():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(bundle.extractfile(member).read())
    for relative in ('v6/models', 'v6/training-data', 'public_benchmarks'):
        target = runtime / relative
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(old / 'runtime' / relative, target_is_directory=True)
        elif target.is_dir() and not target.is_symlink():
            # Recovery archives contain partial train folders; attach missing
            # immutable siblings without replacing recovered artifacts.
            for child in (old / 'runtime' / relative).iterdir():
                if not (target / child.name).exists():
                    (target / child.name).symlink_to(child, target_is_directory=child.is_dir())
    if not (root / '.venv').exists():
        venv.EnvBuilder(with_pip=True).create(root / '.venv')
        site = next((root / '.venv/lib').glob('python*/site-packages'))
        (site / 'v6-readonly-dependencies.pth').write_text(sysconfig.get_path('purelib') + '\n')
    destination = runtime / 'v7'
    destination.mkdir(exist_ok=True)
    snapshot = destination / 'legacy-source-snapshot.json'
    if not snapshot.exists():
        snapshot.write_text(json.dumps(frozen, sort_keys=True))
    print(json.dumps({'root':str(root), 'legacy_sources':len(frozen), 'existing_default_modified':False}))


if __name__ == '__main__':
    main()
