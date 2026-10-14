"""Build a new portable candidate release; never publish or approve it implicitly."""
import argparse
import json
from pathlib import Path

from nutrigenomics.publication import build_candidate

if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("release", "core", "policy", "output"):
        p.add_argument("--" + name, type=Path, required=True)
    p.add_argument("--release-id", required=True)
    a = p.parse_args()
    print(json.dumps(build_candidate(a.release, a.core, a.policy, a.output, a.release_id), ensure_ascii=False, indent=2))
