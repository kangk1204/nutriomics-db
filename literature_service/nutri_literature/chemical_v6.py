"""Complete frozen MeSH D/C records; parent headings never become identities."""

from collections import Counter
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import re

from defusedxml.ElementTree import iterparse
import requests

from .chemical_v3 import CHEMICAL_BRANCHES, augment_mentions
from .db import digest, dumps
from .dictionaries import _insert

BASE = "https://nlmpubs.nlm.nih.gov/projects/mesh/MESH_FILES/xmlmesh/"
SCHEMA = "mesh-d-and-c-v6-1"


def parse_records(stream, supplemental=False):
    tag = "SupplementalRecord" if supplemental else "DescriptorRecord"
    for _, node in iterparse(stream, events=("end",)):
        if node.tag != tag:
            continue
        identifier = node.findtext("SupplementalRecordUI" if supplemental else "DescriptorUI")
        if not re.fullmatch(("C" if supplemental else "D") + r"\d+", identifier or ""):
            raise ValueError("Invalid MeSH native identifier")
        trees = [v.text for v in node.findall("./TreeNumberList/TreeNumber") if v.text]
        headings = sorted(
            {
                v.text
                for v in node.findall(
                    "./HeadingMappedToList/HeadingMappedTo/DescriptorReferredTo/DescriptorUI"
                )
                if v.text
            }
        )
        terms = sorted({v.text for v in node.findall("./ConceptList/Concept/TermList/Term/String") if v.text})
        name = node.findtext("./SupplementalRecordName/String" if supplemental else "./DescriptorName/String")
        if name:
            terms = sorted(set(terms) | {name})
        # SCRClass=1 includes chemicals; protein/gene overlaps are separately checked.
        chemical = (
            node.get("SCRClass") == "1"
            if supplemental
            else any(t.split(".")[0] in CHEMICAL_BRANCHES for t in trees)
        )
        yield {
            "id": "MESH:" + identifier,
            "native_id": identifier,
            "name": name,
            "terms": terms,
            "trees": trees,
            "record_kind": "supplemental" if supplemental else "descriptor",
            "scr_class": node.get("SCRClass"),
            "chemical_candidate": chemical,
            "broader_headings": headings,
            "broader_headings_are_identity": False,
        }
        node.clear()


def build_snapshot(directory, release="2026"):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    records, sources = [], []
    for stem in ("desc", "supp"):
        name = stem + release + ".gz"
        url = BASE + name
        raw_path = directory / name
        if not raw_path.exists():
            temporary = raw_path.with_suffix(".download")
            with requests.get(url, stream=True, timeout=(15, 180)) as response:
                response.raise_for_status()
                with temporary.open("wb") as stream:
                    for chunk in response.raw.stream(1048576, decode_content=False):
                        stream.write(chunk)
            with gzip.open(temporary, "rb") as stream:
                while stream.read(1048576):
                    pass
            sha = digest(temporary.read_bytes())
            preserved = directory / (sha + "-" + name)
            if not preserved.exists():
                preserved.write_bytes(temporary.read_bytes())
            temporary.replace(raw_path)
        with gzip.open(raw_path, "rb") as stream:
            rows = list(parse_records(stream, stem == "supp"))
        if len(rows) < (10000 if stem == "desc" else 100000):
            raise ValueError("Incomplete official MeSH release")
        raw_sha = digest(raw_path.read_bytes())
        preserved = directory / (raw_sha + "-" + name)
        if not preserved.exists():
            preserved.write_bytes(raw_path.read_bytes())
        sources.append({"url": url, "sha256": raw_sha, "raw_file": preserved.name, "records": len(rows)})
        records.extend(rows)
    manifest = {
        "schema_version": SCHEMA,
        "source": BASE,
        "sources": sources,
        "release": release,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "records": len(records),
        "records_sha256": digest(dumps(records)),
        "namespace_counts": dict(Counter(r["native_id"][0] for r in records)),
        "identity_policy": "native IDs only; parent/class links are not equality",
    }
    raw = dumps({"manifest": manifest, "records": records}).encode()
    path = directory / ("mesh-v6-" + digest(raw) + ".json")
    path.write_bytes(raw)
    return {"path": str(path), "sha256": digest(raw), **manifest}


def load_snapshot(path, expected_sha256=None):
    raw = Path(path).read_bytes()
    if expected_sha256 and digest(raw) != expected_sha256:
        raise ValueError("MeSH snapshot hash mismatch")
    snapshot = json.loads(raw)
    manifest, records = snapshot["manifest"], snapshot["records"]
    if manifest["schema_version"] != SCHEMA or digest(dumps(records)) != manifest["records_sha256"]:
        raise ValueError("Invalid complete MeSH snapshot")
    trie, index = {}, {}
    for row in records:
        if not row["chemical_candidate"]:
            continue
        entry = {k: v for k, v in row.items() if k != "terms"}
        for term in row["terms"]:
            index.setdefault(term.casefold(), []).append(entry)
            # Full C/D alias coverage is in the exact normalizer. Only the much
            # smaller descriptor set is a lexical detection fallback: a trie
            # containing every SCR synonym duplicates millions of characters.
            if row.get("record_kind") == "descriptor" and len(term) >= 4 and re.search(r"[A-Za-z]{3}", term):
                _insert(trie, term, {**entry, "term": term})
    return {"trie": trie, "index": index, "manifest": {**manifest, "snapshot_sha256": digest(raw)}}


def augment(text, sentences, mentions, lexicon):
    values = augment_mentions(text, sentences, mentions, lexicon)
    for value in values:
        if str(value.get("normalized_id", "")).startswith("MESH:C"):
            value["payload"]["identity_status"] = "supplemental_concept"
    return values
