"""Frozen official MeSH lexicon; native identifiers never imply ChEBI equality."""
import gzip
import hashlib
import json
from pathlib import Path
import re
from datetime import datetime, timezone

import requests
from defusedxml.ElementTree import iterparse

from .dictionaries import _insert
from .extraction import _trie_matches

URL = "https://nlmpubs.nlm.nih.gov/projects/mesh/MESH_FILES/xmlmesh/desc2026.gz"
# Protein/enzyme/nucleic-acid and pharmacologic-action branches are excluded.
CHEMICAL_BRANCHES = {"D01", "D02", "D03", "D04", "D05", "D06", "D09", "D10", "D20", "D25", "D26"}


def build_snapshot(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    raw_path = directory / "desc2026.gz"
    if not raw_path.exists():
        temporary = raw_path.with_suffix(".download")
        with requests.get(URL, stream=True, timeout=(15, 180)) as response:
            response.raise_for_status()
            with temporary.open("wb") as stream:
                # Requests transparently decodes HTTP gzip in iter_content;
                # preserve the official .gz bytes instead of an XML response.
                for chunk in response.raw.stream(1048576, decode_content=False):
                    stream.write(chunk)
        with gzip.open(temporary, "rb") as stream:
            while stream.read(1048576):
                pass  # Check CRC before accepting the official archive.
        temporary.replace(raw_path)
    records = []
    with gzip.open(raw_path, "rb") as stream:
        for _, element in iterparse(stream, events=("end",)):
            if element.tag != "DescriptorRecord":
                continue
            trees = [node.text for node in element.findall("./TreeNumberList/TreeNumber")]
            if any(tree.split(".")[0] in CHEMICAL_BRANCHES for tree in trees):
                identifier = element.findtext("DescriptorUI")
                terms = sorted({node.text for node in element.findall("./ConceptList/Concept/TermList/Term/String") if node.text})
                if not identifier or not re.fullmatch(r"D\d+", identifier):
                    raise ValueError("Invalid official MeSH descriptor ID")
                records.append({"id": "MESH:" + identifier, "native_id": identifier,
                                "name": element.findtext("./DescriptorName/String"), "terms": terms, "trees": trees})
            element.clear()
    if len(records) < 1000:
        raise ValueError("Official chemical descriptor snapshot appears incomplete")
    manifest = {"schema_version": "literature-mesh-chemicals-v3", "source": URL,
                "source_reference": "https://www.nlm.nih.gov/databases/download/mesh.html",
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "source_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
                "release": "2026", "chemical_branches": sorted(CHEMICAL_BRANCHES),
                "records": len(records), "term_count": sum(len(r['terms']) for r in records),
                "identity_policy": "Native MeSH descriptors; classes remain classes, not ChEBI equivalence"}
    payload = json.dumps({"manifest": manifest, "records": records}, ensure_ascii=False, sort_keys=True).encode()
    path = directory / ("mesh-chemicals-" + hashlib.sha256(payload).hexdigest() + ".json")
    path.write_bytes(payload)
    return {"path": str(path), "sha256": hashlib.sha256(payload).hexdigest(), **manifest}


def load_snapshot(path, expected_sha256=None):
    raw = Path(path).read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    if expected_sha256 and sha != expected_sha256:
        raise ValueError("Chemical dictionary hash mismatch")
    value = json.loads(raw)
    if value['manifest']['schema_version'] != 'literature-mesh-chemicals-v3':
        raise ValueError("Expected official MeSH chemical snapshot")
    trie = {}
    for row in value['records']:
        for term in row['terms']:
            # Very short acronyms/numeric registry values need manual resolution.
            if len(term) < 4 or not re.search(r'[A-Za-z]{3}', term):
                continue
            _insert(trie, term, {**row, 'term': term})
    return {'trie': trie, 'manifest': {**value['manifest'], 'snapshot_sha256': sha}}


def augment_mentions(text, sentences, mentions, lexicon):
    if not lexicon:
        return mentions
    genes = [m for m in mentions if m['kind'] == 'gene']
    result = list(mentions)
    for start, end, entries in _trie_matches(text, lexicon['trie']):
        if any(g['start'] < end and start < g['end'] for g in genes):
            continue
        identifiers = {e['id'] for e in entries}
        if len(identifiers) != 1:
            continue  # Ambiguous aliases never pick the best gold ID.
        overlaps = [m for m in result if m['start'] < end and start < m['end']]
        if any(m['kind'] not in {'nutrient', 'component', 'chemical'} for m in overlaps):
            continue
        # Keep existing identities as separate links, not equivalence mappings.
        result = [m for m in result if m not in overlaps]
        result.append({'kind': 'component', 'text': text[start:end], 'start': start, 'end': end,
                       'normalized_id': next(iter(identifiers)), 'status': 'candidate',
                       'payload': {'dictionary_source': lexicon['manifest']['source'],
                                   'dictionary_sha256': lexicon['manifest']['snapshot_sha256'],
                                   'ontology_namespace': 'MESH', 'identity_status': 'descriptor_concept',
                                   'prior_dictionary_mentions': overlaps,
                                   'requires_species_review': False}})
    return sorted(result, key=lambda m: (m['start'], m['end'], m['kind']))
