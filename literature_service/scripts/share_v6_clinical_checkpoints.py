"""Merge committed train-only numeric checkpoints from an authorized CPU host."""

import argparse
import io
import json
import os
from pathlib import Path
import re
import signal
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def export(directory, archive):
    from nutri_literature.db import digest, dumps, now

    if archive.exists() or not re.fullmatch(r"[a-f0-9]{24}", directory.name):
        raise ValueError("New archive and explicit clinical namespace required")
    files = {}
    with tarfile.open(archive, "w") as bundle:
        for path in sorted(directory.glob("*.json")):
            if not re.fullmatch(r"[a-f0-9]{32}", path.stem):
                continue
            try:
                raw = path.read_bytes()
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue  # A receipt currently being committed is not a checkpoint.
            data = directory / record["filename"]
            if (
                record["filename"] != path.stem + ".npz"
                or record["key"][:32] != path.stem
                or not data.exists()
            ):
                raise ValueError("Source checkpoint identity differs")
            payload = data.read_bytes()
            if digest(payload) != record["sha256"]:
                raise ValueError("Source checkpoint hash differs")
            for name, value in ((path.name, raw), (data.name, payload)):
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(value), 0o600
                bundle.addfile(info, io.BytesIO(value))
                files[name] = digest(value)
        snapshot = {
            "schema": "clinical-checkpoint-transfer-v6-1",
            "namespace": directory.name,
            "at": now(),
            "files": files,
            "paper_checkpoints": len(files) // 2,
            "models_fitted": False,
            "disposable_matrices_transferred": False,
        }
        raw = dumps(snapshot).encode()
        info = tarfile.TarInfo("snapshot.json")
        info.size, info.mode = len(raw), 0o600
        bundle.addfile(info, io.BytesIO(raw))
    return {
        "sha256": digest(archive.read_bytes()),
        "bytes": archive.stat().st_size,
        "paper_checkpoints": snapshot["paper_checkpoints"],
        "namespace": directory.name,
    }


def install(archive, expected, plan_path, pid):
    from nutri_literature.audit_v6 import upstream_clinical
    from nutri_literature.db import digest, dumps
    from nutri_literature.features_v6 import source_hashes
    from nutri_literature.training_v4 import text_key

    if os.name != "posix" or digest(archive.read_bytes()) != expected:
        raise ValueError("Verified Linux training-checkpoint transfer required")
    plan = json.loads(plan_path.read_bytes())
    folder = Path(plan["evidence"])
    data, audit = (json.loads((folder / name).read_bytes()) for name in ("relation-train.json", "audit.json"))
    if (
        data["split"] != "train"
        or not audit["passed"]
        or audit["protected_overlap"]
        or digest((folder / "relation-train.json").read_bytes()) != audit["audited_training_sha256"]
    ):
        raise ValueError("Exact audited training input required")
    models = Path(plan["models"])
    encoder = json.loads((models / "clinical-v6-outcome/manifest.json").read_bytes())["encoder"]
    identity = {
        "kind": "evidence",
        "features": source_hashes("clinical"),
        "encoder": encoder,
        "training_sha256": digest((folder / "relation-train.json").read_bytes()),
        "audit_sha256": digest((folder / "audit.json").read_bytes()),
        "upstream_clinical_audit": upstream_clinical(models / "clinical", audit),
    }
    directory = ROOT / "runtime/embedding-cache-v6/clinical-training" / digest(dumps(identity))[:24]
    allowed = set(audit["training_pmids"])
    protected = set(audit["protected_text_hashes"])
    documents = [
        d for d in data["documents"] if str(d["pmid"]) in allowed and text_key(d["text"]) not in protected
    ]
    expected_keys = {
        digest(dumps({"identity": identity, "document_sha256": digest(dumps(d))})): d["pmid"]
        for d in documents
    }
    command = Path("/proc") / str(pid) / "cmdline"
    if not command.exists():
        raise ValueError("The owned evidence fit is no longer running")
    raw_command = command.read_bytes().replace(b"\0", b" ").decode()
    if (
        str(ROOT / ".venv/bin/python") not in raw_command
        or "--kind evidence" not in raw_command
        or str(folder / "relation-train.json") not in raw_command
    ):
        raise ValueError("Only this task's exact evidence fit may be briefly stopped")
    installed = existing = active_skipped = 0
    os.kill(pid, signal.SIGSTOP)
    try:
        # Avoid the next/currently computing paper. Other committed papers are
        # immutable; the learner will read future receipts on reaching them.
        active = next(
            (key[:32] for key in expected_keys if not (directory / (key[:32] + ".json")).exists()), None
        )
        with tarfile.open(archive) as bundle:
            members = bundle.getmembers()
            names = [m.name for m in members]
            if len(set(names)) != len(names) or any(not m.isfile() for m in members):
                raise ValueError("Unexpected checkpoint archive members")
            snapshot = json.load(bundle.extractfile("snapshot.json"))
            if (
                snapshot["schema"] != "clinical-checkpoint-transfer-v6-1"
                or snapshot["namespace"] != directory.name
                or set(names) != set(snapshot["files"]) | {"snapshot.json"}
            ):
                raise ValueError("Training checkpoint namespace/member list differs")
            if any(not re.fullmatch(r"[a-f0-9]{32}\.(json|npz)", name) for name in snapshot["files"]):
                raise ValueError("Unexpected checkpoint path")
            for name in sorted(n for n in snapshot["files"] if n.endswith(".json")):
                raw = bundle.extractfile(name).read()
                record = json.loads(raw)
                if record["key"] not in expected_keys or expected_keys[record["key"]] != record["pmid"]:
                    raise ValueError("Transferred checkpoint is outside the exact audited training cohort")
                if name[:-5] == active:
                    active_skipped += 1
                    continue
                receipt, array = directory / name, directory / record["filename"]
                if receipt.exists() or array.exists():
                    existing += 1
                    continue  # Retain locally computed vectors and the in-flight writer.
                payload = bundle.extractfile(record["filename"]).read()
                if (
                    digest(raw) != snapshot["files"][name]
                    or digest(payload) != record["sha256"]
                    or digest(payload) != snapshot["files"][record["filename"]]
                    or record["filename"] != name[:-5] + ".npz"
                ):
                    raise ValueError("Transferred checkpoint hash differs")
                directory.mkdir(parents=True, exist_ok=True)
                for target, content in ((array, payload), (receipt, raw)):
                    temporary = target.with_suffix(target.suffix + ".transfer-tmp")
                    temporary.write_bytes(content)
                    temporary.replace(target)
                installed += 1
    finally:
        os.kill(pid, signal.SIGCONT)
    return {
        "installed": installed,
        "retained_local": existing,
        "active_paper_skipped": active_skipped,
        "namespace": directory.name,
        "archive_sha256": expected,
        "models_fitted": False,
        "complete_training_cohort_unchanged": len(documents),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("export", "install"))
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--sha256")
    parser.add_argument("--pid", type=int)
    args = parser.parse_args()
    if args.operation == "export" and not args.directory:
        parser.error("export requires --directory")
    if args.operation == "install" and not all((args.plan, args.sha256, args.pid)):
        parser.error("install requires --plan, --sha256 and the verified owned --pid")
    print(
        json.dumps(
            export(args.directory, args.archive)
            if args.operation == "export"
            else install(args.archive, args.sha256, args.plan, args.pid)
        )
    )
