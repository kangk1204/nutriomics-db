"""Frozen, content-addressed official HGNC snapshots and curated topic lexicons."""

from dataclasses import dataclass
from datetime import datetime, timezone
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import tempfile

import requests

HGNC_URL = "https://storage.googleapis.com/public-download-files/hgnc/tsv/tsv/hgnc_complete_set.txt"
ENTITY_PATH = Path(__file__).resolve().parents[1] / "config" / "entities.json"
_TOKEN = re.compile(r"[\w]+", re.UNICODE)


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(data)
    temporary.replace(path)


def _records(raw: bytes) -> list[dict]:
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")), delimiter="\t")
    required = {"hgnc_id", "symbol", "name", "status"}
    if not required.issubset(reader.fieldnames or []):
        raise ValueError("HGNC TSV is missing required columns")
    records = []
    for row in reader:
        if row["status"] != "Approved":
            continue
        if not re.fullmatch(r"HGNC:\d+", row["hgnc_id"]) or not row["symbol"] or not row["name"]:
            raise ValueError("Invalid approved HGNC record")
        records.append(
            {
                "id": row["hgnc_id"],
                "symbol": row["symbol"],
                "name": row["name"],
                "aliases": [
                    term
                    for key in ("alias_symbol", "prev_symbol", "alias_name", "prev_name")
                    for term in (row.get(key) or "").split("|")
                    if term
                ],
            }
        )
    if not records:
        raise ValueError("HGNC TSV contains no approved genes")
    return records


@dataclass
class Dictionaries:
    version: str
    manifest: dict
    gene_trie: dict
    entity_trie: dict


def _insert(trie, term, record):
    tokens = _TOKEN.findall(term.casefold())
    if not tokens:
        return
    for token in tokens:
        trie = trie.setdefault(token, {})
    candidates = trie.setdefault("_entries", [])
    if record not in candidates:
        candidates.append(record)


def update_hgnc(settings) -> dict:
    """Explicit refresh only. Retain old snapshots and atomically activate one.

    No refresh occurs in load_dictionaries, preserving reproducible extractions.
    HTTPS source, response integrity and minimum complete-set size are checked
    before the active manifest is replaced.
    """
    response = requests.get(HGNC_URL, timeout=(15, 180), headers={"User-Agent": "nutriomics-literature/0.1"})
    response.raise_for_status()
    raw = response.content
    records = _records(raw)
    if len(records) < 30000:
        raise ValueError(f"HGNC complete set appears truncated: {len(records)} approved genes")
    sha = _hash(raw)
    path = settings.dictionaries / f"hgnc-{sha}.tsv"
    manifest = {
        "source": HGNC_URL,
        "sha256": sha,
        "path": path.name,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "approved_genes": len(records),
        "etag": response.headers.get("ETag"),
        "last_modified": response.headers.get("Last-Modified"),
        "version": f"hgnc-sha256:{sha}",
    }
    _atomic(path, raw)
    _atomic(
        settings.dictionaries / f"hgnc-{sha}.manifest.json",
        json.dumps(manifest, ensure_ascii=False, indent=2).encode(),
    )
    _atomic(
        settings.dictionaries / "hgnc-active.json",
        json.dumps(manifest, ensure_ascii=False, indent=2).encode(),
    )
    return manifest


def load_dictionaries(settings) -> Dictionaries:
    entities_path = settings.config.parent / "entities.json"
    if not entities_path.exists():
        entities_path = ENTITY_PATH
    raw_entities = entities_path.read_bytes()
    config = json.loads(raw_entities)
    active = settings.dictionaries / "hgnc-active.json"
    if active.exists():
        manifest = json.loads(active.read_text(encoding="utf-8"))
        snapshot = settings.dictionaries / manifest["path"]
        if snapshot.parent.resolve() != settings.dictionaries.resolve():
            raise ValueError("HGNC snapshot path is outside the dictionary directory")
        raw = snapshot.read_bytes()
        if _hash(raw) != manifest["sha256"]:
            raise ValueError("HGNC snapshot hash mismatch")
        genes = _records(raw)
        manifest = {**manifest, "fallback": False}
    else:
        # Unverified local seeds remain candidate symbols, with no invented IDs.
        genes = [{**row, "id": None} for row in config.get("fallback_genes", [])]
        manifest = {
            "source": "curated fallback symbols (HGNC IDs unavailable)",
            "version": "fallback-unverified-v1",
            "fallback": True,
            "approved_genes": 0,
        }
    gene_trie, entity_trie = {}, {}
    for gene in genes:
        forms = [(gene["symbol"], "symbol"), (gene["name"], "name")] + [
            (alias, "alias") for alias in gene.get("aliases", [])
        ]
        for form, form_kind in forms:
            _insert(gene_trie, form, {"term": form, "form_kind": form_kind, **gene})
    for entity in config["entities"]:
        for term in entity["terms"]:
            _insert(entity_trie, term, {"term": term, **entity})
    entity_hash = _hash(raw_entities)
    version = f"{manifest['version']}|{config['version']}|entities-sha256:{entity_hash}"
    return Dictionaries(version, {**manifest, "entity_sha256": entity_hash}, gene_trie, entity_trie)
