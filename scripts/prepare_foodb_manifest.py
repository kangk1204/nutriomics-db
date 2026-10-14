from __future__ import annotations

import argparse
import json
from pathlib import Path

from nutrigenomics.foodb import build_manifest


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Hash already-staged official FooDB table files; this does not download data"
    )
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--foods", required=True, help="Foods CSV basename")
    parser.add_argument("--compounds", required=True, help="Compounds CSV basename")
    parser.add_argument("--contents", required=True, help="Contents CSV basename")
    parser.add_argument("--replace", action="store_true", help="Replace an existing manifest explicitly")
    args = parser.parse_args()
    data_dir = args.data_dir.expanduser().resolve()
    manifest_path = data_dir / "manifest.json"
    if manifest_path.exists() and not args.replace:
        raise FileExistsError(f"Refusing to replace an existing manifest without --replace: {manifest_path}")
    manifest = build_manifest(
        data_dir,
        {"foods": args.foods, "compounds": args.compounds, "contents": args.contents},
    )
    temporary = manifest_path.with_suffix(".json.partial")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(manifest_path)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
