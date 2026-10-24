"""Actual shared candidate/parser load and inference under the v6 RSS budget."""

import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    if str(ROOT) != "/home/keunsoo/projects/nutriomics-literature-v6-development":
        raise ValueError("Only the isolated Linux v6 checkout may run this smoke")
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[name] = "1"
    os.environ["LITERATURE_NLP_LOCK"] = (
        "/home/keunsoo/projects/nutriomics-literature/runtime/locks/nlp-cpu.lock"
    )
    os.environ["LITERATURE_PROCESSING_LOCK_HOME"] = (
        "/home/keunsoo/projects/nutriomics-literature/runtime/locks"
    )
    os.nice(10)
    from nutri_literature.resources_v6 import memory_budget
    from nutri_literature.operations_v6 import processing_slot
    from nutri_literature.embeddings_v6 import FrozenEncoder
    from nutri_literature.provider_v6 import SharedCandidates
    from nutri_literature.nlp_v2 import parser_for, validate_tokens
    from nutri_literature.settings import Settings
    from nutri_literature.db import dumps

    settings = Settings(ROOT / "runtime", ROOT / "config/topics.json")
    start = time.monotonic()
    with memory_budget(8) as memory, processing_slot(settings):
        models = settings.home / "v6/models"
        encoder = FrozenEncoder(models / "gene/selected_model", settings.home / "embedding-cache-v6")
        encoder.load()
        provider = SharedCandidates(models, encoder)
        provider.load()
        text = "Caffeine reduced human VDR expression."
        sentence = {"start": 0, "end": len(text), "text": text, "label": "Results"}
        tokens = validate_tokens(parser_for({}).parse(sentence), sentence)
        candidates = provider.predict(text)
        if not tokens or not candidates:
            raise ValueError("Real candidate/parser smoke produced no output")
        for mention in candidates:
            if text[mention["start"] : mention["end"]] != mention["text"]:
                raise ValueError("Real candidate/source offsets differ")
        result = {
            "passed": True,
            "elapsed_seconds": time.monotonic() - start,
            "candidates": len(candidates),
            "tokens": len(tokens),
            "provider": provider.manifest,
            "memory": memory,
            "scientific_accuracy_measured": False,
        }
    path = settings.home / "v6/validation/provider-smoke.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(result), encoding="utf-8")
    print(dumps(result))


if __name__ == "__main__":
    main()
