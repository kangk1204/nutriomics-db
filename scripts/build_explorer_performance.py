"""Build a new performance release without changing the live pointer."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutrigenomics.explorer_performance_build import build_performance_release

if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--from-release", type=Path, required=True)
    p.add_argument("--core-db", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--release-id", required=True)
    p.add_argument("--ram-workspace", action="store_true")
    p.add_argument("--memory-budget-gib", type=int, default=32)
    a = p.parse_args()
    m = build_performance_release(a.from_release, a.core_db, a.output_dir, a.release_id, a.ram_workspace, a.memory_budget_gib)
    print(json.dumps({k: m[k] for k in ("release_id", "performance", "database_sha256")}, ensure_ascii=False))
