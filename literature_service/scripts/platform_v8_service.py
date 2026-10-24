"""Owned resumable corpus service; pending/retry work never becomes completed."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutri_literature.collection_v8 import DEFAULT_CONFIG, atomic_json, expand
from nutri_literature.locking import lock
from nutri_literature.settings import Settings


def run(settings, config=DEFAULT_CONFIG, *, once=False):
    try:
        os.nice(10)
    except (AttributeError, PermissionError):
        pass
    state = settings.home / "v8-expansion-service.json"
    stop = settings.home / "v8-expansion-stop.json"
    value = {"pid": os.getpid(), "started_at": time.time(), "running": True,
             "stage": "starting", "default_engine": "heuristic-v1", "model_API_calls": 0}
    previous = -1
    stalls = 0
    with lock(settings.home / "locks/v8-expansion-service.lock"):
        while not stop.exists():
            try:
                result = expand(settings, config, max_batches=10,
                                min_available_gib=float(os.getenv("LITERATURE_MEMORY_RESERVE_GIB", "24")),
                                min_free_disk_gib=50)
                completed = result.get("metadata_status", {}).get("completed", 0)
                stalls = stalls + 1 if completed == previous else 0
                previous = completed
                value.update(result={k: v for k, v in result.items() if k != "missing_pmids"},
                             stage=result.get("stage", "waiting"), updated_at=time.time())
                value.pop("error", None)
                if result.get("metadata_complete"):
                    value.update(running=False, stage="metadata-completed", finished_at=time.time())
                    atomic_json(state, value)
                    return value
            except BlockingIOError:
                value.update(stage="waiting-collector-slot", updated_at=time.time())
                stalls += 1
            except Exception as exc:
                value.update(stage="waiting-retry", error=type(exc).__name__ + ": " + str(exc)[:300],
                             updated_at=time.time())
                stalls += 1
            atomic_json(state, value)
            if once:
                return value
            # A deleted citation or a persistent provider failure remains pending
            # inspection; avoid an unbounded tight retry loop against official APIs.
            time.sleep(2 if stalls == 0 else min(1800, 30 * 2 ** min(stalls, 6)))
        value.update(running=False, stage="cancelled-at-checkpoint", finished_at=time.time())
        atomic_json(state, value)
        return value


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--once", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(Settings.from_env(), a.config, once=a.once), ensure_ascii=False))


if __name__ == "__main__":
    main()
