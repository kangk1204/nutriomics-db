"""Compact observed receipts/status; do not turn running jobs into measurements."""

from collections import deque
import json
from pathlib import Path
import socket
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from nutri_literature import evaluation_v6
    from nutri_literature.db import digest, now
    from nutri_literature.settings import Settings

    home = ROOT / "runtime"
    settings = Settings(home, ROOT / "config/topics.json")
    record = {"at": now(), "host": socket.gethostname(), "default_engine": "heuristic-v1",
              "sealed_test_opened": False, "nutrition_accuracy": "unmeasured"}
    for name in ("development-state", "evidence-pipeline-state"):
        path = home / ("v6/" + name + ".json")
        record[name] = json.loads(path.read_bytes()) if path.exists() else None
    record["models"] = {}
    for family in ("models", "models-evidence"):
        for kind in ("learned-v6", "clinical", "clinical-v6-outcome", "clinical-v6-evidence"):
            path = home / "v6" / family / kind / "manifest.json"
            if not path.exists():
                record["models"][family + "/" + kind] = {"complete": False}
                continue
            value = json.loads(path.read_bytes())
            valid = all(Path(name).name == name and digest((path.parent / name).read_bytes()) == sha
                        for name, sha in value.get("files", {}).items())
            record["models"][family + "/" + kind] = {
                "complete": valid, "manifest_sha256": digest(path.read_bytes()),
                "training_articles": len(value["training_pmids"])
                if isinstance(value.get("training_pmids"), list)
                else {k: len(v) for k, v in value.get("training_pmids", {}).items()},
                "selection": value.get("selection"),
                "numeric_rows": value.get("numeric_features", {}).get("rows"),
                "validation_scope": "training OOF; not development or nutrition accuracy"}
    record["development_reports"] = [
        {k: value[k] for k in ("id", "at", "corpus", "mode", "provider", "source_level", "papers", "metrics")}
        for value in evaluation_v6.reports(settings)["items"]
    ]
    log = home / "logs/v6-clinical-outcome.log"
    if log.exists():
        with log.open() as stream:
            lines = list(deque(stream, maxlen=100))
        events = [json.loads(line) for line in lines if line.startswith('{"articles":')]
        record["outcome_feature_progress"] = events[-1] if events else None
    path = home / "v6/validation/continuation-latest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    print(json.dumps(record))


if __name__ == "__main__":
    main()
