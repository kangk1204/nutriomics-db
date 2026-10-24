"""Verify delivery checksums and extract only into the named isolated checkout."""

import hashlib
import json
from pathlib import Path
import tarfile

BASE = Path("/home/keunsoo/projects")
ROOT = BASE / "nutriomics-literature-v6-development"


def main():
    if not ROOT.is_dir() or ROOT.resolve().parent != BASE:
        raise ValueError("Isolated development checkout must already exist")
    delivery = json.loads((BASE / "delivery-v6.json").read_bytes())
    for filename in ("source-v6-current.tar.gz", "inputs-v6-current.tar.gz"):
        path = BASE / filename
        if hashlib.sha256(path.read_bytes()).hexdigest() != delivery["archives"][filename]["sha256"]:
            raise ValueError("V6 delivery archive checksum differs")
        with tarfile.open(path, "r:gz") as archive:
            for member in archive.getmembers():
                target = (ROOT / member.name).resolve()
                if not target.is_relative_to(ROOT) or member.issym() or member.islnk():
                    raise ValueError("Archive target is outside the isolated development checkout")
            archive.extractall(ROOT, filter="data")
    import sys

    sys.path.insert(0, str(ROOT))
    # Normal package import retains relative imports and verifies the delivered rules.
    from nutri_literature.extraction_v6 import source_manifest

    if source_manifest()[1] != delivery["rules_version"]:
        raise ValueError("Delivered scientific source hash differs")
    print(
        json.dumps(
            {
                "installed": str(ROOT),
                "rules_version": delivery["rules_version"],
                "default_engine": "heuristic-v1",
                "models_trained": False,
            }
        )
    )


if __name__ == "__main__":
    main()
