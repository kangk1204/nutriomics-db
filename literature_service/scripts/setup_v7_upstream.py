"""Restore exact v6 reference heads in v7; legacy model directories are read-only."""
from pathlib import Path
import tarfile,json,hashlib
ROOT=Path(__file__).resolve().parents[1]
target=ROOT/'runtime/v7/frozen-v6-upstream'
target.mkdir(parents=True,exist_ok=True)
for name,checksum in [
    ('v7-frozen-v6-heads.tar.gz','6a247d13c6e05f81a218aa9fbd661517833a91f636fc290de7326ab53e320632'),
    ('v7-frozen-v6-clinical-heads.tar.gz','6e98b8d3d29899e284aa0b78cf3729d559ad90d1118e4e8f654dd15321033ef6')]:
    archive=ROOT/name
    assert hashlib.sha256(archive.read_bytes()).hexdigest()==checksum
    with tarfile.open(archive) as t:
        for member in t:
            if not member.isfile():continue
            p=target/member.name
            if not p.resolve().is_relative_to(target):raise ValueError('Unsafe reference model member')
            p.parent.mkdir(parents=True,exist_ok=True)
            value=t.extractfile(member).read()
            if p.exists() and p.read_bytes()!=value:raise ValueError('Immutable reference weights differ')
            if not p.exists():p.write_bytes(value)
for child in (ROOT/'runtime/v6/models').iterdir():
    p=target/child.name
    if not p.exists():p.symlink_to(child.resolve(),target_is_directory=child.is_dir())
record={name:hashlib.sha256((target/name/'manifest.json').read_bytes()).hexdigest() for name in ('clinical','learned-v6','learned-v5')}
assert record['clinical']=='5088790d277137c591ade68ab0bb5aaedb3d7ca15c9c7d6ccf1202eb0a846f1c'
assert record['learned-v6']=='6661c53bdeb206ba75d8cb3bec5065b609eb3c729b4d7fce42e528c4d8f11c22'
(target/'v7-reference-receipt.json').write_text(json.dumps(record,sort_keys=True))
print(json.dumps(record))
