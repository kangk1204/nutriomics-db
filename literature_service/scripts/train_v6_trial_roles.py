import argparse
import os
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from nutri_literature.trial_roles_v6 import train
    from nutri_literature.resources_v6 import memory_budget
    from nutri_literature.operations_v6 import processing_slot
    from nutri_literature.settings import Settings
    p = argparse.ArgumentParser()
    for key in ("data", "audit", "models", "output"):
        p.add_argument("--" + key, type=Path, required=True)
    a = p.parse_args()
    with memory_budget(), processing_slot(Settings(ROOT / "runtime", ROOT / "config/topics.json")):
        train(a.data, a.audit, a.models, a.output)


if __name__ == "__main__":
    for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[key] = "1"
    if os.name == "posix":
        os.nice(10)
    main()
