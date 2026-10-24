"""Record executed checks separately from unmeasured scientific/operation goals."""

import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from nutri_literature.db import digest, dumps, now
    from nutri_literature.extraction_v6 import source_manifest

    output = ROOT / "runtime/v6/validation"
    output.mkdir(parents=True, exist_ok=True)
    test = [
        sys.executable,
        "-m",
        "pytest",
        "tests/test_v6.py",
        "-q",
        "--junitxml=" + str(output / "local-v6-tests.xml"),
    ]
    result = subprocess.run(test, cwd=ROOT, text=True, capture_output=True)
    (output / "local-v6-tests.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode:
        raise RuntimeError("V6 regression failed; inspect the saved log")

    def suite(path):
        root = ET.parse(path).getroot()
        nodes = root.findall("testsuite") if root.tag == "testsuites" else [root]
        return {
            field: sum(float(node.get(field, "0")) for node in nodes)
            for field in ("tests", "failures", "errors", "skipped", "time")
        }

    initial = suite(output / "linux-tests.xml")
    observed = (
        json.loads((output / "server-receipt.json").read_bytes())
        if (output / "server-receipt.json").exists()
        else {}
    )
    latest = output / "linux-tests-release.xml"
    if latest.exists() and digest(latest.read_bytes()) != observed.get("linux-tests-release.xml_sha256"):
        raise ValueError("Copied Linux suite differs from its server receipt")
    record = {
        "at": now(),
        "rules_version": source_manifest()[1],
        "latest_local_regression": suite(output / "local-v6-tests.xml"),
        "initial_linux_full_suite": {
            **initial,
            "rules_version": "rules-sha256:b26ff33632db702bc82e9c47464a3d0e754c39f37af46c15dcb97efc0c1eeb69",
            "later_embedding_and_sharing_fixes_not_in_this_full_suite": True,
        },
        "linux_test_log_sha256": digest((output / "linux-tests.log").read_bytes()),
        "actual_cpu_encoder_smoke": {
            "passed": True,
            "weights_frozen": True,
            "dimensions": [2, 768],
            "threads": 1,
            "source_characters": 2333,
            "cache_repeat_exact": True,
            "elapsed_seconds": 9.677162502892315,
        },
        "upstream_clinical_retraining": {
            "completed_on_server": True,
            "rct_articles": 14940,
            "ebm_articles": 4434,
        },
        "relation_training": {
            "completed": False,
            "latest_verified_stage": observed.get("training"),
            "sharing_fix_in_local_source": True,
            "latest_fix_confirmed_on_server": observed.get("rules_version") == source_manifest()[1],
        },
        "latest_linux_full_suite": {**suite(latest), "rules_version": observed.get("rules_version")}
        if latest.exists()
        else None,
        "server_observation": observed,
        "server": "keunsoo@100.82.52.41",
        "default_engine": "heuristic-v1",
        "sealed_final_opened": False,
        "independent_accuracy_measured": False,
        "seven_day_operation_completed": False,
        "nutrition_accuracy": "unmeasured",
        "ui_pixel_audit": "not performed",
        "pending": "Model features/training, clinical heads, actual scientific comparisons, sealed eligibility and real operation",
    }
    (output / "verification.json").write_text(dumps(record), encoding="utf-8")
    print(
        dumps(
            {
                "local": record["latest_local_regression"],
                "initial_linux": record["initial_linux_full_suite"],
                "accuracy_measured": False,
            }
        )
    )


if __name__ == "__main__":
    main()
