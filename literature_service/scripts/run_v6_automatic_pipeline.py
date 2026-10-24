"""Sequential resumable owned batch; existing original scientific artifacts preserved."""
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "runtime/v6/refinement-r4"


def run(arguments, log):
    with (WORK / log).open("a", encoding="utf-8") as handle:
        result = subprocess.run([sys.executable, *arguments], cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError("Failed " + log + ": " + str(result.returncode))


if __name__ == "__main__":
    started = time.time()
    while not (WORK / "automatic-features/manifest.json").exists():
        if time.time() - started > 4 * 3600:
            raise TimeoutError("Automatic training features have not completed")
        time.sleep(20)
    if not (WORK / "relation-head/manifest.json").exists():
        run(["scripts/train_v6_automatic_relations.py", "--features", str(WORK / "automatic-features"),
             "--gold-export", "runtime/v6/validation/relation-refinement-training-export.json.gz",
             "--output", str(WORK / "relation-head")], "train.log")
    for mode in ("end_to_end", "gold_entities"):
        output = WORK / ("relations-" + mode)
        if not (output / "report.json").exists():
            run(["scripts/evaluate_v6_automatic_relations.py", "--models", "runtime/v6/refinement-r1/frozen-models",
                 "--head", str(WORK / "relation-head"), "--baseline-predictions", "runtime/v6/refinement-r1/development/" + mode,
                 "--mode", mode, "--output", str(output)], "evaluate-" + mode + ".log")
    (WORK / "pipeline-complete.json").write_text(json.dumps({"completed": True, "elapsed_seconds": time.time() - started}))
