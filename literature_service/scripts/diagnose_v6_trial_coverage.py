"""Post-hoc error analysis only; no parameter/threshold selection."""
from collections import Counter
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from nutri_literature.refinement_v6 import tokens, literal_matches, words
    from nutri_literature.db import digest, dumps
    from nutri_literature.benchmarks import _write_immutable
    spec = json.loads((ROOT / "runtime/v6/training-data/recovery/inference-official-booleans/development-confirmed/spec.json").read_bytes())
    gold = json.loads(Path(spec["gold_path"]).read_bytes())
    counts, first = Counter(), Counter()
    for doc in gold["documents"]:
        pred = json.loads((ROOT / "runtime/v6/refinement-r5/clinical-development" / ("paper-" + doc["pmid"] + ".json")).read_bytes())
        assert pred["source_hash"] == digest(doc["text"])
        raw = pred["trace"]["source_role_candidates"]
        role_keys = {role: {tuple(words(s["text"])) for s in raw[role]} for role in raw}
        registry = pred["trace"]["arm_registry"]
        reg_keys = {tuple(words(a["description"]["text"])) for a in registry}
        selected = {tuple(tuple(words(c[k + "_description"])) for k in ("intervention", "comparator", "outcome")) for c in pred["automatic_comparisons"]}
        sequence = tokens(doc["text"])
        for comparison in doc["comparisons"]:
            counts["prompts"] += 1
            names = [comparison[r + "_description"] for r in ("intervention", "comparator", "outcome")]
            literal = all(literal_matches(sequence, [name]) for name in names)
            counts["all_three_literal"] += literal
            covered = []
            for role, name in zip(("intervention", "comparator", "outcome"), names):
                key = tuple(words(name))
                found = key in role_keys[role]
                counts[role + "_raw_role_coverage"] += found
                covered.append(key in reg_keys if role != "outcome" else found)
            counts["registry_and_outcome_complete"] += all(covered)
            if not literal:
                first["description_not_literal"] += 1
            elif not covered[0]:
                first["intervention_not_in_arm_registry"] += 1
            elif not covered[1]:
                first["comparator_not_in_arm_registry"] += 1
            elif not covered[2]:
                first["outcome_not_in_role_candidates"] += 1
            elif tuple(tuple(words(n)) for n in names) not in selected:
                first["arm_or_outcome_link_unresolved"] += 1
            else:
                first["description_triple_matched_evidence_scored_separately"] += 1
    result = {"counts": dict(counts), "first_stage_partition": dict(first),
              "purpose": "post-hoc development error analysis; not parameter selection",
              "gold_sha256": spec["gold_sha256"], "nutrition_accuracy": "unmeasured"}
    path = ROOT / "runtime/v6/refinement-r5/clinical-loss-diagnostics.json"
    _write_immutable(path, result)
    print(dumps(result))


if __name__ == "__main__":
    main()
