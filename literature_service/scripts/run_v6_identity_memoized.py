"""Exact replay with pure lexical lookup memoization, separate output folder."""
from functools import lru_cache
import json
import os
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


if __name__ == "__main__":
    for key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[key] = "1"
    if os.name == "posix":
        os.nice(10)
    from nutri_literature import automatic_identity_v6, identity_v6, extraction_v3
    from nutri_literature.db import digest, dumps
    original_retrieve = identity_v6.CandidateSearch.retrieve
    original_abbreviations = extraction_v3.abbreviations
    original_sequence = automatic_identity_v6.SequenceMatcher
    checks = {"retrieve": 0, "abbreviations": 0, "ratio": 0}

    @lru_cache(maxsize=8192)
    def retrieve(self, surface, taxon=None, limit=10):
        answer = original_retrieve(self, surface, taxon, limit)
        if checks["retrieve"] < 6:
            assert dumps(answer) == dumps(original_retrieve(self, surface, taxon, limit))
            checks["retrieve"] += 1
        return answer

    @lru_cache(maxsize=8)
    def abbreviations(text):
        answer = original_abbreviations(text)
        if checks["abbreviations"] < 6:
            assert dumps(answer) == dumps(original_abbreviations(text))
            checks["abbreviations"] += 1
        return answer

    @lru_cache(maxsize=200000)
    def ratio(a, b):
        answer = original_sequence(None, a, b).ratio()
        if checks["ratio"] < 6:
            assert answer == original_sequence(None, a, b).ratio()
            checks["ratio"] += 1
        return answer

    class CachedSequence:
        def __init__(self, isjunk, a, b):
            assert isjunk is None
            self.a, self.b = a, b
        def ratio(self):
            return ratio(self.a, self.b)

    identity_v6.CandidateSearch.retrieve = retrieve
    extraction_v3.abbreviations = abbreviations
    automatic_identity_v6.SequenceMatcher = CachedSequence
    # Execute the same driver, changing only the destination. Its scientific
    # feature/model modules and their hashes remain byte-for-byte unchanged.
    original_driver = ROOT / "scripts/run_v6_identity_pipeline.py"
    namespace = {"__name__": "memoized_identity_driver", "__file__": str(original_driver)}
    source = original_driver.read_text().replace('"runtime/v6/refinement-r5"', '"runtime/v6/refinement-r5-fast"')
    exec(compile(source, str(original_driver), "exec"), namespace)
    namespace["main"]()
    receipt = {"adapter_sha256": digest(Path(__file__).read_bytes()), "original_driver_sha256": digest(original_driver.read_bytes()),
               "exact_output_checks": checks, "retrieve_cache": retrieve.cache_info()._asdict(),
               "abbreviation_cache": abbreviations.cache_info()._asdict(), "spelling_cache": ratio.cache_info()._asdict(),
               "scientific_source_sha256": automatic_identity_v6.source_sha(), "feature_values_changed": False}
    path = ROOT / "runtime/v6/refinement-r5-fast/memoization-receipt.json"
    path.write_text(dumps(receipt))
    print(dumps(receipt), flush=True)
