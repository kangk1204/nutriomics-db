"""Transfer only complete frozen-vector caches; never models or predictions."""

import argparse
import io
import json
from pathlib import Path
import re
import sys
import tarfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def export_cache(cache, archive):
    from nutri_literature.db import digest, dumps, now
    from nutri_literature.nlp_v2 import cpu_inference_lock

    if archive.exists():
        raise ValueError("Cache snapshot output must be new")
    records = {}
    with tarfile.open(archive, "w") as bundle:
        for receipt in sorted(cache.glob("*.json")):
            key = receipt.stem
            if not re.fullmatch(r"[0-9a-f]{64}", key):
                continue
            with cpu_inference_lock(cache / (key + ".lock")):
                array = cache / (key + ".npy")
                if not array.exists():
                    continue
                payload, raw = array.read_bytes(), receipt.read_bytes()
                record = json.loads(raw)
                if record.get("key") != key or record.get("sha256") != digest(payload):
                    raise ValueError("Source vector cache integrity differs")
                if record.get("manifest", {}).get("weights_updated") is not False:
                    raise ValueError("Only frozen vectors may be transferred")
                for suffix, value in ((".npy", payload), (".json", raw)):
                    name = key + suffix
                    info = tarfile.TarInfo(name)
                    info.size = len(value)
                    info.mode = 0o600
                    bundle.addfile(info, io.BytesIO(value))
                    records[name] = digest(value)
        snapshot = {"schema": "frozen-vector-transfer-v6-1", "at": now(),
                    "files": records, "vectors": len(records) // 2,
                    "models_transferred": False, "predictions_transferred": False}
        raw = dumps(snapshot).encode()
        info = tarfile.TarInfo("snapshot.json")
        info.size = len(raw)
        info.mode = 0o600
        bundle.addfile(info, io.BytesIO(raw))
    return {"archive": str(archive), "sha256": digest(archive.read_bytes()),
            "vectors": snapshot["vectors"], "bytes": archive.stat().st_size}


def install_cache(cache, archive, expected_sha):
    from nutri_literature.db import digest
    from nutri_literature.nlp_v2 import cpu_inference_lock

    if digest(archive.read_bytes()) != expected_sha:
        raise ValueError("Transferred archive hash differs")
    cache.mkdir(parents=True, exist_ok=True)
    added = existing = 0
    with tarfile.open(archive, "r") as bundle:
        members = bundle.getmembers()
        names = [m.name for m in members]
        if len(set(names)) != len(names) or any(not m.isfile() for m in members):
            raise ValueError("Cache archive contains duplicate or non-file members")
        snapshot = json.load(bundle.extractfile("snapshot.json"))
        if snapshot.get("schema") != "frozen-vector-transfer-v6-1":
            raise ValueError("Unexpected vector transfer schema")
        if set(names) != set(snapshot["files"]) | {"snapshot.json"}:
            raise ValueError("Cache snapshot member list differs")
        for name in snapshot["files"]:
            if not re.fullmatch(r"[0-9a-f]{64}\.(json|npy)", name):
                raise ValueError("Unexpected cache member path")
        for key in sorted(name[:-5] for name in names if name.endswith(".json") and name != "snapshot.json"):
            payload, raw = (bundle.extractfile(key + suffix).read() for suffix in (".npy", ".json"))
            record = json.loads(raw)
            if (record.get("key") != key or digest(payload) != record.get("sha256")
                    or any(digest(value) != snapshot["files"][key + suffix]
                           for suffix, value in ((".npy", payload), (".json", raw)))):
                raise ValueError("Snapshot vector or receipt hash differs")
            with cpu_inference_lock(cache / (key + ".lock")):
                array, receipt = cache / (key + ".npy"), cache / (key + ".json")
                if array.exists() or receipt.exists():
                    if not array.exists() or not receipt.exists():
                        raise ValueError("Destination cache contains an incomplete pair")
                    if digest(array.read_bytes()) != digest(payload) or json.loads(receipt.read_bytes()) != record:
                        raise ValueError("Destination vector cache conflicts")
                    existing += 1
                    continue
                for target, value in ((array, payload), (receipt, raw)):
                    temporary = cache / (uuid.uuid4().hex + ".share-tmp")
                    temporary.write_bytes(value)
                    temporary.replace(target)
                added += 1
    return {"installed": added, "already_present": existing, "snapshot_vectors": snapshot["vectors"],
            "archive_sha256": expected_sha, "models_transferred": False, "predictions_transferred": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("export", "install"))
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--sha256")
    args = parser.parse_args()
    if args.operation == "install" and not args.sha256:
        parser.error("install requires the verified source archive SHA-256")
    result = export_cache(args.cache, args.archive) if args.operation == "export" else install_cache(
        args.cache, args.archive, args.sha256)
    print(json.dumps(result))
