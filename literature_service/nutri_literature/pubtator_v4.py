"""Optional external entity annotations with exact source mapping and raw hashes.

External relation annotations are never converted into own predicted events.
"""
import json
from pathlib import Path
import re
from .db import digest, dumps, now

URL = "https://www.ncbi.nlm.nih.gov/research/pubtator3-api/publications/export/biocjson"


def align(document, text):
    entities = []
    kinds = {"Gene": "gene", "Chemical": "component", "Disease": "outcome", "Species": "organism"}
    for passage in document.get("passages", []):
        source = passage.get("text", "")
        if not source:
            continue
        positions = [m.start() for m in re.finditer(re.escape(source), text)]
        if len(positions) != 1:
            raise ValueError("PubTator passage cannot be uniquely aligned to stored source")
        offset = positions[0] - passage["offset"]
        for annotation in passage.get("annotations", []):
            infons = annotation.get("infons", {})
            if infons.get("type") not in kinds:
                continue
            locations = annotation.get("locations", [])
            if len(locations) != 1:
                raise ValueError("Discontinuous external annotation requires separate review")
            location = locations[0]
            start, end = offset + location["offset"], offset + location["offset"] + location["length"]
            if not 0 <= start < end <= len(text) or text[start:end] != annotation["text"]:
                raise ValueError("PubTator entity offsets do not match stored source")
            identifier = infons.get("identifier", "")
            kind = kinds[infons["type"]]
            native = "NCBIGene:" + identifier if kind == "gene" and identifier.isdigit() else (
                "MeSH:" + identifier if kind == "component" and re.fullmatch(r"[DC]\d+", identifier) else None)
            entities.append({"kind": kind, "start": start, "end": end, "text": text[start:end],
                             "normalized_id": native, "status": "needs_review",
                             "payload": {"origin": "pubtator_assisted", "external_identifier": identifier}})
    return entities


def fetch(pmid, text, directory, session=None):
    import requests
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    key = digest(dumps({"pmid": str(pmid), "source_hash": digest(text), "endpoint": URL}))
    path = directory / (key + ".json")
    if path.exists():
        value = json.loads(path.read_bytes())
        raw = directory / value.get("raw_path", "missing")
        if value.get("status") == "aligned" and digest(raw.read_bytes()) != value["raw_sha256"]:
            raise ValueError("Cached PubTator raw response changed")
        return value
    value = {"pmid": str(pmid), "source_hash": digest(text), "fetched_at": now(),
             "profile": "pubtator_assisted", "endpoint": URL}
    try:
        response = (session or requests).get(URL, params={"pmids": str(pmid)}, timeout=(15, 90))
        if response.status_code == 404 or not response.content.strip():
            value.update(status="absent", entities=[])
        else:
            response.raise_for_status()
            raw_sha = digest(response.content)
            raw_path = directory / (raw_sha + ".bioc.json")
            if not raw_path.exists():
                raw_path.write_bytes(response.content)
            value.update(raw_sha256=raw_sha, raw_path=raw_path.name)
            records = response.json()
            if isinstance(records, dict):
                records = records.get("documents", [records])
            doc = next((d for d in records if str(d.get("id")) == str(pmid)), None)
            if doc is None:
                value.update(status="absent", entities=[])
            else:
                value.update(entities=align(doc, text), status="aligned",
                             external_relations=doc.get("relations", []))
    except ValueError as error:
        value.update(status="source_mismatch", error=str(error), entities=[])
    except requests.RequestException as error:
        # Transient failures are retried; never cached as valid empty annotations.
        return {**value, "status": "transient_failure", "error": str(error), "entities": []}
    path.write_text(dumps(value), encoding="utf-8")
    return value
