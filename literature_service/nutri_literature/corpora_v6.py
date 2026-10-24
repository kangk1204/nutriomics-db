"""Train-only imports with protected comparison/test source auditing."""

from pathlib import Path
import json
import zipfile
import csv
import io
import tarfile
import time

from .benchmarks import _write_immutable
from .db import digest
from .training_v4 import text_key


def official_relation_train(release, output):
    """Read only the official train member; retain explicit undirected labels."""
    from .corpora_v5 import RELEASE_SHA, TRAIN_MEMBER
    from .public_corpora import parse_pubtator

    raw = Path(release).read_bytes()
    if digest(raw) != RELEASE_SHA:
        raise ValueError("Official relation release checksum differs")
    with zipfile.ZipFile(Path(release)) as archive:
        member = archive.read(TRAIN_MEMBER).decode()
    documents = parse_pubtator(member)
    annotated = {}
    for line in member.splitlines():
        cols = line.split("\t")
        if len(cols) == 4 and cols[-1].startswith("Subject:"):
            annotated[(cols[0], frozenset(cols[1:3]))] = cols[-1][8:]
    for doc in documents:
        for relation in doc["relations"]:
            key = (doc["pmid"], frozenset((relation["entity1"], relation["entity2"])))
            # Official BioREDirect data_processor maps a positive relation
            # without a subject field to No_Direct. This policy applies only to
            # this checksum-pinned release, not arbitrary unannotated corpora.
            relation["direction_annotated"] = True
            if key not in annotated or annotated[key] in {"None", "-"}:
                relation["role_direction_annotation"] = "undirected"
    value = {
        "split": "train",
        "corpus": "bioredirect",
        "documents": documents,
        "release_sha256": RELEASE_SHA,
        "member": TRAIN_MEMBER,
        "member_sha256": digest(member),
        "test_labels_read": False,
        "direction_policy": "positive relation + missing subject = official No_Direct",
        "direction_reference": "https://github.com/ncbi-nlp/BioREDirect/blob/main/src/data_processor.py",
    }
    _write_immutable(Path(output), value)
    return {"articles": len(documents), "source": str(output), "sha256": digest(Path(output).read_bytes())}


def audit_training(training, protected_inputs, output, protected_pmids=(), prior_audit=None):
    value = json.loads(Path(training).read_bytes())
    if value.get("split") != "train":
        raise ValueError("Only official training partitions may be audited for fitting")
    ids = set(map(str, protected_pmids))
    pmcids = set()
    hashes = set()
    records = []
    prior_sha = None
    if prior_audit is not None:
        raw = Path(prior_audit).read_bytes()
        prior = json.loads(raw)
        if not prior.get("passed") or prior.get("protected_overlap") != 0:
            raise ValueError("Inherited protected source audit must have passed")
        ids.update(prior["protected_pmids"])
        hashes.update(prior["protected_text_hashes"])
        pmcids.update(prior.get("protected_pmcids", []))
        records.extend(prior["protected_sources"])
        prior_sha = digest(raw)
    for path in protected_inputs:
        path = Path(path)
        raw = path.read_bytes()
        protected = json.loads(raw)
        docs = protected.get("documents", [])
        for doc in docs:
            ids.add(str(doc["pmid"]))
            if doc.get("pmcid"):
                pmcids.add(str(doc["pmcid"]).upper())
            hashes.add(text_key(doc["text"]))
        records.append(
            {
                "path": str(path),
                "filename": path.name,
                "sha256": digest(raw),
                "articles": len(docs),
                "labels_read": False,
            }
        )
    included, removed = [], []
    seen_ids, seen_text, seen_pmcids = set(), set(), set()
    for doc in value["documents"]:
        pmid, key = str(doc["pmid"]), text_key(doc["text"])
        pmcid = str(doc.get("pmcid", "")).upper()
        leak = pmid in ids or key in hashes or bool(pmcid and pmcid in pmcids)
        duplicate = pmid in seen_ids or key in seen_text or bool(pmcid and pmcid in seen_pmcids)
        (removed if leak or duplicate else included).append(doc)
        seen_ids.add(pmid)
        seen_text.add(key)
        if pmcid:
            seen_pmcids.add(pmcid)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    training_path = output / "relation-train.json"
    _write_immutable(training_path, {"split": "train", "documents": included, "protected_overlap": 0})
    audit = {
        "passed": True,
        "inherited_protection_sha256": prior_sha,
        "protected_overlap": 0,
        "training_source_sha256": digest(Path(training).read_bytes()),
        "audited_training_sha256": digest(training_path.read_bytes()),
        "training_pmids": sorted(str(d["pmid"]) for d in included),
        "excluded_pmids": sorted(str(d["pmid"]) for d in removed),
        "protected_sources": records,
        "protected_pmids": sorted(ids),
        "protected_text_hashes": sorted(hashes),
        "protected_pmcids": sorted(pmcids),
        "training_pmcid_missing": sum(not d.get("pmcid") for d in included),
        "pmcid_audit": "explicit IDs only; missing PMCID remains unknown",
        "pretraining_overlap": "unknown",
        "test_labels_read": False,
        "nutrition_accuracy": "unmeasured",
    }
    audit["bc8_annotation_labels_read"] = False
    audit["train_heldout_overlap"] = []
    _write_immutable(output / "audit.json", audit)
    return audit


def merge_training(paths, output):
    """Preserve corpus task coverage; the subsequent audit removes duplicate papers."""
    documents, sources = [], []
    for path in paths:
        path = Path(path)
        value = json.loads(path.read_bytes())
        if value.get("split") != "train" or value.get("test_labels_read") is True:
            raise ValueError("Merging requires official train-only inputs")
        corpus = value.get("corpus")
        for doc in value["documents"]:
            copy = dict(doc)
            if corpus in {"nlmgene", "nlmchem"}:
                copy["relation_annotation_complete"] = False
                copy["gene_annotation_complete"] = corpus == "nlmgene"
            documents.append(copy)
        sources.append(
            {
                "filename": path.name,
                "sha256": digest(path.read_bytes()),
                "corpus": corpus,
                "articles": len(value["documents"]),
            }
        )
    value = {"split": "train", "documents": documents, "sources": sources, "test_labels_read": False}
    _write_immutable(Path(output), value)
    return {
        "articles_before_overlap_audit": len(documents),
        "sources": sources,
        "sha256": digest(Path(output).read_bytes()),
    }


def protected_relation_source(release, output):
    """Extract only BC8 title/abstract lines; no annotation interpretation."""
    from .corpora_v5 import RELEASE_SHA, FINAL_MEMBER, source_only

    if digest(Path(release).read_bytes()) != RELEASE_SHA:
        raise ValueError("Official protected release changed")
    with zipfile.ZipFile(Path(release)) as archive:
        member = archive.read(FINAL_MEMBER).decode()
    value = {
        "split": "test",
        "corpus": "bc8",
        "documents": source_only(member),
        "release_sha256": RELEASE_SHA,
        "final_member_sha256": digest(member),
        "labels_read": False,
    }
    _write_immutable(Path(output), value)
    return value


def download_train_corpora(directory):
    """Official NLM-Gene/Chem train members only; sealed annotations unopened."""
    from .corpora_v4 import bioc_xml
    import requests

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    receipts = []

    def fetch(url, name):
        path = directory / name
        if not path.exists():
            response = requests.get(url, timeout=(15, 180))
            response.raise_for_status()
            raw = response.content
            preserved = directory / (digest(raw) + "-" + name)
            if not preserved.exists():
                preserved.write_bytes(raw)
            path.write_bytes(raw)
        receipts.append({"url": url, "file": name, "sha256": digest(path.read_bytes())})
        return path.read_bytes()

    base = "https://ftp.ncbi.nlm.nih.gov/pub/lu/NLMGene/"
    ids = fetch(base + "Pmidlist.Train.txt", "nlmgene-train-ids.txt").decode().split()
    gene = []
    for i, pmid in enumerate(ids):
        gene.extend(
            bioc_xml(fetch(base + "Corpus/" + pmid + ".BioC.XML", "nlmgene-train-" + pmid + ".xml"), True)
        )
        if (i + 1) % 25 == 0:
            print("NLM-Gene train " + str(i + 1) + "/" + str(len(ids)), flush=True)
        time.sleep(0.15)
    raw = fetch("https://ftp.ncbi.nlm.nih.gov/pub/lu/NLMChem/NLM-Chem-corpus.zip", "nlmchem-release.zip")
    chemicals = []
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = archive.namelist()
        train_list = next((n for n in names if n.endswith("pmcids_train.txt")), None)
        if train_list is None:
            raise ValueError("Official chemical train partition metadata missing")
        for identifier in archive.read(train_list).decode().split():
            name = next((n for n in names if n.endswith("/ALL/" + identifier + "_v1.xml")), None)
            if name is None:
                raise ValueError("Official chemical train article missing")
            chemicals.extend(bioc_xml(archive.read(name)))
    for name, documents in (("nlmgene", gene), ("nlmchem", chemicals)):
        for doc in documents:
            doc["relation_annotation_complete"] = False
            doc["gene_annotation_complete"] = name == "nlmgene"
        _write_immutable(
            directory / (name + "-train.json"),
            {
                "split": "train",
                "corpus": name,
                "documents": documents,
                "tasks": ["gene_detection", "normalization"]
                if name == "nlmgene"
                else ["chemical_normalization"],
                "test_labels_read": False,
                "sources": receipts,
            },
        )
    return {
        "nlmgene_train": len(gene),
        "nlmchem_train": len(chemicals),
        "sources": receipts,
        "test_labels_read": False,
    }


def _inference_flag(value):
    """The official v2 CSV writes True/False; older fixtures use 1/0."""
    normalized = str(value).strip().casefold()
    if normalized in {"true", "1"}:
        return True
    if normalized in {"false", "0"}:
        return False
    raise ValueError("Unknown Evidence Inference validity flag: " + repr(value))


def _inference_documents(release, split):
    """ICO prompts are descriptions, not fabricated literal span annotations."""
    if split not in {"train", "development"}:
        raise ValueError("This adapter cannot open sealed Evidence Inference test annotations")
    with tarfile.open(release, "r:gz") as archive:

        def read(name):
            handle = archive.extractfile(name)
            if handle is None:
                raise ValueError("Missing Evidence Inference release member")
            return handle.read().decode("utf-8-sig")

        train = set(read("splits/ev2_train_article_ids.txt").split())
        dev = set(read("splits/ev2_validation_article_ids.txt").split())
        test = set(read("splits/ev2_test_article_ids.txt").split())
        if train & (dev | test) or dev & test:
            raise ValueError("Official Evidence Inference partitions overlap")
        selected = train if split == "train" else dev
        prompts = {
            r["PromptID"]: r
            for r in csv.DictReader(io.StringIO(read("prompts_merged.csv")))
            if r["PMCID"] in selected
        }
        annotations = {key: [] for key in prompts}
        for row in csv.DictReader(io.StringIO(read("annotations_merged.csv"))):
            if (row["PromptID"] in annotations and _inference_flag(row["Valid Label"])
                    and _inference_flag(row["Valid Reasoning"])):
                annotations[row["PromptID"]].append(row)
        documents = []
        for identifier in sorted(selected):
            text = read("txt_files/PMC" + identifier + ".txt")
            comparisons = []
            for key, prompt in prompts.items():
                if prompt["PMCID"] != identifier:
                    continue
                evidence = []
                labels = []
                for row in annotations[key]:
                    try:
                        lo, hi = int(row["Evidence Start"]), int(row["Evidence End"])
                    except ValueError:
                        continue
                    # Official -1/-1 means no annotated evidence span. It must
                    # neither become a negative offset nor fabricate evidence.
                    if (lo, hi) == (-1, -1):
                        continue
                    if not 0 <= lo < hi <= len(text):
                        raise ValueError("Official training evidence/source offset mismatch")
                    evidence.append({"start": lo, "end": hi, "text": text[lo:hi]})
                    labels.append(row.get("Label Code", row.get("Label")))
                if not evidence:
                    continue
                comparisons.append(
                    {
                        "prompt_id": key,
                        "intervention_description": prompt["Intervention"],
                        "comparator_description": prompt["Comparator"],
                        "outcome_description": prompt["Outcome"],
                        "descriptions_are_literal_spans": False,
                        "evidence": evidence,
                        "labels": labels,
                    }
                )
            documents.append(
                {
                    "pmid": "PMC" + identifier,
                    "pmcid": "PMC" + identifier,
                    "text": text,
                    "sections": [{"start": 0, "end": len(text), "label": "FullText"}],
                    "comparisons": comparisons,
                    "source_hash": digest(text),
                    "pmid_mapping_known": False,
                }
            )
    return documents


def _map_inference_ids(documents, idmap):
    if idmap is None:
        return
    value = json.loads(Path(idmap).read_bytes())
    mapping = {}
    for row in value.get("records", []):
        if row.get("pmid") and row.get("pmcid"):
            pmcid, pmid = str(row["pmcid"]).upper(), str(row["pmid"])
            if not pmid.isdigit() or pmcid in mapping and mapping[pmcid] != pmid:
                raise ValueError("Official PMID mapping is invalid or ambiguous")
            mapping[pmcid] = pmid
    for doc in documents:
        if doc["pmcid"] in mapping:
            doc["pmid"] = mapping[doc["pmcid"]]
            doc["pmid_mapping_known"] = True


def inference_train(release, output, idmap=None):
    documents = _inference_documents(release, "train")
    _map_inference_ids(documents, idmap)
    value = {
        "split": "train",
        "corpus": "evidenceinference",
        "documents": documents,
        "test_labels_read": False,
        "release_sha256": digest(Path(release).read_bytes()),
        "official_idmap_sha256": digest(Path(idmap).read_bytes()) if idmap else None,
        "tasks": ["evidence_linking", "oracle_ICO", "automatic_ICO"],
    }
    _write_immutable(Path(output), value)
    return {
        "articles": len(documents),
        "sha256": digest(Path(output).read_bytes()),
        "test_labels_read": False,
    }


def inference_development(release, output, idmap=None):
    """Separate source-only development texts from prompt/evidence annotations."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    documents = _inference_documents(release, "development")
    _map_inference_ids(documents, idmap)
    inputs = {
        "corpus": "evidenceinference",
        "split": "development",
        "documents": [
            {
                key: doc[key]
                for key in ("pmid", "pmcid", "text", "sections", "source_hash", "pmid_mapping_known")
            }
            for doc in documents
        ],
    }
    gold = {"corpus": "evidenceinference", "split": "development", "documents": documents}
    _write_immutable(output / "inputs.json", inputs)
    _write_immutable(output / "gold.json", gold)
    spec = {
        "corpus": "evidenceinference",
        "official_split": "development",
        "inputs_path": str(output / "inputs.json"),
        "inputs_sha256": digest((output / "inputs.json").read_bytes()),
        "gold_path": str(output / "gold.json"),
        "gold_sha256": digest((output / "gold.json").read_bytes()),
        "release_sha256": digest(Path(release).read_bytes()),
        "papers": len(documents),
        "test_labels_read": False,
        "official_idmap_sha256": digest(Path(idmap).read_bytes()) if idmap else None,
    }
    _write_immutable(output / "spec.json", spec)
    return spec


def outcome_train(release, output):
    """Import official EBM starting-span training, never its sealed test labels."""
    from .public_corpora import _archive_members, _ebm_members

    raw = Path(release).read_bytes()
    documents = _ebm_members(_archive_members(raw), "development")
    value = {
        "split": "train",
        "corpus": "ebmnlp",
        "official_partition": "train",
        "documents": documents,
        "release_sha256": digest(raw),
        "test_labels_read": False,
        "tasks": ["participants", "interventions", "outcomes"],
    }
    _write_immutable(Path(output), value)
    return {
        "articles": len(documents),
        "sha256": digest(Path(output).read_bytes()),
        "test_labels_read": False,
    }


def inference_idmap(release, output):
    """Map train/dev article IDs through official NCBI links with raw receipts."""
    import requests
    from defusedxml import ElementTree
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    with tarfile.open(release, "r:gz") as archive:
        identifiers = set()
        for member in ("splits/ev2_train_article_ids.txt", "splits/ev2_validation_article_ids.txt"):
            handle = archive.extractfile(member)
            if handle is None:
                raise ValueError("Official inference article partition missing")
            identifiers.update(handle.read().decode().split())
    if any(not identifier.isdigit() for identifier in identifiers):
        raise ValueError("Official inference article IDs must be numeric PMCID suffixes")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi"
    session = requests.Session()
    session.mount(
        "https://",
        HTTPAdapter(max_retries=Retry(total=3, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504))),
    )
    records, receipts = [], []
    ordered = sorted(identifiers)
    for begin in range(0, len(ordered), 100):
        chunk = ordered[begin : begin + 100]
        path = output / ("elink-" + digest(json.dumps(chunk)) + ".xml")
        if not path.exists():
            params = [("dbfrom", "pmc"), ("db", "pubmed"), ("linkname", "pmc_pubmed")] + [
                ("id", identifier) for identifier in chunk
            ]
            response = session.get(url, params=params, timeout=(15, 60))
            response.raise_for_status()
            raw = response.content
            (output / (digest(raw) + ".response.xml")).write_bytes(raw)
            path.write_bytes(raw)
        root = ElementTree.fromstring(
            path.read_bytes(), forbid_dtd=False, forbid_entities=True, forbid_external=True
        )
        for link in root.findall("LinkSet"):
            source = link.findtext("IdList/Id")
            targets = {
                node.text
                for group in link.findall("LinkSetDb")
                if group.findtext("LinkName") == "pmc_pubmed"
                for node in group.findall("Link/Id")
            }
            if source in chunk and len(targets) == 1:
                records.append({"pmcid": "PMC" + source, "pmid": next(iter(targets))})
        receipts.append(
            {
                "url": url,
                "requested_pmcids": ["PMC" + identifier for identifier in chunk],
                "response_sha256": digest(path.read_bytes()),
                "file": path.name,
            }
        )
        time.sleep(0.4)
    value = {
        "records": records,
        "source_receipts": receipts,
        "requested_articles": len(ordered),
        "unmapped_pmcids": sorted(
            {"PMC" + identifier for identifier in ordered} - {row["pmcid"] for row in records}
        ),
        "test_annotations_read": False,
        "release_sha256": digest(Path(release).read_bytes()),
    }
    _write_immutable(output / "id-map.json", value)
    return {
        "path": str(output / "id-map.json"),
        "mapped": len(records),
        "unmapped": len(value["unmapped_pmcids"]),
        "sha256": digest((output / "id-map.json").read_bytes()),
    }
