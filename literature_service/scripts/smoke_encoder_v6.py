"""Verify real frozen CPU vectors/cache/Unicode offsets without fitting or labels."""

import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    if str(ROOT) != "/home/keunsoo/projects/nutriomics-literature-v6-development":
        raise ValueError("Pinned Linux CPU smoke only")
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[name] = "1"
    os.environ["LITERATURE_NLP_LOCK"] = (
        "/home/keunsoo/projects/nutriomics-literature/runtime/locks/nlp-cpu.lock"
    )
    os.environ["LITERATURE_PROCESSING_LOCK_HOME"] = (
        "/home/keunsoo/projects/nutriomics-literature/runtime/locks"
    )
    os.nice(10)
    from nutri_literature.resources_v5 import memory_budget
    from nutri_literature.operations_v6 import processing_slot
    from nutri_literature.embeddings_v6 import FrozenEncoder
    from nutri_literature.settings import Settings
    from nutri_literature.db import dumps
    import numpy as np

    start = time.monotonic()
    settings = Settings(ROOT / "runtime", ROOT / "config/topics.json")
    with memory_budget(8), processing_slot(settings):
        encoder = FrozenEncoder(
            Path(
                "/home/keunsoo/projects/nutriomics-literature-v5-development/runtime/models/gene/selected_model"
            ),
            settings.home / "embedding-cache-v6-smoke",
        )
        line = "α human VDR expression " * 100 + " caffeine reduced VDR expression."
        sentence = {"start": 0, "end": len(line), "text": line}
        start_entity = line.rindex("VDR")
        mention = {"start": start_entity, "end": start_entity + 3, "text": "VDR"}
        vector = encoder.encode(line, sentence, (mention,))
        cached = encoder.encode(line, sentence, (mention,))
        if vector.shape != (2, 768) or not np.isfinite(vector).all() or not np.array_equal(vector, cached):
            raise ValueError("Real frozen-vector coverage/cache smoke failed")
        import torch

        record = {
            "passed": True,
            "device": "cpu",
            "shape": list(vector.shape),
            "weights_updated": False,
            "frozen_parameters": all(not p.requires_grad for p in encoder.model.parameters()),
            "source_characters": len(line),
            "final_entity_start": start_entity,
            "cache_repeat_exact": True,
            "threads": torch.get_num_threads(),
            "encoder": encoder.manifest,
            "elapsed_seconds": time.monotonic() - start,
            "scientific_accuracy_measured": False,
        }
    target = settings.home / "v6/validation/encoder-smoke.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dumps(record), encoding="utf-8")
    print(dumps(record))


if __name__ == "__main__":
    main()
