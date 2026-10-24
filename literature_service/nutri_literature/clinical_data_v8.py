"""Prepare only explicitly permitted native PIO/RCT training annotations.

Protected archive annotation members are never opened. Evidence Inference ICO
descriptions are not read or converted into token labels.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import tarfile
import uuid

from .public_corpora import _ebm_members, validate_span

PIO = {"participants", "interventions", "outcomes"}
RCT_LABELS = {"BACKGROUND", "OBJECTIVE", "METHODS", "RESULTS", "CONCLUSIONS"}


def _sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode("utf-8")).hexdigest()


def _filehash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _write(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())


def _ids(values, label):
    if not isinstance(values, list) or not values or any(not str(p).isdecimal() for p in values):
        raise ValueError("Explicit nonempty native training PMIDs required: " + label)
    ids = list(map(str, values))
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate permitted PMID: " + label)
    return set(ids)


def _ebm(path, allowed):
    members, categories, document_kinds = {}, {}, {}
    source_hash = _filehash(path)
    with tarfile.open(path, "r:*") as archive:
        for member in archive:
            if not member.isfile():
                continue
            name = member.name
            annotation = re.search(r"annotations/aggregated/starting_spans/([^/]+)/(train|test)/([^/]+)\.ann$", name)
            document = re.search(r"documents/(\d+)\.(text|txt|tokens)$", name)
            selected = False
            if annotation:
                pmid = annotation[3].removesuffix(".AGGREGATED")
                if annotation[2] != "train" or pmid not in allowed:
                    continue
                if annotation[1] not in PIO:
                    raise ValueError("Unknown native EBM annotation category")
                key = (pmid, annotation[1])
                if key in categories:
                    raise ValueError("Duplicate native EBM annotation")
                categories[key] = name
                selected = True
            elif document and document[1] in allowed:
                kind = "text" if document[2] in {"text", "txt"} else "tokens"
                key = (document[1], kind)
                if key in document_kinds:
                    raise ValueError("Duplicate EBM source text/token member")
                document_kinds[key] = name
                selected = True
            if selected:
                if name in members or member.size > 10 * 1024 ** 2:
                    raise ValueError("Duplicate or oversized training archive member")
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError("Training archive member unavailable")
                with stream:
                    raw = stream.read()
                if annotation:
                    labels = raw.decode("utf-8").split()
                    if any(value not in {"0", "1"} for value in labels):
                        raise ValueError("Unknown native EBM token annotation value")
                members[name] = raw
    if any(not any((p, k) in categories for k in PIO) for p in allowed):
        raise ValueError("Missing all allowed EBM PIO annotations")
    if any((p, k) not in document_kinds for p in allowed for k in ("text", "tokens")):
        raise ValueError("Missing allowed EBM source text/tokens")
    documents = []
    for pmid in sorted(allowed):
        # The legacy importer scans members to locate each document. Per-paper
        # subsets preserve its exact token alignment while avoiding a quadratic
        # scan across thousands of unrelated annotation filenames.
        names = [document_kinds[(pmid, k)] for k in ("text", "tokens")]
        names += [categories[(pmid, k)] for k in sorted(PIO) if (pmid, k) in categories]
        documents.extend(_ebm_members({name: members[name] for name in names}, "development"))
    if {str(d["pmid"]) for d in documents} != allowed or len(documents) != len(allowed):
        raise ValueError("Allowed EBM PMID set mismatch")
    for document in documents:
        for span in document["annotated_spans"]:
            if span["label"] not in PIO:
                raise ValueError("Unknown prepared PIO label")
            validate_span(document["text"], span)
        document["source_sha256"] = source_hash
        document["text_sha256"] = _sha(document["text"])
        document["official_split"] = "train"
        # Missing annotation files do not mean all-negative labels. Downstream
        # role-specific models must skip this paper for unannotated P/I/O roles.
        document["annotated_labels"] = sorted(k for k in PIO if (document["pmid"], k) in categories)
    return documents


def _rct(path, allowed):
    documents, seen, current, original = [], set(), None, []

    def finish():
        if current is None:
            return
        if not current["sentences"]:
            raise ValueError("Allowed RCT paper contains no sentences")
        text = "\n".join(s["text"] for s in current["sentences"])
        cursor, sections = 0, []
        for sentence in current["sentences"]:
            sentence.update(start=cursor, end=cursor + len(sentence["text"]))
            validate_span(text, sentence)
            sections.append({"label": sentence["label"], "start": sentence["start"], "end": sentence["end"]})
            cursor = sentence["end"] + 1
        documents.append({**current, "text": text, "text_sha256": _sha(text), "sections": sections,
                          "source_sha256": source_hash, "raw_article_sha256": _sha("".join(original)),
                          "source_text_assembly": "Native sentence text joined by LF; labels preserved separately",
                          "official_split": "train"})

    source_hash = _filehash(path)
    with Path(path).open(encoding="utf-8", newline="") as stream:
        for raw in stream:
            line = raw.rstrip("\r\n")
            if line.startswith("###"):
                finish()
                pmid = line[3:].strip()
                if not pmid.isdecimal() or pmid in seen:
                    raise ValueError("Invalid or duplicate RCT PMID")
                seen.add(pmid)
                current = {"pmid": pmid, "sentences": []} if pmid in allowed else None
                original = [raw] if current is not None else []
            elif current is not None:
                original.append(raw)
                if line.strip():
                    if "\t" not in line:
                        raise ValueError("Malformed allowed RCT sentence annotation")
                    label, text = line.split("\t", 1)
                    if label not in RCT_LABELS or not text:
                        raise ValueError("Unknown native RCT sentence label")
                    current["sentences"].append({"label": label, "text": text})
    finish()
    if {d["pmid"] for d in documents} != allowed or len(documents) != len(allowed):
        raise ValueError("Missing allowed RCT training PMIDs")
    return documents


def prepare_clinical(source_directory, allowed_manifest, output, protection=None, allowed_manifest_sha256=None):
    source_directory, output = Path(source_directory), Path(output).resolve()
    if output.exists():
        raise FileExistsError("Frozen clinical output already exists")
    raw = Path(allowed_manifest).read_bytes()
    allowed_hash = _sha(raw)
    if allowed_manifest_sha256 and allowed_manifest_sha256 != allowed_hash:
        raise ValueError("Trusted allowed-manifest SHA-256 mismatch")
    allowed = json.loads(raw)
    if allowed.get("training_split") != "train" or allowed.get("test_used_for_selection") is not False:
        raise ValueError("Trusted manifest must record train-only model selection")
    ids = {kind: _ids(allowed["training_pmids"][kind], kind) for kind in ("ebm", "rct")}
    hashes = allowed["source_hashes"]
    observed_hashes = {}
    for name in ("ebm_nlp_2_00.tar.gz", "rct-train.txt"):
        observed_hashes[name] = _filehash(source_directory / name)
        if hashes.get(name) != observed_hashes[name]:
            raise ValueError("Official clinical training source hash mismatch: " + name)
    protected_pmids = set()
    excluded = allowed.get("excluded_pmids")
    if not isinstance(excluded, dict) or any(not isinstance(v, list) for v in excluded.values()):
        raise ValueError("Trusted held-out PMID exclusion audit required")
    for values in excluded.values():
        if any(not re.fullmatch(r"[0-9]+", str(p)) for p in values):
            raise ValueError("Invalid held-out PMID metadata")
        protected_pmids.update(map(str, values))
    protection_hash, protected_texts = None, set()
    if protection is not None:
        protection_raw = Path(protection).read_bytes()
        protection_hash = _sha(protection_raw)
        value = json.loads(protection_raw)
        if value.get("source_only") is not True or not isinstance(value.get("pmids"), list):
            raise ValueError("Protection input must contain source-only PMID metadata")
        if any(key in value for key in ("labels", "relations", "annotations", "predictions")):
            raise ValueError("Protected labels must not be opened by training preparation")
        if any(not re.fullmatch(r"[0-9]+", str(p)) for p in value["pmids"]):
            raise ValueError("Invalid protected PMID metadata")
        text_hashes = value.get("normalized_text_hashes", value.get("text_hashes", []))
        if not isinstance(text_hashes, list) or any(not re.fullmatch(r"[a-fA-F0-9]{64}", str(h)) for h in text_hashes):
            raise ValueError("Invalid protected normalized text SHA-256 metadata")
        protected_pmids.update(map(str, value["pmids"]))
        protected_texts.update(h.lower() for h in text_hashes)
    if (ids["ebm"] | ids["rct"]) & protected_pmids:
        raise ValueError("Allowed training IDs overlap protected PMID audit")
    documents = _ebm(source_directory / "ebm_nlp_2_00.tar.gz", ids["ebm"])
    rct = _rct(source_directory / "rct-train.txt", ids["rct"])
    # Source-only normalized hashes use the same v8 corpus policy; no test labels.
    import unicodedata
    def text_hash(text):
        return _sha(" ".join(unicodedata.normalize("NFKC", text).casefold().split()))
    overlap = sum(str(d["pmid"]) in protected_pmids or text_hash(d["text"]) in protected_texts for d in documents + rct)
    if overlap:
        raise ValueError("Actual selected clinical training documents overlap protected sources")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.with_name(output.name + ".incomplete-" + uuid.uuid4().hex[:12])
    staging.mkdir()
    _write(staging / "data/clinical-train.json", {"split": "train", "corpus": "ebmnlp", "documents": documents})
    _write(staging / "data/rct-train.json", {"split": "train", "corpus": "pubmed-rct", "documents": rct})
    training_hash = _filehash(staging / "data/clinical-train.json")
    audit = {"passed": True, "protected_overlap": overlap, "audited_training_sha256": training_hash,
             "audited_rct_training_sha256": _filehash(staging / "data/rct-train.json"),
             "sourcehashes": observed_hashes, "source_hashes": observed_hashes,
             "allowed_manifest_sha256": allowed_hash, "protection_sha256": protection_hash,
             "protected_pmids_checked": len(protected_pmids), "protected_normalized_text_hashes_checked": len(protected_texts),
             "all_allowed_ids_present": True, "training_documents": {"ebm": len(documents), "rct": len(rct)},
             "pio_annotated_documents": {role: sum(role in d["annotated_labels"] for d in documents) for role in sorted(PIO)},
             "missing_pio_annotation_is_negative": False,
             "actual_training_pmids_sha256": {k: _sha("\n".join(sorted(v, key=int))) for k, v in ids.items()},
             "protected_annotation_members_opened": False, "evidence_inference_ico_converted_to_bio": False,
             "native_labels_validated": True, "unicode_offsets_validated": True,
             "optional_protection_provided": protection is not None,
             "protection_scope": "trusted-manifest held-out PMID exclusions plus optional source-only protection"}
    _write(staging / "data/clinical-audit.json", audit)
    files = [{"path": str(p.relative_to(staging)).replace("\\", "/"), "sha256": _filehash(p)}
             for p in sorted((staging / "data").glob("*.json"))]
    manifest = {"format": "evidence-v8-clinical-native-train/1", "split": "train", "files": files,
                "allowed_manifest_sha256": allowed_hash, "training_documents": audit["training_documents"],
                "protected_overlap": 0, "passed": True}
    _write(staging / "manifest.json", manifest)
    if output.exists():
        raise FileExistsError("Frozen output appeared during preparation")
    staging.rename(output)
    return {**manifest, "output": str(output)}
