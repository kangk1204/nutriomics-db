"""Check the versioned code inventory; does not validate scientific findings."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = 'code-files.sha256.json'


def member(root, name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or '\\' in name or path.as_posix() != name:
        raise ValueError('Invalid repository path: ' + name)
    resolved = (root / name).resolve(strict=True)
    if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
        raise ValueError('Repository file missing or outside root: ' + name)
    return resolved


def inventory(root=ROOT):
    result = subprocess.run(['git', 'ls-files', '-z'], cwd=root, check=True,
                            capture_output=True)
    names = result.stdout.decode('utf-8').split('\0')
    return {name: hashlib.sha256(member(root, name).read_bytes()).hexdigest()
            for name in sorted(set(names)) if name and name != MANIFEST}


def verify(root=ROOT):
    expected = json.loads((root / MANIFEST).read_text(encoding='utf-8'))
    if not isinstance(expected, dict) or not expected:
        raise ValueError('Empty or invalid source inventory')
    for name, digest in expected.items():
        if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError('Invalid SHA-256: ' + name)
        actual = hashlib.sha256(member(root, name).read_bytes()).hexdigest()
        if actual != digest:
            raise ValueError('SHA-256 mismatch: ' + name)
    # An extracted source archive may have no Git metadata. Hash checks still apply.
    if (root / '.git').exists() and set(inventory(root)) != set(expected):
        raise ValueError('Tracked source coverage differs from inventory')
    return dict(status='pass', files=len(expected), sha256_fields_verified=True,
                scope='Repository bytes and tracked-file coverage; not scientific validation')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write', action='store_true', help='Record staged/tracked files from Git')
    args = parser.parse_args()
    if args.write:
        (ROOT / MANIFEST).write_text(json.dumps(inventory(), ensure_ascii=False, indent=2)
                                     + '\n', encoding='utf-8')
    print(json.dumps(verify(), ensure_ascii=False))


if __name__ == '__main__':
    main()
