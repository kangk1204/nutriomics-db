import argparse
import os
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from nutri_literature.automatic_models_v6 import train
    from nutri_literature.operations_v6 import processing_slot
    from nutri_literature.resources_v6 import memory_budget
    from nutri_literature.settings import Settings
    from nutri_literature.db import dumps
    parser = argparse.ArgumentParser()
    for key in ("features", "gold-export", "output"):
        parser.add_argument("--" + key, type=Path, required=True)
    args = parser.parse_args()
    with memory_budget() as resources, processing_slot(Settings(ROOT / "runtime", ROOT / "config/topics.json")):
        receipt = train(args.features, args.gold_export, args.output)
        print(dumps({"stage": "automatic_training_complete", "selection": receipt["selection"], "resources": resources}), flush=True)


if __name__ == "__main__":
    for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[key] = "1"
    if os.name == "posix":
        os.nice(10)
    main()
