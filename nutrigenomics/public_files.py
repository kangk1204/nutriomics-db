"""Manifest-bound public downloads; no internal file or directory fallback."""
from pathlib import Path, PurePosixPath

from fastapi import HTTPException
from fastapi.responses import FileResponse

from .explorer_build import sha256


def _path(root, name):
    relative = PurePosixPath(name)
    if (not name or relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name):
        raise ValueError("Invalid release file path")
    path = root / name
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError("Release file escapes its immutable root")
    return path


def attach_public_files(app, release, manifest, release_id, preverified=None):
    root = Path(release).resolve()
    files = {}
    for entry in manifest.get("files", []):
        name = entry["path"]
        path = _path(root, name)
        if not path.is_file() or path.stat().st_size != entry["bytes"]:
            raise ValueError("Manifest download file absent or wrong size")
        stat = path.stat()
        actual = (preverified or {}).get(name) or sha256(path)
        if actual != entry["sha256"]:
            raise ValueError("Manifest download file hash mismatch")
        identity = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)
        files[name] = (dict(entry), path, identity)

    @app.get("/api/v1/public/files")
    def listing():
        return {"release_id": release_id, "published": manifest.get("published", False),
            "availability": "candidate_release_files" if files else "files_not_manifested",
            "items": [{**entry, "url": "/api/v1/public/files/" + name} for name, (entry, _, _) in sorted(files.items())],
            "note": "Only this rights-filtered release is downloadable. Availability is not expert approval or DOI registration."}

    @app.get("/api/v1/public/files/{name:path}")
    def download(name: str):
        try:
            _path(root, name)
        except ValueError as exc:
            raise HTTPException(404, "File is not in this public release") from exc
        if name not in files:
            raise HTTPException(404, "File is not in this public release")
        entry, path, identity = files[name]
        if not path.is_file() or path.is_symlink():
            raise HTTPException(503, {"code": "source_unavailable"})
        stat = path.stat()
        if (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino) != identity:
            raise HTTPException(503, {"code": "source_changed"})
        return FileResponse(path, filename=path.name, stat_result=stat,
            headers={"ETag": '"' + entry["sha256"] + '"', "X-Content-SHA256": entry["sha256"],
                     "X-NutriOmics-Release": release_id})
