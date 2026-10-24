"""CPU candidate providers and taxon-specific, collision-preserving gene identity.

Prediction providers never identify genes by themselves. Canonical identities
require a frozen native dictionary and independently supported taxon.
"""
from collections import defaultdict
import csv
import gzip
import hashlib
import io
import json
from pathlib import Path
import re

from .db import digest, dumps
from .extraction_v3 import abbreviations, _mentions
from .chemical_v3 import augment_mentions
from .nlp_v2 import cpu_inference_lock, ParserUnavailable

TAXA = {9606: ("Homo_sapiens", "human"), 10090: ("Mus_musculus", "mouse"),
        10116: ("Rattus_norvegicus", "rat")}
SPECIES = {9606: r"\b(?:human|humans|patients?|participants?|volunteers?|Homo sapiens|HeLa|HEK[- ]?293|HepG2|HUVEC|MCF[- ]?7)\b",
           10090: r"\b(?:mice|mouse|murine|Mus musculus)\b",
           10116: r"\b(?:rats?|Rattus norvegicus)\b"}


def directory_hash(path):
    value = hashlib.sha256()
    for file in sorted(Path(path).rglob("*")):
        if file.is_file() and "__pycache__" not in file.parts and file.suffix != ".pyc":
            value.update(file.relative_to(path).as_posix().encode() + b"\0")
            with file.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1048576), b""):
                    value.update(chunk)
    return value.hexdigest()


def term_key(text):
    return " ".join(re.findall(r"\w+", text.casefold()))


def source_chunks(text, limit=12000):
    """Contiguous Unicode slices; a long line must never silently lose its prefix."""
    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        if end < len(text):
            boundary = max(text.rfind("\n", start + limit // 2, end),
                           text.rfind(" ", start + limit // 2, end))
            if boundary >= 0:
                end = boundary + 1
        yield start, text[start:end]
        start = end


class GeneIndex:
    def __init__(self, snapshot):
        self.manifest = snapshot["manifest"]
        self.records = snapshot["records"]
        if digest(dumps(self.records)) != self.manifest["records_sha256"]:
            raise ValueError("Gene snapshot content hash differs")
        self.index = defaultdict(list)
        for record in self.records:
            for term in [record["symbol"], record["name"], *record["aliases"]]:
                if term_key(term) and record not in self.index[term_key(term)]:
                    self.index[term_key(term)].append(record)

    @classmethod
    def load(cls, path):
        return cls(json.loads(Path(path).read_bytes()))


def build_gene_snapshot(directory):
    import requests
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    records, sources = [], []
    for taxon, (name, _) in TAXA.items():
        url = f"https://ftp.ncbi.nlm.nih.gov/gene/DATA/GENE_INFO/Mammalia/{name}.gene_info.gz"
        response = requests.get(url, timeout=(15, 180))
        response.raise_for_status()
        raw = response.content
        sha = digest(raw)
        path = directory / f"{taxon}-{sha}.gene_info.gz"
        if not path.exists():
            path.write_bytes(raw)
        rows = csv.DictReader(io.StringIO(gzip.decompress(raw).decode()), delimiter="\t")
        added, excluded_taxa = 0, defaultdict(int)
        for row in rows:
            if int(row.get("#tax_id", row.get("tax_id", 0))) != taxon:
                excluded_taxa[row.get("#tax_id",row.get("tax_id"))] += 1
                continue
            aliases = [s for field in ("Synonyms", "Other_designations", "Symbol_from_nomenclature_authority",
                                      "Full_name_from_nomenclature_authority")
                       for s in row.get(field, "-").split("|") if s and s != "-"]
            records.append({"id": "NCBIGene:" + row["GeneID"], "native_id": row["GeneID"],
                            "taxon_id": taxon, "symbol": row["Symbol"], "name": row["description"],
                            "aliases": sorted(set(aliases)), "gene_type": row["type_of_gene"],
                            "hgnc_ids": re.findall(r"HGNC:HGNC:(\d+)", row.get("dbXrefs", ""))})
            added += 1
        if added < 10000:
            raise ValueError("Native species gene snapshot appears incomplete")
        sources.append({"url": url, "raw_sha256": sha, "taxon_id": taxon, "records": added,
                        "excluded_subtaxa": dict(excluded_taxa)})
    snapshot = {"manifest": {"schema": "native-gene-v4", "records_sha256": digest(dumps(records)),
                              "sources": sources, "taxa": list(TAXA)}, "records": records}
    path = directory / (snapshot["manifest"]["records_sha256"] + ".json")
    if not path.exists():
        path.write_text(dumps(snapshot), encoding="utf-8")
    return path


def taxon_context(line, document, metadata):
    def find(value):
        return {taxon for taxon, pattern in SPECIES.items() if re.search(pattern, value, re.I)}
    local = find(line)
    global_taxa = find(document)
    mesh = " ".join(str(v) for v in metadata.get("mesh", []))
    if not global_taxa:
        global_taxa = find(mesh)
    supplied = set(metadata.get("taxon_ids", [])) & set(TAXA)
    if not global_taxa:
        global_taxa = supplied
    taxa = local or global_taxa
    model = "cell" if re.search(r"\b(?:cells?|cell[- ]lines?|in vitro|HeLa|HEK|HepG2|HUVEC|MCF)\b", line, re.I) else (
        "animal" if taxa & {10090, 10116} else
        "clinical" if re.search(r"\b(?:patients?|participants?|volunteers?|clinical trial)\b", line, re.I) else "unknown")
    return (next(iter(taxa)) if len(taxa) == 1 else None), model, (
        "local_text" if local else "document_text" if find(document) else "source_metadata")


def normalize_gene(mention, line, text, index, metadata, definitions):
    value = {**mention, "payload": dict(mention.get("payload", {}))}
    candidates = list(index.index.get(term_key(value["text"]), []))
    active = [d for d in definitions.get(value["text"], []) if d["short_start"] <= value["start"]]
    if active:
        long_candidates = list(index.index.get(term_key(active[-1]["long"]), []))
        value["payload"]["abbreviation_definition"] = active[-1]
        if not long_candidates:
            value.update(normalized_id=None, status="needs_review")
            value["payload"].update(ambiguous=True, identity_reason="abbreviation_conflict")
            return value
        candidates = long_candidates
    taxon, model, taxon_source = taxon_context(line, text, metadata)
    resolved = {r["id"]: r for r in candidates if r["taxon_id"] == taxon}
    group = bool(re.search(r"\b(?:family|families|isoforms|superfamily)\b", value["text"], re.I))
    reason = "gene_group" if group else "species_unresolved" if taxon is None else (
        "alias_collision" if len(resolved) > 1 else "identity_unresolved" if not resolved else None)
    record = next(iter(resolved.values())) if len(resolved) == 1 and not group else None
    value.update(normalized_id=record["id"] if record else None, status="candidate" if record else "needs_review")
    value["payload"].update({"taxon_id": taxon, "taxon_source": taxon_source, "study_model": model,
                             "species": "human" if taxon == 9606 else "animal" if taxon else "unknown",
                             "candidate_ids": sorted({r["id"] for r in candidates}),
                             "identity_reason": reason, "ambiguous": bool(group or len(resolved) > 1),
                             "native_id": record["native_id"] if record else None,
                             "symbol": record["symbol"] if record else value["payload"].get("symbol")})
    return value


class CpuCandidates:
    def __init__(self, model_directory, gene_provider="biomedbert"):
        self.path = Path(model_directory)
        self.gene_provider = gene_provider
        self.gene = self.chemical = self.tokenizer = None
        self.manifest = {"available": False, "device": "cpu", "gene_provider": gene_provider}

    def load(self):
        if self.manifest.get("available"):
            return
        import spacy
        import en_ner_bc5cdr_md
        self.chemical = spacy.load("en_ner_bc5cdr_md", disable=["tagger", "parser", "lemmatizer"])
        self.manifest["chemical_model_sha256"] = directory_hash(Path(en_ner_bc5cdr_md.__file__).parent)
        if self.gene_provider == "biomedbert":
            import torch
            from transformers import AutoModelForTokenClassification, AutoTokenizer
            path = self.path / "gene" / "selected_model"
            raw = path / "model.safetensors"
            if not raw.exists() or digest(raw.read_bytes()) != "16b4c517cc3adaa654e2f7cfc2315939c5e93f5d7ad2d5b8d3fa3dc48e456971":
                raise ParserUnavailable("Pinned BC2GM checkpoint is missing or changed")
            torch.set_num_threads(1)
            self.tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True)
            self.gene = AutoModelForTokenClassification.from_pretrained(path, local_files_only=True).cpu().eval()
            if self.gene.config.id2label != {0: "O", 1: "B-GENE", 2: "I-GENE"}:
                raise ParserUnavailable("Gene model label mapping changed")
            self.manifest["gene_model_sha256"] = directory_hash(path)
        elif self.gene_provider == "bionlp13cg":
            import en_ner_bionlp13cg_md
            self.gene = spacy.load("en_ner_bionlp13cg_md", disable=["tagger", "parser", "lemmatizer"])
            self.manifest["gene_model_sha256"] = directory_hash(Path(en_ner_bionlp13cg_md.__file__).parent)
        else:
            raise ParserUnavailable("Unknown gene candidate provider")
        self.manifest["available"] = True

    def predict(self, text):
        self.load()
        result = []
        with cpu_inference_lock():
            # Preserve complete source coverage, including long XML sources.
            for offset, chunk in source_chunks(text):
                for ent in self.chemical(chunk).ents:
                    if ent.label_ == "CHEMICAL":
                        result.append({"kind": "component", "text": ent.text, "start": offset+ent.start_char,
                                       "end": offset+ent.end_char, "normalized_id": None, "status": "needs_review",
                                       "payload": {"origin": "bc5cdr"}})
            if self.gene_provider == "bionlp13cg":
                for offset, chunk in source_chunks(text):
                    for ent in self.gene(chunk).ents:
                        if ent.label_ == "GENE_OR_GENE_PRODUCT":
                            result.append({"kind": "gene", "text": ent.text, "start": offset+ent.start_char,
                                           "end": offset+ent.end_char, "payload": {"origin": "bionlp13cg"}})
            else:
                import torch
                tokens = [{"text": m.group(), "start": m.start(), "end": m.end()}
                          for m in re.finditer(r"\w+(?:[-']\w+)*|[^\w\s]", text)]
                labels = []
                cursor = 0
                while cursor < len(tokens):
                    stop = min(cursor+200, len(tokens))
                    while True:
                        encoded = self.tokenizer([t["text"] for t in tokens[cursor:stop]], is_split_into_words=True,
                                                 return_tensors="pt", truncation=False)
                        if encoded["input_ids"].shape[1] <= 256:
                            break
                        if stop == cursor+1 and encoded["input_ids"].shape[1] > 256:
                            raise ParserUnavailable("Single source token exceeds model budget; no silent truncation")
                        stop = cursor + max(1, (stop-cursor)//2)
                    with torch.inference_mode():
                        predicted = self.gene(**encoded).logits.argmax(-1)[0].tolist()
                    word_labels = {}
                    for wid, tag in zip(encoded.word_ids(), predicted):
                        if wid is not None:
                            word_labels.setdefault(wid, tag)
                    words = source_word_labels(tokens[cursor:stop], word_labels)
                    labels.extend(words)
                    cursor = stop
                start = None
                for i, tag in enumerate(labels+[0]):
                    if start is not None and tag != 2:
                        lo, hi = tokens[start]["start"], tokens[i-1]["end"]
                        result.append({"kind": "gene", "start": lo, "end": hi, "text": text[lo:hi],
                                       "payload": {"origin": "bc2gm_biomedbert"}})
                        start = None
                    if tag in (1, 2) and start is None:
                        start = i
        return result


def source_word_labels(tokens, word_labels):
    """Keep native positions when BERT removes an invisible control-only token.

    A dropped printable token is an error. Control/format characters and isolated
    combining marks are explicit nonentity tokens, never silently shifted labels.
    """
    import unicodedata
    if set(word_labels) - set(range(len(tokens))):
        raise ParserUnavailable("Tokenizer word ID outside native source")
    labels = []
    for index, token in enumerate(tokens):
        if index in word_labels:
            labels.append(word_labels[index])
        elif token["text"] and all(unicodedata.category(c) in {"Cc", "Cf", "Mn"} for c in token["text"]):
            labels.append(0)
        else:
            raise ParserUnavailable("Printable native source token omitted by tokenizer")
    return labels


def collect_candidates(text, sentences, dictionaries, metadata, provider, gene_index):
    if metadata.get("_gold_entities") is not None:
        return [{**m, "payload": dict(m.get("payload", {}))} for m in metadata["_gold_entities"]], abbreviations(text)
    base, definitions = _mentions(text, sentences, dictionaries, metadata)
    # Short/common dictionary aliases are not independent gene detections.
    # Full official multiword names remain a narrow lexical fallback. The CPU
    # detector owns other boundaries; unresolved detector candidates stay visible.
    base=[m for m in base if m["kind"]!="gene" or (len(re.findall(r"\w+",m["text"]))>=3
          and re.search(r"\b(?:gene|protein|receptor|transcription|kinase|factor)\b",m["text"],re.I))]
    base = augment_mentions(text, sentences, base, metadata.get("_chemical_lexicon"))
    predicted = provider.predict(text)+list(metadata.get("_external_entities",[]))
    if any(text[m["start"]:m["end"]] != m["text"] for m in predicted):
        raise ValueError("Model candidate differs from source")
    # Match chemicals to frozen native concepts; an NER label alone supplies no ID.
    known = {(term_key(m["text"]), m["kind"]): m for m in base}
    for value in predicted:
        if value["kind"] == "component":
            match = known.get((term_key(value["text"]), "component"))
            if match:
                value.update(normalized_id=match.get("normalized_id"), status=match["status"], payload=dict(match["payload"]))
    # A model label at an already identified nutrient/food span creates no new
    # identity. Keep its detection provenance on the existing concept instead.
    for value in predicted:
        authoritative=next((m for m in base if m["kind"] in {"nutrient","food","diet"}
                            and (m["start"],m["end"])==(value["start"],value["end"])),None)
        if value["kind"]=="component" and authoritative:
            value.update(kind=authoritative["kind"],normalized_id=authoritative.get("normalized_id"),
                         status=authoritative["status"],payload={**authoritative["payload"],"additional_detection":"bc5cdr"})
    # Prefer full gene mentions over dictionary substrings, retain distinct overlaps for review.
    model_genes = [m for m in predicted if m["kind"] == "gene"]
    base = [m for m in base if not (m["kind"] == "gene" and any(
        g["start"] <= m["start"] and m["end"] <= g["end"] and (g["start"],g["end"]) != (m["start"],m["end"])
        for g in model_genes))]
    merged = {(m["kind"],m["start"],m["end"]): m for m in base+predicted}
    result = []
    for value in merged.values():
        if value["kind"] == "gene":
            line = next((s["text"] for s in sentences if s["start"] <= value["start"] < s["end"]), "")
            value = normalize_gene(value, line, text, gene_index, metadata, definitions)
        result.append(value)
    return sorted(result, key=lambda m:(m["start"],m["end"],m["kind"])), definitions
