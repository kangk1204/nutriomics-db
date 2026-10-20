"""Build a reproducible, unapproved food-state reviewer worksheet."""
import argparse
import csv
import hashlib
import json
from pathlib import Path

from nutrigenomics.food_semantic_precheck import prepare_food_checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("review", "internal-panel", "public-panel", "output"):
        parser.add_argument("--" + key, type=Path, required=True)
    args = parser.parse_args()
    review = json.loads(args.review.read_text(encoding="utf-8"))
    result = prepare_food_checks(review, {"internal": args.internal_panel, "public": args.public_panel})
    args.output.mkdir(parents=True, exist_ok=True)
    output = args.output / "food-precheck.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (args.output / "food-human-review.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        fields = ["scope", "food_id", "display_name", "source_original_description", "descriptor_hints_not_verified",
                  "recorded_preparation", "recorded_preparation_origin", "source_record_sha256", "panel_sha256",
                  "human_preparation_decision", "human_edible_basis_decision", "human_recipe_decision",
                  "source_location", "reviewer", "review_date", "reason"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in result["records"]:
            writer.writerow({**{k: row[k] for k in fields if k in row},
                "descriptor_hints_not_verified": "; ".join(s["text"] for s in row["description_candidate_spans"])})
    files = [args.review, args.internal_panel, args.public_panel, Path(__file__),
             Path(__file__).resolve().parents[1] / "nutrigenomics/food_semantic_precheck.py", output,
             args.output / "food-human-review.csv"]
    (args.output / "provenance.json").write_text(json.dumps({"human_approved": 0,
        "files": {str(p.resolve()): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("tasks", "human_approved", "by_scope", "records_with_descriptor_candidates")}))


if __name__ == "__main__":
    main()
