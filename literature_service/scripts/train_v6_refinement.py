"""Fit isolated experimental refinement heads from audited train files only."""

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from nutri_literature.refinement_v6 import train_relations, train_clinical_roles
    from nutri_literature.operations_v6 import processing_slot
    from nutri_literature.resources_v6 import memory_budget
    from nutri_literature.settings import Settings

    parser = argparse.ArgumentParser()
    parser.add_argument("--task", choices=("relations", "clinical", "boundary"), required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--upstream-role-manifest", type=Path)
    args = parser.parse_args()
    with (
        memory_budget() as resources,
        processing_slot(Settings(ROOT / "runtime", ROOT / "config/topics.json")),
    ):
        print(json.dumps({"stage": "training_started", "task": args.task}), flush=True)
        if args.task == "boundary":
            from nutri_literature.clinical_boundary_v6 import train

            if args.upstream_role_manifest is None:
                parser.error("Boundary training requires --upstream-role-manifest")
            result = train(args.data, args.audit, args.output, args.upstream_role_manifest)
        else:
            function = train_relations if args.task == "relations" else train_clinical_roles
            result = function(args.data, args.audit, args.output)
        print(
            json.dumps(
                {
                    "stage": "training_completed",
                    "task": args.task,
                    "manifest": str(args.output / "manifest.json"),
                    "resources": resources,
                    "selection": result.get("selection"),
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    for variable in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[variable] = "1"
    if os.name == "posix":
        os.nice(10)
        production = Path("/home/keunsoo/projects/nutriomics-literature/runtime/locks")
        if production.exists():
            os.environ["LITERATURE_PROCESSING_LOCK_HOME"] = str(production)
            os.environ["LITERATURE_NLP_LOCK"] = str(production / "nlp-cpu.lock")
    main()
