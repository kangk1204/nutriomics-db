"""Public-corpus import with original offsets, explicit splits, and immutable receipts.

Importing a development partition never parses a test member of an archive. Labels
and extractor inputs are written separately; prediction must not open gold.json.
"""

import io
import json
import math
import re
import tarfile
import zipfile
from collections import Counter
from pathlib import Path

import requests
from defusedxml import ElementTree

from .db import connect, digest, dumps, now, uid

CATALOG = {
    "biored": {
        "url": "https://ftp.ncbi.nlm.nih.gov/pub/lu/BioRED/BIORED.zip",
        "reference": "https://doi.org/10.1093/bib/bbac282",
        "license_reference": "https://ftp.ncbi.nlm.nih.gov/pub/lu/BioRED/README.txt",
        "license": "US government work; corpus README public-domain notice; article text rights separate",
        "official_splits": True, "tasks": ["gene_detection", "normalization", "chemical_gene_relation"],
    },
    "bioredirect": {
        "url": "https://ftp.ncbi.nlm.nih.gov/pub/lu/BioREDirect/datasets.zip",
        "reference": "https://github.com/ncbi-nlp/BioREDirect",
        "license_reference": "https://github.com/ncbi-nlp/BioREDirect#disclaimer",
        "license": "NCBI research release; underlying BioRED public-domain notice; retain source disclaimer",
        "official_splits": True,
        "tasks": ["gene_detection", "normalization", "chemical_gene_relation", "direction"],
    },
    "bioscope": {
        "url": None,
        "urls": {"abstract": "https://rgai.inf.u-szeged.hu/sites/rgai.inf.u-szeged.hu/files/abstracts_pmid.xml",
                 "fulltext": "https://rgai.inf.u-szeged.hu/sites/rgai.inf.u-szeged.hu/files/full_papers.xml"},
        "reference": "https://rgai.inf.u-szeged.hu/node/105",
        "license_reference": "https://rgai.inf.u-szeged.hu/node/105",
        "license": "Publisher permits research purposes; cite corpus authors; clinical restricted corpus excluded",
        "official_splits": False, "tasks": ["negation_scope", "speculation_scope"],
    },
    "ebmnlp": {
        "url": "https://raw.githubusercontent.com/bepnye/EBM-NLP/master/ebm_nlp_2_00.tar.gz",
        "reference": "https://github.com/bepnye/EBM-NLP",
        "license_reference": "https://github.com/bepnye/EBM-NLP/blob/master/README.md",
        "license": "Release README specifies aggregation/annotators but no explicit corpus license; record this limitation",
        "official_splits": True, "tasks": ["participants", "interventions", "outcomes"],
    },
    "evidenceoutcomes": {
        "url": "https://raw.githubusercontent.com/ebmlab/EvidenceOutcomes/main/consensus.tar.gz",
        "reference": "https://github.com/ebmlab/EvidenceOutcomes",
        "license_reference": "https://github.com/ebmlab/EvidenceOutcomes#license",
        "license": "MIT for dataset repository; retain annotation attribution; article rights separate",
        "official_splits": False, "tasks": ["outcomes"],
    },
}

_KINDS = {"GeneOrGeneProduct": "gene", "Gene": "gene", "ChemicalEntity": "component",
          "Chemical": "component", "DiseaseOrPhenotypicFeature": "outcome", "Disease": "outcome",
          "OrganismTaxon": "organism", "SequenceVariant": "variant", "CellLine": "cell_line"}


def validate_span(text, span, field="text"):
    start, end = span.get("start"), span.get("end")
    if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text)
            or span.get(field) != text[start:end]):
        raise ValueError("Public annotation does not exactly match original Unicode codepoint span")


def _entity(text, start, end, surface, kind, identifiers, annotation_id=None):
    # The publisher's converter separates IDs by comma. Vertical bars are part
    # of sequence-variant identifiers and must never be split as entity IDs.
    ids = [x.strip("*") for x in re.split(r"[,;]", identifiers) if x.strip("*") not in {"", "-"}]
    entity = {"kind": _KINDS.get(kind, kind), "corpus_type": kind, "text": surface,
              "start": int(start), "end": int(end), "native_ids": sorted(set(ids)),
              "normalized_id": ids[0] if len(set(ids)) == 1 else None,
              "annotation_id": annotation_id}
    validate_span(text, entity)
    return entity


def parse_pubtator(raw):
    """BioRED/BioREDirect; preserve every relation type and optional subject role."""
    documents = []
    for block in re.split(r"\n\s*\n", raw.replace("\r\n", "\n").replace("\r", "\n").strip()):
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        parts, annotation_lines, relation_lines, directions = [], [], [], {}
        pmid = None
        for line in lines:
            title = re.match(r"^(\S+)\|([ta])\|(.*)$", line)
            if title:
                if pmid is not None and pmid != title[1]:
                    raise ValueError("PubTator block contains multiple papers")
                pmid = title[1]
                parts.append((title[2], title[3]))
                continue
            cols = line.split("\t")
            if len(cols) == 6 and cols[1].isdigit() and cols[2].isdigit():
                annotation_lines.append(cols)
            elif len(cols) == 4 and cols[-1].startswith("Subject:"):
                subject = cols[-1].split(":", 1)[1]
                if subject not in cols[1:3] and subject.rsplit("|", 1)[0] in cols[1:3]:
                    subject = subject.rsplit("|", 1)[0]
                directions[frozenset((cols[1], cols[2]))] = subject
            elif len(cols) in {4, 5}:
                relation_lines.append(cols)
            else:
                raise ValueError("Unsupported PubTator annotation record")
        if not pmid or not parts:
            raise ValueError("PubTator paper requires source text")
        text = " ".join(value for _, value in parts)
        sections, offset = [], 0
        for tag, value in parts:
            sections.append({"start": offset, "end": offset + len(value),
                             "label": "Title" if tag == "t" else "Abstract"})
            offset += len(value) + 1
        entities = [_entity(text, c[1], c[2], c[3], c[4], c[5]) for c in annotation_lines]
        relations = []
        for cols in relation_lines:
            if cols[0] != pmid:
                raise ValueError("Relation PMID differs from source")
            relation = {"predicate": cols[1].split("|")[0], "entity1": cols[2], "entity2": cols[3]}
            if len(cols) == 5:
                relation["novelty"] = cols[4].split("|")[0]
            role = directions.get(frozenset((cols[2], cols[3])))
            if role is not None and role not in {cols[2], cols[3], "None", "-"}:
                raise ValueError("Direction annotation subject is not a relation endpoint")
            if role not in {None, "None", "-"}:
                relation["subject"] = role
            relations.append(relation)
        documents.append({"pmid": pmid, "text": text, "sections": sections, "entities": entities,
                          "relations": relations, "source_level": "abstract"})
    return documents


def parse_bioc_json(raw):
    collection = json.loads(raw)
    documents = []
    for record in collection["documents"]:
        passages = sorted(record["passages"], key=lambda p: p["offset"])
        length = max(p["offset"] + len(p.get("text", "")) for p in passages)
        chars, sections, entities, annotation_ids = [" "] * length, [], [], {}
        for passage in passages:
            start, text = passage["offset"], passage.get("text", "")
            chars[start:start + len(text)] = text
            sections.append({"start": start, "end": start + len(text),
                             "label": passage.get("infons", {}).get("type", "Abstract")})
        text = "".join(chars)
        for passage in passages:
            for annotation in passage.get("annotations", []):
                infons = annotation.get("infons", {})
                for location in annotation["locations"]:
                    start, end = location["offset"], location["offset"] + location["length"]
                    entity = _entity(text, start, end, text[start:end], infons["type"],
                                     infons.get("identifier", ""), annotation["id"])
                    entities.append(entity)
                    annotation_ids[annotation["id"]] = entity["native_ids"]
        relations = []
        for relation in record.get("relations", []):
            infons = relation.get("infons", {})
            if "entity1" in infons:
                endpoints = [infons["entity1"], infons["entity2"]]
            else:
                endpoints = [annotation_ids[node["refid"]][0] for node in relation.get("nodes", [])]
            if len(endpoints) != 2:
                raise ValueError("BioC relation must have two endpoints")
            relations.append({"entity1": endpoints[0], "entity2": endpoints[1],
                              "predicate": infons["type"], "novelty": infons.get("novel", "unknown")})
        documents.append({"pmid": record["id"], "text": text, "sections": sections,
                          "entities": entities, "relations": relations, "source_level": "abstract"})
    return documents


def parse_bioscope(raw, source_level="abstract"):
    """Keep cue-linked XML scopes; biological papers, not clinical radiology notes."""
    if source_level not in {"abstract", "fulltext"}:
        raise ValueError("Only biological abstract/fulltext BioScope partitions are supported")
    root = ElementTree.fromstring(raw)
    documents = []
    for index, record in enumerate(root.findall(".//Document")):
        sentences = record.findall(".//sentence")
        paper_parts, scopes, cues, offset = [], [], [], 0
        for sentence in sentences:
            content = sentence.find("sentence_text")
            if content is None:
                content = sentence
            pieces, cursor = [], offset

            def visit(node):
                nonlocal cursor
                start = cursor
                if node.text:
                    pieces.append(node.text)
                    cursor += len(node.text)
                for child in node:
                    visit(child)
                    if child.tail:
                        pieces.append(child.tail)
                        cursor += len(child.tail)
                if node.tag == "cue":
                    cues.append({"cue_id": node.get("ref"), "kind": node.get("type"),
                                 "start": start, "end": cursor})
                elif node.tag == "xcope":
                    scopes.append({"cue_id": node.get("id"), "start": start, "end": cursor})

            visit(content)
            sentence_text = "".join(pieces)
            paper_parts.append(sentence_text)
            offset = cursor + 1
        text = " ".join(paper_parts)
        for scope in scopes:
            linked_cues = [cue for cue in cues if cue["cue_id"] == scope["cue_id"]]
            scope["kind"] = next((cue["kind"] for cue in linked_cues), "unknown")
            scope["cue_spans"] = [{"start": cue["start"], "end": cue["end"]} for cue in linked_cues]
            scope["text"] = text[scope["start"]:scope["end"]]
            validate_span(text, scope)
        for cue in cues:
            cue["text"] = text[cue["start"]:cue["end"]]
            validate_span(text, cue)
        identifier = (record.get("id") or record.get("pmid") or (record.findtext("DocID") or "").strip()
                      or f"bioscope-{source_level}-{index}")
        documents.append({"pmid": identifier, "text": text,
                          "sections": [{"start": 0, "end": len(text), "label": "Abstract"}],
                          "entities": [], "relations": [], "scopes": scopes, "cues": cues,
                          "source_level": source_level})
    if not documents:
        raise ValueError("Expected BioScope Document elements")
    return documents


def _archive_members(raw):
    """Read members in memory: no filesystem archive traversal or extraction."""
    if zipfile.is_zipfile(io.BytesIO(raw)):
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            return {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as archive:
        return {member.name: archive.extractfile(member).read() for member in archive.getmembers()
                if member.isfile() and member.size < 100_000_000}


def _brat_members(members):
    documents = []
    for name, raw in sorted(members.items()):
        if not name.endswith(".txt"):
            continue
        ann = members.get(name[:-4] + ".ann")
        if ann is None:
            continue
        text, spans = raw.decode("utf-8"), []
        for line in ann.decode("utf-8").splitlines():
            if not line.startswith("T"):
                continue
            _, description, surface = line.split("\t", 2)
            label, locations = description.split(" ", 1)
            # Discontinuous BRAT labels remain separate spans, never fill intervening text.
            for location in locations.split(";"):
                start, end = map(int, location.split())
                span = {"label": "outcomes", "native_label": label, "start": start, "end": end,
                        "text": text[start:end]}
                validate_span(text, span)
                spans.append(span)
            if ";" not in locations and surface != spans[-1]["text"]:
                raise ValueError("BRAT surface text differs from annotation offset")
        documents.append({"pmid": Path(name).stem, "text": text, "annotated_spans": spans,
                          "sections": [{"start": 0, "end": len(text), "label": "Results"}],
                          "entities": [], "relations": [], "source_level": "abstract"})
    return documents


def _ebm_members(members, split):
    """Aggregated starting spans only, preserving token positions in raw text."""
    documents = []
    partition = "test" if split == "test" else "train"
    label_files = {}
    for name in members:
        match = re.search(r"annotations/aggregated/starting_spans/(participants|interventions|outcomes)/"
                          r"(train|test)/([^/]+)\.ann$", name)
        if match and match[2] == partition:
            pmid = match[3].removesuffix(".AGGREGATED")
            label_files.setdefault(pmid, {})[match[1]] = name
    for pmid, files in sorted(label_files.items()):
        text_name = next((n for n in members if n.endswith((f"documents/{pmid}.text", f"documents/{pmid}.txt"))), None)
        token_name = next((n for n in members if n.endswith(f"documents/{pmid}.tokens")), None)
        if not text_name or not token_name:
            raise ValueError("EBM-NLP annotation requires raw text and tokens")
        text = members[text_name].decode("utf-8")
        tokens = members[token_name].decode("utf-8").split()
        offsets, cursor = [], 0
        for token in tokens:
            start = text.find(token, cursor)
            if start < 0:
                raise ValueError("EBM-NLP token cannot align to original text; no offset guessing")
            offsets.append((start, start + len(token)))
            cursor = start + len(token)
        spans = []
        for label, name in files.items():
            labels = [int(v) for v in members[name].decode("utf-8").split()]
            if len(labels) != len(tokens):
                raise ValueError("EBM-NLP label/token lengths differ")
            start = None
            for i, value in enumerate(labels + [0]):
                if value and start is None:
                    start = i
                if not value and start is not None:
                    a, b = offsets[start][0], offsets[i - 1][1]
                    spans.append({"label": label, "start": a, "end": b, "text": text[a:b]})
                    start = None
        documents.append({"pmid": pmid, "text": text, "annotated_spans": spans,
                          "sections": [{"start": 0, "end": len(text), "label": "Abstract"}],
                          "entities": [], "relations": [], "source_level": "abstract"})
    return documents


def _read_source(corpus, raw, split, source_level):
    stripped = raw.lstrip()
    if stripped.startswith(b"{"):
        payload = json.loads(raw)
        if payload.get("schema_version") == "literature-public-source-v1":
            if payload.get("corpus") != corpus or payload.get("split") != split:
                raise ValueError("Normalized source corpus/split mismatch")
            return payload["documents"], "normalized-json", False
        if corpus in {"biored", "bioredirect"}:
            return parse_bioc_json(raw), "BioC.JSON", True
    if stripped.startswith(b"<") and corpus == "bioscope":
        return parse_bioscope(raw, source_level), "BioScope.XML", False
    if corpus in {"biored", "bioredirect"} and not zipfile.is_zipfile(io.BytesIO(raw)):
        return parse_pubtator(raw.decode("utf-8")), "PubTator", True
    members = _archive_members(raw)
    if corpus == "biored":
        tag = {"development": "Dev", "train": "Train", "test": "Test"}[split]
        name = next((n for n in members if n.endswith(f"/{tag}.PubTator")), None)
        if name is None:
            raise ValueError("Official BioRED partition not found")
        return parse_pubtator(members[name].decode("utf-8")), name, True
    if corpus == "bioredirect":
        tag = {"development": "dev", "train": "train", "test": "test"}[split]
        name = next((n for n in members if n.endswith(f"bioredirect_{tag}.pubtator")), None)
        if name is None:
            raise ValueError("Official BioREDirect partition not found")
        return parse_pubtator(members[name].decode("utf-8")), name, True
    if corpus == "evidenceoutcomes":
        return _brat_members(members), "consensus BRAT", False
    if corpus == "ebmnlp":
        return _ebm_members(members, split), "aggregated starting spans", True
    if corpus == "bioscope":
        marker = "abstract" if source_level == "abstract" else "full"
        matching = [name for name in members if marker in name.lower() and name.lower().endswith(".xml")]
        if len(matching) != 1:
            raise ValueError("Specify the biological BioScope abstract/fulltext XML source explicitly")
        return parse_bioscope(members[matching[0]], source_level), matching[0], False
    raise ValueError("Unsupported public release format")


def _preregister_partition(settings, corpus, source_hash, source_level, documents, excluded,
                          seed=20261003, test_fraction=.2):
    """Assign papers using IDs only, never strata/labels/predictions or model output."""
    identifiers = sorted(str(d["pmid"]) for d in documents if str(d["pmid"]) not in excluded)
    if len(set(identifiers)) != len(identifiers) or len(identifiers) < 2:
        raise ValueError("Preregistered holdout requires at least two unique document groups")
    if type(seed) is not int or not 0 < test_fraction < 1:
        raise ValueError("Invalid prespecified paper partition")
    ranked = sorted(identifiers, key=lambda pmid: digest(f"{corpus}|{source_level}|{seed}|{pmid}"))
    count = min(len(ranked) - 1, max(1, math.ceil(test_fraction * len(ranked))))
    heldout = set(ranked[:count])
    record = {"schema_version": "literature-public-partition-v1", "corpus": corpus,
              "source_sha256": source_hash, "source_level": source_level, "seed": seed,
              "test_fraction": test_fraction, "method": "paper ID SHA256 ranking; ceil fraction; no label strata",
              "assignments": {pmid: "test" if pmid in heldout else "development" for pmid in identifiers},
              "excluded_overlap_pmids": sorted(excluded), "official_split": False,
              "pretraining_overlap": "unknown; text may occur in parser training corpus"}
    identity = digest(dumps(record))
    path = settings.home / "public_benchmarks" / "partitions" / (identity + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if json.loads(path.read_bytes()) != record:
            raise ValueError("Preregistered partition changed")
    else:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(dumps(record))
        with connect(settings, durable=True) as con:
            con.execute("INSERT INTO audit VALUES(?,?,?,?,?)", (
                uid(), "public_partition_predeclared", None,
                dumps({"id": identity, "sha256": digest(dumps(record))}), now(),
            ))
    return record, identity


def _reserve_test_ids(settings, corpus, raw, partition=None):
    """Read paper identifiers only from official test members, not their labels."""
    identifiers = set()
    if partition is not None:
        identifiers = {pmid for pmid, split in partition["assignments"].items() if split == "test"}
    elif corpus in {"biored", "bioredirect"} and zipfile.is_zipfile(io.BytesIO(raw)):
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            suffix = "/Test.PubTator" if corpus == "biored" else "bioredirect_test.pubtator"
            for name in archive.namelist():
                if name.endswith(suffix):
                    identifiers.update(re.findall(r"^(\S+)\|t\|", archive.read(name).decode("utf-8"), re.M))
    elif corpus == "ebmnlp":
        try:
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as archive:
                for name in archive.getnames():
                    match = re.search(r"aggregated/starting_spans/[^/]+/test/(\d+)\.", name)
                    if match:
                        identifiers.add(match[1])
        except tarfile.ReadError:
            pass
    if identifiers:
        detail = {"corpus": corpus, "release_sha256": digest(raw), "pmids": sorted(identifiers)}
        with connect(settings, durable=True) as con:
            con.execute("INSERT INTO audit VALUES(?,?,?,?,?)", (
                uid(), "public_protected_test_ids", None, dumps(detail), now(),
            ))
    with connect(settings) as con:
        prior = [json.loads(row[0]) for row in con.execute(
            "SELECT detail FROM audit WHERE kind='public_protected_test_ids'",
        )]
    return {str(pmid) for record in prior for pmid in record.get("pmids", [])}


def prepare_public(settings, corpus, split="development", source_path=None, license_reference=None,
                   exclude_inputs=None, source_level="abstract", split_seed=20261003, test_fraction=.2):
    if corpus not in CATALOG or split not in {"development", "train", "test"}:
        raise ValueError("Unknown corpus or partition")
    spec = CATALOG[corpus]
    if split == "train" and not spec["official_splits"]:
        raise ValueError("This release has preregistered development/test, not an official train partition")
    license_reference = license_reference or spec["license_reference"]
    if not license_reference:
        raise ValueError("Record the release license reference before importing")
    if source_path is not None:
        raw, source_url = Path(source_path).read_bytes(), str(Path(source_path).resolve())
    else:
        download_url = spec["url"] or spec.get("urls", {}).get(source_level)
        if not download_url:
            return {"corpus": corpus, "status": "source_required", "reference": spec["reference"],
                    "note": "No unavailable corpus is counted as evaluated"}
        response = requests.get(download_url, timeout=(15, 120))
        response.raise_for_status()
        raw, source_url = response.content, download_url
    documents, member, official = _read_source(corpus, raw, split, source_level)
    excluded = set()
    nutrition_inputs = ([Path(exclude_inputs)] if exclude_inputs else
                        list((settings.home / "evaluation").glob("*/frozen_inputs.json")))
    for nutrition_input in nutrition_inputs:
        # Inputs have no gold labels; reserve all nutrition documents, not only heldout.
        nutrition = json.loads(nutrition_input.read_bytes())
        excluded.update(str(d["pmid"]) for d in nutrition["documents"])
    prior_inputs = [json.loads(path.read_bytes()) for path in
                    (settings.home / "public_benchmarks").glob("*/*/inputs.json")]
    if not spec["official_splits"]:
        for prior in prior_inputs:
            if (prior.get("split") != "test" and (prior.get("corpus") != corpus
                    or prior.get("manifest", {}).get("source_sha256") != digest(raw))):
                excluded.update(d["pmid"] for d in prior.get("documents", []))
        with connect(settings) as con:
            other_test_reservations = [json.loads(row[0]) for row in con.execute(
                "SELECT detail FROM audit WHERE kind='public_protected_test_ids'",
            )]
        for reservation in other_test_reservations:
            if reservation.get("corpus") != corpus or reservation.get("release_sha256") != digest(raw):
                excluded.update(reservation.get("pmids", []))
        excluded.intersection_update(str(document["pmid"]) for document in documents)
    partition, partition_sha = None, None
    if not spec["official_splits"]:
        partition, partition_sha = _preregister_partition(
            settings, corpus, digest(raw), source_level, documents, excluded, split_seed, test_fraction,
        )
    protected_test_ids = _reserve_test_ids(settings, corpus, raw, partition)
    previously_developed = set()
    if split == "test":
        for prior in prior_inputs:
            if prior.get("split") != "test":
                previously_developed.update(d["pmid"] for d in prior.get("documents", []))
    prior_test_exclusions = set()
    retained, duplicates, seen = [], [], set()
    for document in documents:
        pmid = str(document["pmid"])
        if pmid in seen:
            raise ValueError("Duplicate paper in public partition")
        seen.add(pmid)
        if pmid in excluded:
            duplicates.append(pmid)
            continue
        if partition is not None and partition["assignments"][pmid] != split:
            continue
        if split != "test" and pmid in protected_test_ids:
            prior_test_exclusions.add(pmid)
            continue
        if split == "test" and pmid in previously_developed:
            raise ValueError(f"Public test paper {pmid} already appeared in another development partition")
        text = document["text"]
        if not isinstance(text, str) or not text:
            raise ValueError("Public source text must be nonempty")
        for field in ["entities", "scopes", "cues", "annotated_spans"]:
            for span in document.get(field, []):
                validate_span(text, span)
        retained.append({**document, "pmid": pmid, "group_id": "pmid:" + pmid,
                         "source_hash": digest(text), "split": split})
    if not retained:
        raise ValueError("No public documents after leakage exclusions")
    identity = digest(dumps({"preparation": "v2-oracle-inputs", "corpus": corpus, "split": split, "raw": digest(raw),
                             "member": member, "excluded": sorted(excluded), "partition": partition_sha}))[:24]
    directory = settings.home / "public_benchmarks" / corpus / identity
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {"schema_version": "literature-public-manifest-v1", "corpus": corpus, "split": split,
                "prepared_at": now(), "source_url": source_url, "source_sha256": digest(raw),
                "source_member": member, "reference": spec["reference"], "license": spec["license"],
                "license_reference": license_reference, "official_split": official,
                "preregistered_split": partition is not None, "partition_sha256": partition_sha,
                "partition": partition,
                "complete_partition": ((source_path is None and official and not duplicates)
                                       or (partition is not None and member != "normalized-json"))
                                      and not prior_test_exclusions,
                "full_release_coverage": not duplicates,
                "documents": len(retained), "excluded_overlap_pmids": duplicates,
                "excluded_other_test_pmids": sorted(prior_test_exclusions),
                "tasks": spec["tasks"], "source_levels": dict(Counter(d["source_level"] for d in retained)),
                "pretraining_overlap": "unknown; model training data may include PubMed text",
                "annotation_status": "published_corpus_annotations"}
    manifest["annotation_coverage"] = {
        "scope_annotations": sum(len(d.get("scopes", [])) for d in retained),
        "scopes_without_annotated_cue_type": sum(scope.get("kind") not in {"negation", "speculation", "hedge"}
                                                 for d in retained for scope in d.get("scopes", [])),
        "note": "Fields without published labels are not scored; cue-linked scopes use exact cue spans",
    }
    inputs = {"schema_version": "literature-public-inputs-v1", "corpus": corpus, "split": split,
              "manifest": manifest, "documents": [
                  {key: document[key] for key in ("pmid", "group_id", "text", "source_hash", "split",
                                                 "source_level", "sections")} for document in retained]}
    gold = {"schema_version": "literature-public-gold-v1", "corpus": corpus, "split": split,
            "manifest": manifest, "documents": retained}
    oracle = {"schema_version": "literature-public-oracle-entities-v1", "corpus": corpus, "split": split,
              "documents": [{"pmid": document["pmid"], "source_hash": document["source_hash"],
                             "entities": document["entities"]} for document in retained]}
    inputs_raw, gold_raw = dumps(inputs), dumps(gold)
    oracle_raw = dumps(oracle)
    receipt = {**manifest, "inputs_sha256": digest(inputs_raw), "gold_sha256": digest(gold_raw),
               "oracle_entities_sha256": digest(oracle_raw)}
    for name, content in [("inputs.json", inputs_raw), ("gold.json", gold_raw),
                          ("oracle_entities.json", oracle_raw), ("manifest.json", dumps(receipt))]:
        path = directory / name
        if path.exists() and path.read_text(encoding="utf-8") != content:
            # Same source release is immutable, even on a repeated import at a new time.
            existing = json.loads((directory / "manifest.json").read_bytes())
            if existing["source_sha256"] != digest(raw):
                raise ValueError("Immutable public release already exists with different hash")
            return {"status": "prepared_existing", "corpus": corpus, "split": split,
                    "inputs_path": str(directory / "inputs.json"), "gold_path": str(directory / "gold.json"),
                    "manifest_path": str(directory / "manifest.json"), "documents": existing["documents"]}
        path.write_text(content, encoding="utf-8")
    (directory / "source.release").write_bytes(raw)
    with connect(settings, durable=True) as con:
        con.execute("INSERT INTO audit VALUES(?,?,?,?,?)", (
            uid(), "public_corpus_prepared", None,
            dumps({"id": identity, "sha256": digest(dumps(receipt))}), now(),
        ))
    return {"status": "prepared", "corpus": corpus, "split": split, "inputs_path": str(directory / "inputs.json"),
            "gold_path": str(directory / "gold.json"), "manifest_path": str(directory / "manifest.json"),
            "documents": len(retained)}
