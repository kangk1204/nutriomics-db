"""Import the hashed development-only archive into an isolated desktop home."""
import hashlib
import json
from pathlib import Path
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ARCHIVE_SHA256 = "8eaa9a8161784a85bbc5a370dc5838db3fab420819177db7d2ec41327e4d701b"


def main():
    from nutri_literature.db import initialize, connect, dumps
    from nutri_literature.settings import Settings
    from nutri_literature import benchmarks
    archive_path = ROOT / "dist/chatgpt-development-inputs.tar.gz"
    if hashlib.sha256(archive_path.read_bytes()).hexdigest() != ARCHIVE_SHA256:
        raise ValueError("Development export archive hash differs")
    home = ROOT / "runtime/chatgpt-inputs"
    home.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "r:gz") as archive:
        archive.extractall(home, filter="data")
    settings = Settings(home, ROOT / "config/topics.json")
    initialize(settings)
    with connect(settings) as con:
        con.executemany("INSERT OR IGNORE INTO audit VALUES(?,?,?,?,?)",
                        json.loads((home / "public-audit.json").read_bytes()))
    source = home / "public_benchmarks/bioredirect/404a04bf9fb316caa061e9ab"
    index = {"steps": {"prepare:development:bioredirect:abstract": {
        "inputs_path": str(source / "inputs.json"), "gold_path": str(source / "gold.json")}}}
    (home / "public_benchmarks/v2-measurement-index.json").write_text(dumps(index), encoding="utf-8")
    documents, _, fingerprint = benchmarks._load_inputs(settings, source / "inputs.json")
    print(dumps({"home": str(home), "papers": len(documents["documents"]),
                 "input_sha256": fingerprint, "contains_credentials": False,
                 "official_dictionary": str(home / "official-id-dictionary.json.gz")}))


if __name__ == "__main__":
    main()
