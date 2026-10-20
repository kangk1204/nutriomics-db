"""Prepare portable, rights-scoped submission files; uploading is never automatic."""
import argparse
import json
from pathlib import Path

from nutrigenomics.submission_package import assemble, prepare


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("prepare")
    for field in ["release", "code-archive", "original-builder-archive", "dictionary", "prior-verification", "output"]:
        build.add_argument("--" + field, type=Path, required=True)
    for field in ["expected-prior-sha256", "expected-manifest-sha256", "expected-builder-archive-sha256"]:
        build.add_argument("--" + field, required=True)
    build.add_argument("--expected-code-sha256")
    build.add_argument("--current-reader-archive", type=Path)
    build.add_argument("--expected-reader-sha256")
    for field in ["corrections", "proofs", "official-policy-snapshot"]:
        build.add_argument("--" + field, type=Path)
    copy = commands.add_parser("assemble")
    copy.add_argument("--package", type=Path, required=True)
    copy.add_argument("--expected-manifest-sha256", required=True)
    copy.add_argument("--roots-json", type=Path, required=True)
    copy.add_argument("--destination", type=Path, required=True)
    args = vars(parser.parse_args())
    command = args.pop("command")
    if command == "assemble":
        args["roots"] = json.loads(args.pop("roots_json").read_text(encoding="utf-8"))
        result = assemble(**args)
    else:
        if args.get("official_policy_snapshot"):
            args["official_policy_snapshot"] = json.loads(args["official_policy_snapshot"].read_text(encoding="utf-8"))
        result = prepare(**args)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
