"""Resume exactly identical features with verified sentence hidden-state reuse."""
import os
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


if __name__ == "__main__":
    for variable in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[variable] = "1"
    if os.name == "posix":
        os.nice(10)
    from nutri_literature import operations_v6
    from nutri_literature.fast_frozen_v6 import SentenceMemoEncoder, source_sha
    from nutri_literature.db import digest, dumps
    original = operations_v6.components

    def adapted(settings, provider="union"):
        import numpy as np
        values = original(settings, provider)
        encoder = values["_encoder"]
        wrapped = SentenceMemoEncoder(encoder)
        cases = ["α Caffeine reduced IL6 expression compared with placebo.", "β Vitamin D was administered to patients. " * 75]
        checked = 0
        for text in cases:
            sentence = {"text": text, "start": 0, "end": len(text)}
            positions = [(text.index("Caffeine"), len("Caffeine"))] if "Caffeine" in text else [(text.index("Vitamin"), len("Vitamin D"))]
            entities = [{"start": a, "end": a + size, "text": text[a:a + size]} for a, size in positions]
            for subset in ((), entities, entities * 2):
                old, new = encoder.encode(text, sentence, subset), wrapped.encode(text, sentence, subset)
                assert np.array_equal(old, new), "Frozen vector equivalence failed"
                checked += 1
        print(dumps({"stage": "encoder_equivalence", "exact_array_equal_cases": checked,
                     "adapter_sha256": source_sha(), "encoder": encoder.manifest}), flush=True)
        values["_encoder"] = wrapped
        return values

    operations_v6.components = adapted
    from prepare_v6_automatic_learning import main
    main()
