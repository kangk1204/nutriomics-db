"""Attach hash-verified external audits without rewriting signed analysis files.

Manifest format: {"version": "...", "analyses": [{"accession": "...",
"analysis_version": "...", "audits": [{"source_path": "...", "sha256": "...",
"source_scope": "sample_scope|reactome_release", "source_url": "optional"}],
"sample_scope": {"optional": "overall fitted-model counts"},
"contrast_overrides": {"exact contrast name or ID": {"sample_scope": {...}}}}]}.

Original metadata, completion markers, approvals, and audit source files are
read-only. New audit identities are additive, and existing identities are immutable.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from nutrigenomics.bridge_models import StudyContrast
from nutrigenomics.db import get_session_factory
from nutrigenomics.models import AnalysisRun

MAX_JSON_BYTES = 20 * 1024 * 1024


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value


def _safe_path(value: Any, base: Path, allowed_roots: tuple[Path, ...]) -> Path:
    path = Path(_text(value, "source_path")).expanduser()
    if not path.is_absolute():
        path = base / path
    resolved = path.resolve(strict=True)
    if not resolved.is_file() or not any(resolved.is_relative_to(root) for root in allowed_roots):
        raise ValueError(f"Audit source is outside approved project roots: {resolved}")
    return resolved


def _read_snapshot(path: Path, expected_sha: str | None = None) -> tuple[dict, str]:
    if path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError("Audit JSON exceeds the bounded snapshot size")
    blob = path.read_bytes()
    digest = hashlib.sha256(blob).hexdigest()
    if expected_sha is not None and digest != expected_sha:
        raise ValueError(f"Audit source SHA-256 mismatch: {path}")
    payload = json.loads(
        blob,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"Non-finite JSON value: {value}")
        ),
    )
    if not isinstance(payload, dict):
        raise TypeError("Audit JSON must contain an object")
    return payload, digest


def _scope(value: Any, label: str) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{label} must be a nonempty object")
    return copy.deepcopy(value)


def _audit_identity(source_scope: str, source_sha: str) -> str:
    return hashlib.sha256((source_scope + ":" + source_sha).encode()).hexdigest()


def _verify_unchanged(files: dict[Path, str], allowed_roots: tuple[Path, ...]) -> None:
    for path, expected in files.items():
        if _safe_path(str(path), path.parent, allowed_roots) != path:
            raise ValueError(f"Audit source path changed during attachment: {path}")
        _, actual = _read_snapshot(path, expected)
        if actual != expected:
            raise ValueError(f"Audit source changed during attachment: {path}")


def _merge_metadata(
    metadata: dict | None, audits: list[dict], sample_scope: dict | None, manifest_record: dict
) -> tuple[dict, int, bool]:
    merged = copy.deepcopy(metadata or {})
    attachments = merged.setdefault("audit_attachments", {})
    if not isinstance(attachments, dict):
        raise TypeError("Existing audit_attachments must be an identity-keyed object")
    inserted = 0
    for source in audits:
        identity = _audit_identity(source["source_scope"], source["source_sha256"])
        existing = attachments.get(identity)
        # Manifest provenance belongs to the first attachment. A later manifest
        # may repeat an identical source while adding other analyses, but cannot
        # alter facts or their scope under an already published source identity.
        facts = {**source, "sample_scope": copy.deepcopy(sample_scope)}
        if existing is not None:
            recorded_facts = {
                key: value
                for key, value in existing.items()
                if key not in {"id", "attachment_manifest"}
            }
            if recorded_facts != facts:
                raise ValueError("Immutable audit identity has conflicting facts or source scope")
            continue
        attachments[identity] = {
            "id": identity,
            **facts,
            "attachment_manifest": copy.deepcopy(manifest_record),
        }
        inserted += 1
    if sample_scope is not None:
        if "sample_scope" in merged and merged["sample_scope"] != sample_scope:
            raise ValueError("Existing sample_scope cannot be overwritten by an additive audit")
        merged["sample_scope"] = copy.deepcopy(sample_scope)
    return merged, inserted, merged != (metadata or {})


def run(manifest_path: Path, database_url: str, allowed_roots: list[Path]) -> dict:
    """Atomically attach audits to exact analysis versions and their contrasts."""
    if not allowed_roots:
        raise ValueError("At least one approved project root is required")
    roots = tuple(Path(root).expanduser().resolve(strict=True) for root in allowed_roots)
    if any(not root.is_dir() for root in roots):
        raise ValueError("Approved project roots must be directories")
    manifest_path = _safe_path(str(manifest_path), Path.cwd(), roots)
    manifest, manifest_sha = _read_snapshot(manifest_path)
    version = _text(manifest.get("version"), "manifest version")
    entries = manifest.get("analyses")
    if not isinstance(entries, list) or not entries:
        raise ValueError("Audit manifest must list analyses")
    verified_files = {manifest_path: manifest_sha}
    manifest_record = {
        "version": version,
        "source_path": str(manifest_path),
        "sha256": manifest_sha,
        "hash_scope": "attachment_manifest",
    }
    plans = []
    seen_analyses = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise TypeError("Each audit analysis must be an object")
        accession = _text(entry.get("accession"), "accession")
        analysis_version = _text(entry.get("analysis_version"), "analysis_version")
        key = (accession, analysis_version)
        if key in seen_analyses:
            raise ValueError("Duplicate exact analysis in audit manifest")
        seen_analyses.add(key)
        sources = entry.get("audits")
        if not isinstance(sources, list) or not sources:
            raise ValueError("Each analysis must have a hash-verified external audit")
        audits = []
        identities = set()
        for source in sources:
            if not isinstance(source, dict):
                raise TypeError("Each audit source must be an object")
            expected = _text(source.get("sha256"), "audit SHA-256")
            if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
                raise ValueError("Audit SHA-256 must be a lowercase hexadecimal digest")
            source_scope = _text(source.get("source_scope"), "source_scope")
            path = _safe_path(source.get("source_path"), manifest_path.parent, roots)
            payload, source_sha = _read_snapshot(path, expected)
            if path in verified_files and verified_files[path] != source_sha:
                raise ValueError("Conflicting hashes for one audit source")
            verified_files[path] = source_sha
            identity = _audit_identity(source_scope, source_sha)
            if identity in identities:
                raise ValueError("Duplicate audit source identity for an analysis")
            identities.add(identity)
            audits.append(
                {
                    "source_path": str(path),
                    "source_sha256": source_sha,
                    "source_scope": source_scope,
                    "source_url": source.get("source_url"),
                    "hash_scope": "external_audit_snapshot",
                    "content": payload,
                }
            )
        overrides = entry.get("contrast_overrides", {})
        if not isinstance(overrides, dict):
            raise TypeError("contrast_overrides must use exact names or IDs as keys")
        for name, override in overrides.items():
            _text(name, "contrast override key")
            if not isinstance(override, dict) or set(override) != {"sample_scope"}:
                raise ValueError("Contrast overrides must contain only a sample_scope object")
            _scope(override.get("sample_scope"), "contrast sample_scope")
        plans.append(
            {
                "accession": accession,
                "analysis_version": analysis_version,
                "audits": audits,
                "sample_scope": _scope(entry.get("sample_scope"), "sample_scope"),
                "overrides": overrides,
            }
        )
    counts = {
        "analyses_updated": 0,
        "contrasts_updated": 0,
        "audits_attached": 0,
        "unchanged_records": 0,
        "manifest_sha256": manifest_sha,
        "version": version,
    }
    with get_session_factory(database_url)() as session, session.begin():
        for plan in plans:
            target = session.scalar(
                select(AnalysisRun).where(
                    AnalysisRun.series_accession == plan["accession"],
                    AnalysisRun.analysis_version == plan["analysis_version"],
                )
            )
            if target is None:
                raise ValueError(
                    "Audit target exact accession/analysis_version does not exist: "
                    + plan["accession"]
                    + "/"
                    + plan["analysis_version"]
                )
            contrasts = list(
                session.scalars(
                    select(StudyContrast).where(
                        StudyContrast.series_accession == plan["accession"],
                        StudyContrast.analysis_version == plan["analysis_version"],
                    )
                )
            )
            by_key = {}
            for contrast in contrasts:
                for key in {contrast.id, contrast.contrast}:
                    by_key.setdefault(key, []).append(contrast)
            selected_overrides = {}
            for key, override in plan["overrides"].items():
                matches = by_key.get(key, [])
                if len(matches) != 1 or matches[0].id in selected_overrides:
                    raise ValueError(
                        "Contrast override is unknown, ambiguous or duplicated: " + key
                    )
                selected_overrides[matches[0].id] = _scope(
                    override["sample_scope"], "contrast sample_scope"
                )
            metadata, inserted, changed = _merge_metadata(
                target.metadata_json, plan["audits"], plan["sample_scope"], manifest_record
            )
            counts["audits_attached"] += inserted
            if changed:
                target.metadata_json = metadata
                counts["analyses_updated"] += 1
            else:
                counts["unchanged_records"] += 1
            for contrast in contrasts:
                metadata, inserted, changed = _merge_metadata(
                    contrast.metadata_json,
                    plan["audits"],
                    selected_overrides.get(contrast.id),
                    manifest_record,
                )
                counts["audits_attached"] += inserted
                if changed:
                    contrast.metadata_json = metadata
                    counts["contrasts_updated"] += 1
                else:
                    counts["unchanged_records"] += 1
        session.flush()
        # Hash verification brackets all mutations; a changed source or
        # manifest aborts the whole transaction, including earlier analyses.
        _verify_unchanged(verified_files, roots)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--allowed-root", type=Path, action="append", required=True)
    options = parser.parse_args()
    print(json.dumps(run(options.manifest, options.database_url, options.allowed_root), indent=2))


if __name__ == "__main__":
    main()
