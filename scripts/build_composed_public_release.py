"""Build one new native public SQLite from a base and materialized corrected package."""
import argparse
import json
from pathlib import Path

from nutrigenomics.composed_release import build


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("base", "candidate", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--release-id", required=True)
    for name in ("base-manifest", "candidate-filemap"):
        parser.add_argument("--expected-" + name + "-sha256", required=True)
    for name in ("decisions", "classifications", "review-dependencies"):
        parser.add_argument("--" + name, type=Path)
        parser.add_argument("--expected-" + name + "-sha256")
    args = vars(parser.parse_args())
    manifest = build(**args)
    print(json.dumps({"release_id": manifest["release_id"], "database_sha256": manifest["database_sha256"],
                      "submission_ready": manifest["submission_ready"], "review_summary": manifest["review_summary"]}))


if __name__ == "__main__":
    main()
