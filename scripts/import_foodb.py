from __future__ import annotations

import argparse
import json
from pathlib import Path

from nutrigenomics.foodb import import_foodb
from nutrigenomics.settings import DATA_DIR, DATABASE_URL


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import staged FooDB compound composition using a reviewed food crosswalk"
    )
    parser.add_argument(
        "--data-dir", type=Path, default=DATA_DIR / "raw" / "foodb" / "foodb-2020-04-07"
    )
    parser.add_argument("--crosswalk", type=Path, required=True)
    parser.add_argument("--database-url", default=DATABASE_URL)
    args = parser.parse_args()
    print(
        json.dumps(
            import_foodb(args.data_dir, args.crosswalk, args.database_url),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
