"""Bounded source-version-bound lexical previews prepared before HTTP readiness."""
from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path

from .explorer_context import configured_path
from .explorer_performance import (
    CACHE,
    CURRENT,
    Budget,
    QueryUnavailable,
    canonical,
    fingerprint,
    read_connection,
    selector_key,
)
from .explorer_scientific import _batch_contrast_effects, _cached_effects, _preview_key
from .settings import DATA_DIR
from .source_updates import atomic_json


def prepare_effect_preview_cache(release, application, receipt, *, seconds=120, max_selectors=128,
                                 max_rows=768, max_bytes=64 * 1024**2):
    """Only existing first-six previews; never create a persistent effect copy.

    The separate startup budget does not change HTTP query/export budgets. A
    failed preparation raises and writes ready=false before traffic is served.
    Zero source results remain zero; unknown results cannot become ready.
    """
    if CURRENT.get() is not None:
        raise ValueError("Preview preparation is startup-only, outside any HTTP request budget")
    if (application not in {"internal", "public"} or not 0 < seconds <= 600 or not 0 < max_selectors <= 128
            or not 0 < max_rows <= 768 or not 0 < max_bytes <= 64 * 1024**2):
        raise ValueError("Startup preview bounds exceed the protected limits")
    release, receipt = Path(release).resolve(), Path(receipt)
    projection = release / ("nutriomics.sqlite3" if application == "public" else "explorer.sqlite")
    core = projection if application == "public" else configured_path(
        "NUTRIGENOMICS_EXPLORER_CORE_DATABASE", DATA_DIR / "db/nutrigenomics.sqlite3")
    started = time.monotonic()
    report = {"ready": False, "application": application, "budget_seconds": seconds,
        "bounds": {"selectors": max_selectors, "rows": max_rows, "serialized_bytes": max_bytes},
        "selection": "Existing lexical first six source features per registered contrast; not significance-selected",
        "cache_storage": "Process memory only; no source/effect matrix/analysis changes",
        "expert_review_status": "pending", "http_slo_evidence": False}
    budget = Budget(seconds)
    token = CURRENT.set(budget)
    try:
        if not projection.is_file():
            raise QueryUnavailable("source_unavailable")
        projection_identity = fingerprint(projection)
        with read_connection(projection) as db:
            release_id = json.loads(db.execute("SELECT value_json FROM meta WHERE key='release_id'").fetchone()[0])
            manifest = json.loads((release / "manifest.json").read_text(encoding="utf-8"))
            if release_id != manifest["release_id"]:
                raise QueryUnavailable("analysis_key_mismatch")
            report.update(release_id=release_id, projection_sha256_verified_at_startup=manifest["database_sha256"],
                          projection_fingerprint=projection_identity)
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='science_record'").fetchone():
                report.update(ready=True, availability="no_registered_contrasts", selectors=0, rows=0, serialized_bytes=0)
            else:
                contrasts = [json.loads(row[0]) for row in db.execute("SELECT metadata_json FROM science_record WHERE kind='contrast' ORDER BY id")]
                selected = {}
                for contrast in contrasts:
                    if any(contrast.get(key) is None for key in ("series_accession", "contrast", "analysis_version")):
                        raise QueryUnavailable("analysis_key_mismatch")
                    selected.setdefault(selector_key(contrast), contrast)
                if len(selected) > max_selectors:
                    raise ValueError("Registered selectors exceed the bounded startup preview capacity")
                report["registered_contrast_records"] = len(contrasts)
                if not selected:
                    report.update(ready=True, availability="no_registered_contrasts", selectors=0, rows=0, serialized_bytes=0)
                else:
                    if not Path(core).is_file():
                        raise QueryUnavailable("source_unavailable")
                    core_identity = fingerprint(core)
                    report["core_fingerprint"] = core_identity
                    batch = _batch_contrast_effects(core, list(selected.values()), None, db, store_cache=False)
                    prepared, features, totals, size, row_count = [], Counter(), {}, 0, 0
                    for selector, contrast in selected.items():
                        if budget.expired():
                            raise QueryUnavailable("query_timeout")
                        key = _preview_key(core_identity, projection_identity, contrast, None, 6)
                        result = batch.get(key) if batch is not None else _cached_effects(core, contrast, None, 6, db)
                        if result is None or result[2] != "available":
                            report["failed_selector"] = selector
                            raise QueryUnavailable(result[2] if result is not None else "analysis_key_mismatch")
                        rows, total, _ = result
                        if len(rows) > 6 or not isinstance(total, int) or total < len(rows):
                            raise ValueError("Preview count or lexical bound is invalid")
                        seen = set()
                        for row in rows:
                            keys = ("series_accession", "feature_type", "feature_id", "contrast", "analysis_version")
                            native = tuple(row.get(key) for key in keys)
                            if (any(row.get(key) != contrast[key] for key in ("series_accession", "contrast", "analysis_version"))
                                    or any(row.get("key", {}).get(key) != row.get(key) for key in keys)
                                    or any(value is None for value in native) or native in seen):
                                raise QueryUnavailable("analysis_key_mismatch")
                            seen.add(native)
                            features[row["feature_type"]] += 1
                            # Original permission, P/FDR, effect and missingness
                            # fields remain in the serialized native record.
                        encoded = canonical(result).encode()
                        size += len(encoded)
                        row_count += len(rows)
                        if size > max_bytes or row_count > max_rows:
                            raise ValueError("Native preview payload exceeds protected memory bounds")
                        prepared.append((key, encoded))
                        totals[selector] = total
                    if fingerprint(core) != core_identity or fingerprint(projection) != projection_identity:
                        raise QueryUnavailable("source_changed")
                    if budget.expired():
                        raise QueryUnavailable("query_timeout")
                    # Publish no entries until the entire bounded source set
                    # and both DB/WAL fingerprints have passed validation.
                    for key, encoded in prepared:
                        CACHE.put(key, encoded)
                    report.update(ready=True, availability="prepared", selectors=len(selected), rows=row_count,
                        serialized_bytes=size, by_native_feature_type=dict(features), exact_source_totals=totals,
                        sources_unchanged=True, gene_filtered_previews="dynamic; not precomputed")
        report["preparation_seconds"] = time.monotonic() - started
        atomic_json(receipt, report)
        return report
    except Exception as error:
        report.update(ready=False, preparation_seconds=time.monotonic() - started,
                      error_type=type(error).__name__, error=str(error))
        atomic_json(receipt, report)
        raise
    finally:
        CURRENT.reset(token)
