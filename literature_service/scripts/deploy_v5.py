"""Install additive v5 API/UI code after tests; preserve every replaced file."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import urllib.request


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--main", type=Path, required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    target = args.main.resolve(strict=True)
    from nutri_literature.extraction_v4 import source_manifest

    frozen, rules = source_manifest()
    if any(sha(target / name) != value for name, value in frozen.items()):
        raise SystemExit("Existing frozen science differs; do not overwrite it")
    files = [str(p.relative_to(source)) for p in (source / "nutri_literature").glob("*_v5.py")]
    files += [
        "nutri_literature/" + name + ".py"
        for name in ("api", "pipeline", "reporting", "review_eligibility", "cli")
    ]
    files += ["ui/app.py", "requirements-v5.lock", "scripts/service_watchdog.sh"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = target / "runtime/backups" / ("source-pre-v5-" + stamp)
    backup.mkdir(parents=True, exist_ok=False)
    previous = {}
    for name in files:
        path = target / name
        if path.exists():
            saved = backup / name
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, saved)
            previous[name] = sha(path)
    record = {
        "at": stamp,
        "frozen_rules": rules,
        "backup": str(backup),
        "previous_hashes": previous,
        "installed_hashes": {name: sha(source / name) for name in files},
    }
    env = {**os.environ, "LITERATURE_HOME": str(target / "runtime")}
    ctl = [str(target / ".venv/bin/supervisorctl"), "-c", str(target / "scripts/supervisord.conf")]
    try:
        for name in files:
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".v5tmp")
            shutil.copy2(source / name, temporary)
            temporary.replace(path)
        subprocess.run(ctl + ["restart", "literature_api", "literature_ui"], env=env, cwd=target, check=True)
        with urllib.request.urlopen("http://127.0.0.1:8010/healthz", timeout=15) as response:
            health = json.load(response)
        if health["engine"] != "heuristic-v1" or "heuristic-v5" not in health["engines"]:
            raise ValueError("Unexpected default engine or missing additive v5 API")
        if any(sha(target / name) != value for name, value in frozen.items()):
            raise ValueError("Frozen source changed during deployment")
        record.update(passed=True, health=health)
    except BaseException:
        for name in previous:
            shutil.copy2(backup / name, target / name)
        subprocess.run(ctl + ["restart", "literature_api", "literature_ui"], env=env, cwd=target, check=False)
        raise
    out = target / "runtime/experiments/v5/deployment.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("x", encoding="utf-8") as stream:
        json.dump(record, stream, indent=2)
    print(json.dumps(record), flush=True)


if __name__ == "__main__":
    main()
