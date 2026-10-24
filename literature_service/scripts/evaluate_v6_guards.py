"""Score representation guards with unchanged v6 relation/ICO evaluators."""

import argparse
import json
from pathlib import Path
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from nutri_literature import benchmarks as scoring, clinical_evaluation_v6
    from nutri_literature.decision_guard_v6 import guard_comparisons, implementation, POLICY
    from nutri_literature.db import digest, dumps, now, uid
    from nutri_literature.settings import Settings

    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    assert (
        digest(args.archive.read_bytes())
        == "2fde38d63af625310a927e477b44175eb88027dfe9ca9d56ee7db6e2033b5bb1"
    )
    spec = json.loads(args.spec.read_bytes())
    assert spec["official_split"] == "development"
    raw = Path(spec["inputs_path"]).read_bytes()
    assert digest(raw) == spec["inputs_sha256"]
    inputs = json.loads(raw)
    sources = {d["pmid"]: d for d in inputs["documents"]}
    originals = {}
    with tarfile.open(args.archive) as archive:
        for member in archive.getmembers():
            if member.name.startswith("paper-"):
                pred = json.load(archive.extractfile(member))
                assert pred["pmid"] not in originals
                assert pred["source_hash"] == sources[pred["pmid"]]["source_hash"]
                assert (
                    pred["artifact_identity"]
                    == "bb3d131c2e50b16542397570148283b669b2c7f83bcee8a3098a945d2ec65680"
                )
                originals[pred["pmid"]] = pred
    assert set(originals) == set(sources)
    args.output.mkdir(parents=True, exist_ok=True)
    predictions = {}
    identity = {
        "policy": POLICY,
        "sources": implementation(),
        "runner_sha256": digest(Path(__file__).read_bytes()),
        "input_sha256": digest(raw),
        "original_source_prediction_archive_sha256": digest(args.archive.read_bytes()),
    }
    for pmid, original in originals.items():
        selected, deferred = guard_comparisons(original["automatic_comparisons"])
        value = {
            "pmid": pmid,
            "source_hash": original["source_hash"],
            "identity": identity,
            "selected": selected,
            "deferred": deferred,
        }
        p = args.output / ("paper-" + pmid + ".json")
        if p.exists():
            assert json.loads(p.read_bytes()) == value
        else:
            scoring._write_immutable(p, value)
        predictions[pmid] = value
    # Source-derived decisions are all immutable before opening annotations.
    raw_gold = Path(spec["gold_path"]).read_bytes()
    assert digest(raw_gold) == spec["gold_sha256"]
    gold = json.loads(raw_gold)
    rows = {"baseline": [], "guarded": []}
    for d in gold["documents"]:
        assert digest(d["text"]) == predictions[d["pmid"]]["source_hash"]
        for stage, values in [
            ("baseline", originals[d["pmid"]]["automatic_comparisons"]),
            ("guarded", predictions[d["pmid"]]["selected"]),
        ]:
            counts = clinical_evaluation_v6.evidence_counts(d, values, [])
            rows[stage].append(
                {
                    "pmid": d["pmid"],
                    "automatic_ico_evidence_tokens": counts["automatic_ico_evidence_tokens"],
                    "ico_alignment": counts["ico_alignment"],
                }
            )
    key = "automatic_ico_evidence_tokens"
    metrics = {k: scoring._aggregate(v, key) for k, v in rows.items()}
    assert tuple(metrics["baseline"][k] for k in ("tp", "fp", "fn")) == (0, 1277, 9486)
    old = {d["pmid"]: d for d in rows["baseline"]}
    new = {d["pmid"]: d for d in rows["guarded"]}
    report = {
        "id": uid(),
        "at": now(),
        "schema": "v6-decision-guard-clinical-development-1",
        "identity": identity,
        "papers": len(old),
        "input_sha256": digest(raw),
        "gold_sha256": digest(raw_gold),
        "metrics": metrics,
        "per_paper": rows,
        "comparison_counts": {
            "baseline": sum(len(p["automatic_comparisons"]) for p in originals.values()),
            "guarded": sum(len(p["selected"]) for p in predictions.values()),
            "deferred_same_arm": sum(len(p["deferred"]) for p in predictions.values()),
        },
        "paired": scoring.paired_bootstrap(sorted(old), old, new, key, 2000, 20261010),
        "original_baseline_reproduced": True,
        "clinical_evaluator_sha256": digest(Path(clinical_evaluation_v6.__file__).read_bytes()),
        "full_engine_evaluation": False,
        "sealed_test_opened": False,
        "nutrition_accuracy": "unmeasured",
        "default_engine": "heuristic-v1",
    }
    scoring._write_immutable(args.output / "report.json", report)
    settings = Settings(ROOT / "runtime", ROOT / "config/topics.json")
    scoring._receipt(settings, "v6_guarded_clinical_development", report["id"], report)
    print(
        dumps(
            {
                "report": str(args.output / "report.json"),
                "metrics": metrics,
                "comparison_counts": report["comparison_counts"],
            }
        )
    )


if __name__ == "__main__":
    main()
