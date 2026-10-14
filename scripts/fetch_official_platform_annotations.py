"""Two bounded official annotation files; no raw arrays or bulk resources."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutrigenomics.expansion import fetch

ROOT = Path("research/2026-09-30/expansion/platform-annotations")


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    manifest = ROOT / "capture-manifest.json"
    if manifest.exists():
        raise ValueError("Saved probe manifest exists; use captured data")
    rows = []
    for platform, group in [("GPL11532", "GPL11nnn"), ("GPL570", "GPLnnn")]:
        name = platform + ".annot.gz"
        url = f"https://ftp.ncbi.nlm.nih.gov/geo/platforms/{group}/{platform}/annot/{name}"
        try:
            row = fetch(url, ROOT / name, max_bytes=9_000_000)
            row["file"] = name
        except (OSError, ValueError) as error:
            row = {"file": name, "url": url, "error": str(error)}
        rows.append(row)
        manifest.write_text(json.dumps(rows, indent=2))
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
