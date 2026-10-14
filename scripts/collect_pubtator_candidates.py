"""Freeze PubTator abstract annotations for internal review; never import evidence.

Only PubMed IDs from the local study manifest are requested. Automated mentions
are candidates, not intervention designs, doses, effect directions or p-values.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import requests

API_URL = "https://www.ncbi.nlm.nih.gov/research/pubtator3-api/publications/export/biocjson"
API_DOCS = "https://www.ncbi.nlm.nih.gov/research/pubtator3/api"
PRIMARY_PAPER = "https://pmc.ncbi.nlm.nih.gov/articles/PMC11223843/"
PIPELINE_VERSION = "pubtator-candidates-v1"
MAX_PMIDS = 32
MAX_BYTES = 10 * 1024 * 1024
UNVALIDATED_FIELDS = (
    "assigned_dose", "dose_unit", "regimen_duration_days", "response_time_h",
    "study_design", "organism", "tissue", "effect_direction", "effect_estimate",
    "p_value", "q_value", "health_effect", "personal_prediction",
)
SIGNALS = {
    "quantity_mention": re.compile(
        r"(?<![\w.])\d+(?:\.\d+)?(?:\s*[/–-]\s*\d+(?:\.\d+)?)*\s*"
        r"(?:kcal|mg|µg|μg|ug|kg|g|mL|ml|hours?|hrs?|h|days?|weeks?|months?|years?)\b",
        re.IGNORECASE,
    ),
    "design_mention": re.compile(
        r"\b(?:randomi[sz]ed|cross[ -]?over|double[ -]blind|single[ -]blind|"
        r"placebo[ -]controlled|controlled trial)\b", re.IGNORECASE,
    ),
    "statistical_mention": re.compile(
        r"\b[pq]\s*(?:value\s*)?[<=>≤≥]\s*(?:0?\.\d+|\d+(?:\.\d+)?)",
        re.IGNORECASE,
    ),
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def read_studies(config_path: Path, max_pmids: int = MAX_PMIDS) -> tuple[dict, dict]:
    if not 1 <= max_pmids <= MAX_PMIDS:
        raise ValueError(f"max_pmids must be between 1 and {MAX_PMIDS}")
    raw = config_path.read_bytes()
    config = json.loads(raw)
    pmid_studies: dict[str, list[str]] = {}
    missing = []
    for study in config["studies"]:
        pmid = str(study.get("pubmed_id") or "").strip()
        study_id = str(study["id"])
        if not pmid:
            missing.append(study_id)
            continue
        if not re.fullmatch(r"[1-9]\d*", pmid):
            raise ValueError(f"Invalid PMID for study {study_id}")
        ids = pmid_studies.setdefault(pmid, [])
        if study_id not in ids:
            ids.append(study_id)
    if len(pmid_studies) > max_pmids:
        raise ValueError("Study manifest exceeds bounded PMID limit; nothing was fetched")
    if not pmid_studies:
        raise ValueError("No PubMed IDs in study manifest")
    return pmid_studies, {
        "path": str(config_path), "sha256": sha256(raw),
        "version": config.get("version"), "studies_without_pmid": missing,
    }


def fetch_response(client, pmids: list[str], max_bytes: int = MAX_BYTES) -> tuple[bytes, dict]:
    """One bounded GET for public abstracts, with no submission or full-text request."""
    response = client.get(
        API_URL, params={"pmids": ",".join(pmids), "full_text": "false"},
        timeout=(10, 45), stream=True,
        headers={"Accept": "application/json", "User-Agent": "NutrigenomicsResearch/0.1 PubTatorCandidateCollector"},
    )
    try:
        response.raise_for_status()
        chunks, size = [], 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            size += len(chunk)
            if size > max_bytes:
                raise ValueError("PubTator response exceeded byte limit")
            chunks.append(chunk)
        raw = b"".join(chunks)
        json.loads(raw)  # Fail before freezing a successful but non-JSON response.
        return raw, {
            "url": response.url, "http_status": response.status_code,
            "retrieved_at": utc_now(), "bytes": size, "sha256": sha256(raw),
            "headers": {key: response.headers[key] for key in (
                "Content-Type", "ETag", "Last-Modified",
            ) if key in response.headers},
            "request": {"pmids": pmids, "full_text": False},
            "export_allowed": False, "access_scope": "internal_research_review",
            "rights_review_status": "pending_per_article_source_rights_review",
        }
    finally:
        response.close()


def documents_from_response(raw: bytes) -> list[dict]:
    payload = json.loads(raw)
    if isinstance(payload, list):
        documents = payload
    elif isinstance(payload, dict):
        documents = payload.get("PubTator3", payload.get("documents"))
    else:
        documents = None
    if not isinstance(documents, list) or any(not isinstance(doc, dict) for doc in documents):
        raise ValueError("Unrecognized PubTator BioCJSON document container")
    return documents


def _source(source: dict, pmid: str, passage_index: int, offset: int) -> dict:
    return {
        "snapshot_sha256": source["sha256"], "snapshot_path": source.get("path"),
        "source_url": source["url"], "pmid": pmid, "passage_index": passage_index,
        "passage_offset": offset, "offset_basis": "original_BioC_document_characters",
        "retrieved_at": source["retrieved_at"],
    }


def extract_candidates(raw: bytes, pmid_studies: dict, source: dict) -> dict:
    if sha256(raw) != source["sha256"]:
        raise ValueError("Source response hash does not match manifest")
    documents, seen, excluded = [], set(), []
    for doc in documents_from_response(raw):
        pmid = str(doc.get("id") or "")
        if pmid not in pmid_studies:
            excluded.append({"document_id": pmid, "reason": "not_a_requested_pmid"})
            continue
        if pmid in seen:
            raise ValueError(f"Duplicate PubTator document for PMID {pmid}")
        seen.add(pmid)
        passages, entities, mentions, relations = [], [], [], []
        for passage_index, passage in enumerate(doc.get("passages", [])):
            section = str((passage.get("infons") or {}).get("type") or "").lower()
            if section not in {"title", "abstract"}:
                excluded.append({"document_id": pmid, "passage_index": passage_index,
                                 "reason": "not_title_or_abstract"})
                continue
            text = str(passage.get("text") or "")
            offset = passage.get("offset")
            if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
                raise ValueError(f"Invalid passage offset in PMID {pmid}")
            if len(text) > 200_000:
                raise ValueError("Passage exceeded character bound")
            provenance = _source(source, pmid, passage_index, offset)
            passages.append({"type": section, "offset": offset, "text": text,
                             "provenance": provenance, "export_allowed": False})
            for annotation_index, annotation in enumerate(passage.get("annotations", [])):
                locations, spans = annotation.get("locations") or [], []
                valid = bool(locations)
                for location in locations:
                    start, length = location.get("offset"), location.get("length")
                    location_valid = all(isinstance(x, int) and not isinstance(x, bool) for x in (start, length))
                    location_valid = location_valid and length >= 0 and offset <= start <= start + length <= offset + len(text)
                    valid = valid and location_valid
                    spans.append(text[start-offset:start-offset+length] if location_valid else None)
                mention_text = str(annotation.get("text") or "")
                exact = valid and "".join(spans) == mention_text
                entities.append({
                    "id": f"PT3:{pmid}:entity:{passage_index}:{annotation_index}",
                    "annotation_id": annotation.get("id"), "mention_text": mention_text,
                    "locations": locations, "source_span_text": spans,
                    "span_check": "exact" if exact else "requires_review",
                    "reported_identifiers": annotation.get("infons") or {},
                    "review_status": "candidate", "normalization_verified": False,
                    "provenance": provenance, "export_allowed": False,
                })
            for kind, pattern in SIGNALS.items():
                for match in pattern.finditer(text):
                    mentions.append({
                        "id": f"PT3:{pmid}:{kind}:{passage_index}:{match.start()}",
                        "kind": kind, "mention_text": match.group(),
                        "offset": offset + match.start(), "length": match.end() - match.start(),
                        "review_status": "candidate", "assigned_to_intervention": False,
                        "validated_value": None, "provenance": provenance, "export_allowed": False,
                    })
            for relation_index, relation in enumerate(passage.get("relations", [])):
                relations.append({"id": f"PT3:{pmid}:relation:{passage_index}:{relation_index}",
                                  "reported_relation": relation, "review_status": "candidate",
                                  "causal_or_health_claim": None, "provenance": provenance,
                                  "export_allowed": False})
        for relation_index, relation in enumerate(doc.get("relations", [])):
            relations.append({
                "id": f"PT3:{pmid}:relation:document:{relation_index}",
                "reported_relation": relation, "review_status": "candidate",
                "causal_or_health_claim": None, "export_allowed": False,
                "provenance": {
                    "snapshot_sha256": source["sha256"], "snapshot_path": source.get("path"),
                    "source_url": source["url"], "pmid": pmid,
                    "passage_index": None, "passage_offset": None,
                    "offset_basis": "document_relation_no_character_span",
                    "retrieved_at": source["retrieved_at"],
                },
            })
        documents.append({
            "pmid": pmid, "study_ids": pmid_studies[pmid],
            "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            "pubtator_url": f"https://www.ncbi.nlm.nih.gov/research/pubtator3/publication/{pmid}",
            "review_status": "candidate", "expert_review_status": "pending",
            "export_allowed": False, "production_import_allowed": False,
            "validated_interpretation": dict.fromkeys(UNVALIDATED_FIELDS),
            "passages": passages, "entity_candidates": entities,
            "mention_candidates": mentions, "relation_candidates": relations,
        })
    return {"version": PIPELINE_VERSION, "review_status": "candidate",
            "export_allowed": False, "production_import_allowed": False,
            "documents": documents, "missing_pmids": sorted(set(pmid_studies)-seen),
            "excluded_records": excluded}


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def collect(config_path: Path, output: Path, client=None, reuse: bool = False) -> dict:
    """Reuse is hash verification only; refreshed sources require a new directory."""
    pmid_studies, config_source = read_studies(config_path)
    manifest_path = output / "manifest.json"
    if manifest_path.exists():
        if not reuse:
            raise FileExistsError("Frozen output exists; use --reuse or a new output directory")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["study_config"]["sha256"] != config_source["sha256"]:
            raise ValueError("Study manifest changed; use a new output directory")
        for artifact in manifest["artifacts"]:
            relative = Path(artifact["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Unsafe artifact path")
            if sha256((output / relative).read_bytes()) != artifact["sha256"]:
                raise ValueError("Frozen artifact hash mismatch")
        return manifest["summary"]
    if output.exists() and any(output.iterdir()):
        raise FileExistsError("Output directory is nonempty; preserve it and use a new directory")
    session = client or requests.Session()
    try:
        raw, source = fetch_response(session, sorted(pmid_studies))
    finally:
        if client is None:
            session.close()
    source["path"] = f"private/{source['sha256']}.biocjson"
    candidates = extract_candidates(raw, pmid_studies, source)
    output.mkdir(parents=True, exist_ok=True)
    private = output / source["path"]
    private.parent.mkdir(parents=True, exist_ok=True)
    private.write_bytes(raw)
    write_json(output / "private/reviewer_candidates.json", candidates)
    entity_types = Counter(str(e["reported_identifiers"].get("type") or "unreported")
                           for d in candidates["documents"] for e in d["entity_candidates"])
    summary = {
        "version": PIPELINE_VERSION, "status": "candidate_only",
        "pmids_requested": len(pmid_studies), "documents_returned": len(candidates["documents"]),
        "missing_pmids": candidates["missing_pmids"], "entity_types": dict(entity_types),
        "entity_candidates": sum(len(d["entity_candidates"]) for d in candidates["documents"]),
        "mention_candidates": sum(len(d["mention_candidates"]) for d in candidates["documents"]),
        "relation_candidates": sum(len(d["relation_candidates"]) for d in candidates["documents"]),
        "validated_scientific_assertions": 0, "production_records_written": 0,
        "personal_prediction": None, "export_allowed": True,
        "source_url": source["url"], "response_sha256": source["sha256"],
        "api_docs": API_DOCS, "primary_paper": PRIMARY_PAPER,
    }
    write_json(output / "public_summary.json", summary)
    artifacts = [{"path": str(p.relative_to(output)).replace("\\", "/"),
                  "sha256": sha256(p.read_bytes()), "export_allowed": p.name == "public_summary.json"}
                 for p in (private, output / "private/reviewer_candidates.json", output / "public_summary.json")]
    write_json(manifest_path, {
        "version": PIPELINE_VERSION, "created_at": utc_now(), "study_config": config_source,
        "collector_sha256": sha256(Path(__file__).read_bytes()), "source": source,
        "artifacts": artifacts, "summary": summary,
        "rights_review_status": "pending_per_article_source_rights_review",
        "automatic_promotion_supported": False,
    })
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/human_exposures.v1.json"))
    parser.add_argument("--output", type=Path, default=Path("research/2026-10-01/pubtator/collection-v1"))
    parser.add_argument("--reuse", action="store_true")
    args = parser.parse_args()
    print(json.dumps(collect(args.config, args.output, reuse=args.reuse), ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
