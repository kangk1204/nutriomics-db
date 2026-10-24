"""Read-only compact status for the owned refinement batches."""
import json
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]


if __name__ == "__main__":
    for relative in ("runtime/v6/refinement-r4/train.log", "runtime/v6/refinement-r4/identity-pipeline-final.log"):
        path = ROOT / relative
        if not path.exists():
            continue
        stages = []
        for line in path.read_text(errors="replace").splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if value.get("stage") in {"automatic_relation_oof", "absolute_id_oof"}:
                item = {"stage": value["stage"], "C": value["C"]}
                if value.get("configurations"):
                    item["best"] = max(value["configurations"], key=lambda r: r["f1"])
                stages.append(item)
        print(json.dumps({"log": relative, "completed_training_stages": stages}))
    for relative in ("runtime/v6/refinement-r4/relations-end_to_end", "runtime/v6/refinement-r4/relations-gold_entities",
                     "runtime/v6/refinement-r5/relations-end_to_end"):
        path = ROOT / relative / "report.json"
        if path.exists():
            report = json.loads(path.read_bytes())
            print(json.dumps({"report": relative, "metrics": {k: v["relation"] for k, v in report["metrics"].items()}}))
        else:
            print(json.dumps({"report": relative, "saved_predictions": len(list((ROOT / relative).glob("paper-*.json")))}))
