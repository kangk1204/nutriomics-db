"""Source rows remain native; batch only their verified references and PK reads."""
from .explorer_performance import QueryUnavailable


def read_rows_by_ids(core, identifiers, chunk_size=400):
    """One caller-owned connection, deduplicated native IDs, sorted PK hydration."""
    if not 1 <= chunk_size <= 400:
        raise ValueError("Effect row batch size must be between one and 400")
    identifiers = sorted(set(identifiers), key=lambda item: (type(item).__name__, item))
    rows = {}
    for offset in range(0, len(identifiers), chunk_size):
        batch = identifiers[offset:offset + chunk_size]
        for row in core.execute("SELECT * FROM study_effect WHERE id IN (" + ",".join("?" for _ in batch) + ")", batch):
            rows[row["id"]] = row
    return rows


def verify_reference(row, contrast, reference):
    """Never resolve a stale row ID to another feature, contrast or version."""
    if (row is None or any(row[key] != contrast[key] for key in ("series_accession", "contrast", "analysis_version"))
            or (row["feature_type"], row["feature_id"]) != (reference["feature_type"], reference["feature_id"])):
        raise QueryUnavailable("analysis_key_mismatch")
    return row
