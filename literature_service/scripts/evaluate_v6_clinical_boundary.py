"""Frozen, source-first boundary refinement development evaluation on all 92 papers."""

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from nutri_literature import benchmarks as scoring, clinical_boundary_v6 as boundary_code
    from nutri_literature.refinement_v6 import load_artifact, source_identity
    from nutri_literature.clinical_evaluation_v6 import evidence_counts
    from nutri_literature.extraction_v6 import source_manifest
    from nutri_literature.models_v6 import Models
    from nutri_literature.embeddings_v6 import FrozenEncoder
    from nutri_literature.db import digest, dumps, uid, now
    from nutri_literature.settings import Settings
    from nutri_literature.operations_v6 import processing_slot
    from nutri_literature.resources_v6 import memory_budget

    parser = argparse.ArgumentParser()
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--roles", type=Path, required=True)
    parser.add_argument("--boundary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    args = parser.parse_args()
    settings = Settings(ROOT / "runtime", ROOT / "config/topics.json")
    with memory_budget(), processing_slot(settings):
        roles, role_manifest = load_artifact(args.roles)
        boundary, manifest = boundary_code.load(args.boundary)
        assert manifest["upstream_role_manifest_sha256"] == digest(
            (args.roles / "manifest.json").read_bytes()
        )
        spec = json.loads(args.spec.read_bytes())
        assert spec["official_split"] == "development"
        raw = Path(spec["inputs_path"]).read_bytes()
        assert digest(raw) == spec["inputs_sha256"]
        inputs = json.loads(raw)
        assert not set(manifest["training_pmids"]) & {d["pmid"] for d in inputs["documents"]}
        assert all(
            not set(d) & {"comparisons", "evidence", "labels", "entities"} for d in inputs["documents"]
        )
        models = Models(args.models / "learned-v6")
        models.load()
        head = models.heads["evidence"]
        head_manifest = models.manifest["clinical_heads"]["evidence"]
        assert (
            head_manifest["manifest_sha256"]
            == "4c275cc8ba60ac9af3671de4acb65bd79b9168da699d8f71e05ce6907f3719b1"
        )
        encoder = FrozenEncoder(args.models / "gene/selected_model", settings.home / "embedding-cache-v6")
        encoder.load()
        identity = {
            "engine": "clinical-boundary-v6-r2",
            "v6_source": source_manifest()[1],
            "role_source": source_identity(),
            "boundary_source": boundary_code.implementation_sha(),
            "runner_sha256": digest(Path(__file__).read_bytes()),
            "input_sha256": digest(raw),
            "boundary_manifest_sha256": digest((args.boundary / "manifest.json").read_bytes()),
            "role_manifest_sha256": digest((args.roles / "manifest.json").read_bytes()),
            "evidence_head_sha256": head_manifest["manifest_sha256"],
        }
        args.output.mkdir(parents=True, exist_ok=True)
        preds = {}
        for d in inputs["documents"]:
            assert source_manifest()[1] == identity["v6_source"]
            assert boundary_code.implementation_sha() == identity["boundary_source"]
            assert source_identity() == identity["role_source"]
            assert digest(Path(__file__).read_bytes()) == identity["runner_sha256"]
            path = args.output / ("paper-" + d["pmid"] + ".json")
            if path.exists():
                pred = json.loads(path.read_bytes())
                assert pred["identity"] == identity and pred["source_hash"] == d["source_hash"]
            else:
                assert digest(d["text"]) == d["source_hash"]
                values, trace = boundary_code.comparisons(
                    d, roles, role_manifest, boundary, head, head_manifest["selection"]["threshold"], encoder
                )
                pred = {
                    "pmid": d["pmid"],
                    "identity": identity,
                    "source_hash": d["source_hash"],
                    "automatic_comparisons": values,
                    "trace": trace,
                }
                scoring._write_immutable(path, pred)
            preds[d["pmid"]] = pred
            print(
                dumps(
                    {
                        "stage": "boundary_predict",
                        "papers": len(preds),
                        "total": len(inputs["documents"]),
                        "comparisons": len(pred["automatic_comparisons"]),
                    }
                ),
                flush=True,
            )
        # Original strict evaluation opens annotations after every prediction.
        raw_gold = Path(spec["gold_path"]).read_bytes()
        assert digest(raw_gold) == spec["gold_sha256"]
        gold = json.loads(raw_gold)
        baseline = json.loads(args.baseline.read_bytes())
        assert baseline["inputs_sha256"] == digest(raw) and baseline["gold_sha256"] == digest(raw_gold)
        old = {r["pmid"]: r for r in baseline["per_paper"]}
        rows = []
        alignment = Counter()
        for d in gold["documents"]:
            assert digest(d["text"]) == preds[d["pmid"]]["source_hash"]
            counts = evidence_counts(d, preds[d["pmid"]]["automatic_comparisons"], [])
            row = {
                "pmid": d["pmid"],
                "automatic_ico_evidence_tokens": counts["automatic_ico_evidence_tokens"],
                "ico_alignment": counts["ico_alignment"],
            }
            rows.append(row)
            alignment.update(row["ico_alignment"])
        new = {r["pmid"]: r for r in rows}
        key = "automatic_ico_evidence_tokens"
        assert set(old) == set(new)
        assert all(
            old[p][key]["tp"] + old[p][key]["fn"] == new[p][key]["tp"] + new[p][key]["fn"] for p in old
        )
        metrics = {
            "baseline": scoring._aggregate(list(old.values()), key),
            "refinement": scoring._aggregate(rows, key),
        }
        report = {
            "id": uid(),
            "at": now(),
            "identity": identity,
            "papers": len(rows),
            "schema": "v6-r2-clinical-boundary-development-1",
            "input_sha256": digest(raw),
            "gold_sha256": digest(raw_gold),
            "baseline_report_sha256": baseline["original_report_sha256"],
            "metrics": metrics,
            "per_paper": rows,
            "alignment": dict(alignment),
            "paired": scoring.paired_bootstrap(sorted(old), old, new, key, 2000, 20261010),
            "default_engine": "heuristic-v1",
            "full_engine_evaluation": False,
            "sealed_test_opened": False,
            "nutrition_accuracy": "unmeasured",
            "mapping": "unchanged strict three-description lexical identity",
        }
        scoring._write_immutable(args.output / "report.json", report)
        scoring._receipt(settings, "v6_r2_clinical_boundary_development", report["id"], report)
        print(
            dumps(
                {"report": str(args.output / "report.json"), "metrics": metrics, "alignment": dict(alignment)}
            ),
            flush=True,
        )


if __name__ == "__main__":
    for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[name] = "1"
    if os.name == "posix":
        os.nice(10)
        locks = Path("/home/keunsoo/projects/nutriomics-literature/runtime/locks")
        if not locks.exists():
            locks = ROOT / "runtime/locks"
        os.environ["LITERATURE_NLP_LOCK"] = str(locks / "nlp-cpu.lock")
        os.environ["LITERATURE_PROCESSING_LOCK_HOME"] = str(locks)
    main()
