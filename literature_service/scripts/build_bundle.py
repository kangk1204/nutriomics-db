"""Source-only archive; exclude runtime text, credentials, caches and environments."""

from pathlib import Path
import hashlib
import tarfile

root = Path(__file__).resolve().parents[1]
out = root / "dist" / "source.tar.gz"
out.parent.mkdir(exist_ok=True)
excluded = {".venv", ".venv-local", "runtime", "dist", "__pycache__", ".pytest_cache", ".ruff_cache", ".git"}
with tarfile.open(out, "w:gz") as archive:
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if (
            path.is_file()
            and not any(p in excluded or p.startswith(".venv") or p.endswith(".egg-info") for p in relative.parts)
            and path.name != ".env"
        ):
            archive.add(path, arcname=relative.as_posix(), recursive=False)
print(str(out))
print(hashlib.sha256(out.read_bytes()).hexdigest())
