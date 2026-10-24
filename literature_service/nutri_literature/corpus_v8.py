"""Read-only, license- and evaluation-protected DAPT corpus export.

Only a source-only protection manifest is read; benchmark annotations and model
predictions are never opened. Unknown language/license/tokenization never becomes
an implicitly valid training sample.
"""
from collections import Counter, OrderedDict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import unicodedata
import uuid
from defusedxml import ElementTree as ET

FORMAT = "nutriomics-v8-source-corpus/1"
MAX_TOKENS = 50_000_000


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode("utf-8")).hexdigest()


def _filehash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def text_key(text):
    """Same NFKC/casefold/whitespace identity as evidence_v8.common.textkey."""
    return _sha(" ".join(unicodedata.normalize("NFKC", text).casefold().split()))


def _write_json(path, payload):
    with Path(path).open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(_canonical(payload))
        stream.flush()
        os.fsync(stream.fileno())


def _protection(path):
    raw = Path(path).read_bytes()
    payload = json.loads(raw)
    if payload.get("source_only") is not True:
        raise ValueError("Exclusion manifest must declare source_only=true")
    for forbidden in ("annotations", "relations", "labels", "predictions", "gold"):
        if forbidden in payload:
            raise ValueError("Exclusion input must not contain benchmark labels/predictions")
    required = ("pmids", "pmcids")
    if any(not isinstance(payload.get(k), list) for k in required):
        raise ValueError("Source-only PMID and PMCID exclusion lists are required")
    hashes = payload.get("normalized_text_hashes", payload.get("text_hashes"))
    if not isinstance(hashes, list) or any(not re.fullmatch(r"[a-fA-F0-9]{64}", h) for h in hashes):
        raise ValueError("Normalized source text SHA-256 exclusion list required")
    if any(not str(p).isdecimal() for p in payload["pmids"]):
        raise ValueError("Invalid protected PMID")
    if any(not re.fullmatch(r"PMC\d+", str(p).upper()) for p in payload["pmcids"]):
        raise ValueError("Invalid protected PMCID")
    sources = payload.get("public_gold_sources", [])
    if not isinstance(sources, list):
        raise ValueError("Invalid public-gold source audit")
    valid_sources = bool(sources) and all(
        isinstance(s, dict) and s.get("name") and s.get("split")
        and re.fullmatch(r"[a-fA-F0-9]{64}", str(s.get("sha256", ""))) for s in sources)
    ready = payload.get("all_public_gold_excluded") is True and valid_sources
    return {"raw": raw, "sha256": _sha(raw), "payload": payload,
            "pmids": set(map(str, payload["pmids"])), "pmcids": {str(p).upper() for p in payload["pmcids"]},
            "hashes": {h.lower() for h in hashes}, "ready": ready, "public_gold_sources": sources}


def textmining_license(value):
    """Conservative explicit reusable text licenses; never inherit a paper flag."""
    if not value:
        return None
    value = str(value).strip()
    normalized = re.sub(r"[ _]+", "-", value.casefold())
    if re.search(r"(?:^|[^a-z])(?:nc|nd)(?:[^a-z]|$)|non.?commercial|no.?deriv", normalized):
        return None
    if re.fullmatch(r"cc-?0(?:-?1\.0)?", normalized) or re.search(r"creativecommons\.org/publicdomain/(?:zero|mark)/1\.0", normalized):
        return "CC0-1.0" if "mark" not in normalized else "Public-Domain-Mark-1.0"
    match = re.fullmatch(r"cc-?by(-sa)?(?:-?(1\.0|2\.0|2\.5|3\.0|4\.0))?", normalized)
    if match:
        return "CC-BY" + ("-SA" if match[1] else "") + ("-" + match[2] if match[2] else "")
    match = re.search(r"https?://creativecommons\.org/licenses/by(-sa)?/(1\.0|2\.0|2\.5|3\.0|4\.0)/?(?:$|\s)", value.casefold())
    if match:
        return "CC-BY" + ("-SA" if match[1] else "") + "-" + match[2]
    return None


def _language(value):
    if value is None or not str(value).strip():
        return "unknown", False
    try:
        decoded = json.loads(value) if isinstance(value, str) and value.startswith("[") else value
    except json.JSONDecodeError:
        decoded = value
    values = decoded if isinstance(decoded, list) else re.split(r"[,;|]", str(decoded))
    values = {str(v).strip().casefold() for v in values if str(v).strip()}
    english = bool(values) and all(v in {"en", "eng", "english"} or re.fullmatch(r"en(?:-[a-z0-9]{2,8})+", v) for v in values)
    return ("en", True) if english else (";".join(sorted(values)) or "unknown", False)


def _counter(name, revision):
    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(name, revision=revision, use_fast=True)
    fingerprint = _sha(tokenizer.backend_tokenizer.to_str()) if hasattr(tokenizer, "backend_tokenizer") else None

    def count(text):
        return len(tokenizer(text, add_special_tokens=False, truncation=False,
                             return_attention_mask=False, return_token_type_ids=False)["input_ids"])
    return count, fingerprint


class ArchivedLanguage:
    """Bounded raw-SHA cache of official XML language metadata, never heuristics."""

    def __init__(self, limit=32):
        self.limit, self.cache = limit, OrderedDict()

    def read(self, raw_path, expected_sha, pmid):
        if not raw_path:
            return {"status": "archive_path_absent", "codes": [], "source_sha256": expected_sha}
        path = Path(raw_path)
        try:
            stat = path.stat()
            if stat.st_size > 50 * 1024 ** 2:
                return {"status": "archive_too_large", "codes": [], "source_sha256": expected_sha}
            key = (expected_sha, str(path), stat.st_mtime_ns, stat.st_size)
            if key in self.cache:
                result = self.cache.pop(key)
                self.cache[key] = result
            else:
                result = self._parse(path, expected_sha)
                self.cache[key] = result
                while len(self.cache) > self.limit:
                    self.cache.popitem(last=False)
            codes = result.get("papers", {}).get(str(pmid), result.get("jats_codes", []))
            evidence = {"status": result["status"], "source_sha256": expected_sha,
                        "codes": codes, "tag_path": result.get("tag_path")}
            if result["status"] == "verified" and not codes:
                evidence["status"] = "language_tag_absent"
            return evidence
        except (OSError, ValueError) as exc:
            return {"status": "archive_unavailable", "codes": [], "source_sha256": expected_sha,
                    "error_type": type(exc).__name__}

    @staticmethod
    def _parse(path, expected_sha):
        if _filehash(path) != expected_sha:
            return {"status": "hash_mismatch"}
        stack, papers, current, jats_codes = [], {}, None, []
        try:
            for event, element in ET.iterparse(path, events=("start", "end")):
                tag = element.tag.split("}")[-1]
                if event == "start":
                    stack.append(tag)
                    if len(stack) == 1 and tag == "article":
                        value = element.get("{http://www.w3.org/XML/1998/namespace}lang")
                        jats_codes = [value] if value else []
                    if tag == "PubmedArticle":
                        current = {"pmid": None, "codes": []}
                else:
                    if current is not None and stack[-2:] == ["MedlineCitation", "PMID"]:
                        current["pmid"] = "".join(element.itertext()).strip()
                    if current is not None and stack[-2:] == ["Article", "Language"] and "MedlineCitation" in stack:
                        current["codes"].append("".join(element.itertext()).strip())
                    if tag == "PubmedArticle" and current is not None:
                        if current["pmid"]:
                            papers.setdefault(current["pmid"], []).extend(current["codes"])
                        current = None
                    element.clear()
                    stack.pop()
        except Exception as exc:
            # defusedxml forbids entities/external declarations. XML failures are
            # unknown metadata, not evidence that the original text is English.
            return {"status": "invalid_xml", "error_type": type(exc).__name__}
        return {"status": "verified", "papers": papers, "jats_codes": jats_codes,
                "tag_path": "PubmedArticle/MedlineCitation/Article/Language" if papers else "/article/@xml:lang"}


def export_corpus(database, output, exclusion_manifest, max_tokens=MAX_TOKENS,
                  tokenizer_name=None, tokenizer_revision=None, token_counter=None, archived_language=True):
    """Export an immutable new directory, leaving .incomplete-* on interruption.

    documents.jsonl preserves licensed candidates; files[] lists only verified
    English, exactly tokenized training records. No-tokenizer exports are never
    declared DAPT-ready. Whole documents, without rewritten text, fit the budget.
    """
    database, output = Path(database).resolve(), Path(output).resolve()
    if output.exists():
        raise FileExistsError("Frozen corpus output already exists")
    if not database.is_file() or not 0 < max_tokens <= MAX_TOKENS:
        raise ValueError("Existing database and token budget 1..50000000 required")
    protection = _protection(exclusion_manifest)
    exact = tokenizer_name is not None
    if exact != (tokenizer_revision is not None) or (token_counter is not None and not exact):
        raise ValueError("Tokenizer name and immutable revision must be supplied together")
    if exact and not re.fullmatch(r"[a-fA-F0-9]{40}", str(tokenizer_revision)):
        raise ValueError("Exact tokenization requires a fixed 40-character HF revision")
    tokenizer_hash = None
    if exact and token_counter is None:
        token_counter, tokenizer_hash = _counter(tokenizer_name, tokenizer_revision)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.with_name(output.name + ".incomplete-" + uuid.uuid4().hex[:12])
    staging.mkdir()
    counts, token_total, budget_stopped = Counter(), 0, False
    source_digest = hashlib.sha256()
    language_digest, language_counts, archived = hashlib.sha256(), Counter(), ArchivedLanguage()
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    seen = sqlite3.connect(staging / "dedup.sqlite3")
    seen.execute("CREATE TABLE seen(text_key TEXT PRIMARY KEY,pmid TEXT NOT NULL)")
    try:
        connection.execute("PRAGMA temp_store=FILE")
        connection.execute("PRAGMA cache_size=-32768")
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        columns = {r[1] for r in connection.execute("PRAGMA table_info(papers)")}
        source_columns = {r[1] for r in connection.execute("PRAGMA table_info(sources)")}
        language_field = "p.language" if "language" in columns else "p.languages" if "languages" in columns else "NULL"
        raw_path_field = "s.raw_path" if "raw_path" in source_columns else "NULL"
        query = ("SELECT p.pmid,p.pmcid,p.pub_date,p.retracted," + language_field + " AS language,"
                 + raw_path_field + " AS raw_path,s.source_id,s.kind,s.sha256,s.text_hash,s.text_content,s.sections,s.license FROM papers p "
                 "JOIN sources s ON s.source_id=p.current_source_id "
                 "ORDER BY p.pub_date DESC,CAST(p.pmid AS INTEGER),s.source_id")
        with (staging / "documents.jsonl").open("x", encoding="utf-8", newline="\n") as candidates, \
             (staging / "training.jsonl").open("x", encoding="utf-8", newline="\n") as training:
            for row in connection.execute(query):
                counts["scanned"] += 1
                source_digest.update((_canonical([row["pmid"], row["source_id"], row["sha256"], row["text_hash"], row["license"]]) + "\n").encode())
                if row["retracted"]:
                    counts["excluded_retracted"] += 1
                    continue
                if row["kind"] not in {"fulltext", "pubmed"}:
                    counts["excluded_source_kind"] += 1
                    continue
                license_id = textmining_license(row["license"])
                if license_id is None:
                    counts["excluded_license_unknown_or_restricted"] += 1
                    continue
                text = row["text_content"]
                if not text or not text.strip():
                    counts["excluded_empty"] += 1
                    continue
                if _sha(text) != row["text_hash"]:
                    raise RuntimeError("Stored source text hash mismatch")
                if not re.fullmatch(r"[a-fA-F0-9]{64}", str(row["sha256"])):
                    raise RuntimeError("Invalid original source SHA-256")
                normalized = text_key(text)
                if (str(row["pmid"]) in protection["pmids"] or str(row["pmcid"] or "").upper() in protection["pmcids"]
                        or normalized in protection["hashes"]):
                    counts["excluded_protected_document"] += 1
                    continue
                if seen.execute("SELECT 1 FROM seen WHERE text_key=?", (normalized,)).fetchone():
                    counts["excluded_normalized_duplicate"] += 1
                    continue
                seen.execute("INSERT INTO seen VALUES(?,?)", (normalized, str(row["pmid"])))
                sections = json.loads(row["sections"] or "[]")
                for section in sections:
                    if not (isinstance(section.get("start"), int) and isinstance(section.get("end"), int)
                            and 0 <= section["start"] <= section["end"] <= len(text)):
                        raise RuntimeError("Stored Unicode source section offset mismatch")
                language, english = _language(row["language"])
                language_evidence = [{"kind": "paper_metadata", "field": language_field,
                                      "language": language, "verified_english": english}]
                if archived_language and raw_path_field != "NULL":
                    evidence = archived.read(row["raw_path"], row["sha256"], row["pmid"])
                    language_counts[evidence["status"]] += 1
                    language_evidence.append({"kind": "archived_xml", **evidence})
                    if evidence["status"] == "verified":
                        raw_language, raw_english = _language(evidence["codes"])
                        if not raw_english and len(set(c.casefold() for c in evidence["codes"])) > 1:
                            raw_language = "unknown"
                        if language == "unknown":
                            language, english = raw_language, raw_english
                        elif language != raw_language:
                            language, english = "unknown", False
                            language_counts["language_metadata_conflict"] += 1
                    else:
                        language, english = "unknown", False
                language_digest.update((_canonical([str(row["pmid"]), language_evidence]) + "\n").encode())
                reasons = []
                if not protection["ready"]:
                    reasons.append("global_public_gold_audit_incomplete")
                if not english:
                    reasons.append("language_unverified" if language == "unknown" else "non_english_or_multilingual")
                if not exact:
                    reasons.append("needs_tokenization")
                tokens = None
                if english and exact:
                    tokens = token_counter(text)
                    if not isinstance(tokens, int) or tokens <= 0:
                        raise RuntimeError("Invalid exact tokenizer count")
                if not reasons and token_total + tokens > max_tokens:
                    budget_stopped = True
                    counts["next_document_exceeds_token_budget"] += 1
                    break
                record = {"pmid": str(row["pmid"]), "pmcid": row["pmcid"], "text": text,
                          "source_sha256": row["sha256"], "text_sha256": row["text_hash"],
                          "normalized_text_sha256": normalized, "license": row["license"], "license_id": license_id,
                          "source_kind": row["kind"], "source_id": row["source_id"], "sections": sections,
                          "text_start": 0, "text_end": len(text), "language": language,
                          "language_metadata_verified": english, "language_evidence": language_evidence, "token_count": tokens,
                          "training_eligible": not reasons, "withheld_reasons": reasons, "source_only": True}
                candidates.write(_canonical(record) + "\n")
                counts["exported_candidates"] += 1
                for reason in reasons:
                    counts[reason] += 1
                if not reasons:
                    training.write(_canonical(record) + "\n")
                    counts["training_documents"] += 1
                    token_total += tokens
            for stream in (candidates, training):
                stream.flush()
                os.fsync(stream.fileno())
        connection.rollback()
        connection.close()
        seen.close()
        (staging / "dedup.sqlite3").unlink()
        protection_path = staging / "protection.json"
        protection_path.write_bytes(protection["raw"])
        if _filehash(protection_path) != protection["sha256"]:
            raise RuntimeError("Protection manifest copy integrity failure")
        # Re-read every JSONL to verify closed output, source text and row counts.
        file_receipts = {}
        for filename, expected in (("documents.jsonl", counts["exported_candidates"]), ("training.jsonl", counts["training_documents"])):
            total = 0
            with (staging / filename).open(encoding="utf-8") as stream:
                for line in stream:
                    record = json.loads(line)
                    if _sha(record["text"]) != record["text_sha256"] or len(record["text"]) != record["text_end"]:
                        raise RuntimeError("Output Unicode text integrity failure")
                    total += 1
            if total != expected:
                raise RuntimeError("Closed output row count mismatch")
            file_receipts[filename] = {"path": filename, "sha256": _filehash(staging / filename), "documents": total}
        ready = protection["ready"] and exact and counts["training_documents"] > 0
        status = ("waiting_global_public_gold_audit" if not protection["ready"] else "needs_tokenization" if not exact
                  else "ready" if counts["training_documents"] else "token_budget_too_small" if budget_stopped
                  else "needs_language_verification" if counts["language_unverified"]
                  else "no_english_documents" if counts["non_english_or_multilingual"] else "no_eligible_documents")
        manifest = {"format": FORMAT, "status": status, "training_ready": ready, "source_only": True,
                    "observed_at": datetime.now(timezone.utc).isoformat(), "pinned_sqlite_read_snapshot": True,
                    "source_selection_sha256": source_digest.hexdigest(), "default_engine": "heuristic-v1",
                    "counts": dict(counts), "actual_tokens": token_total if exact else None,
                    "max_tokens": max_tokens, "token_budget_verified": exact,
                    "budget_stopped": budget_stopped, "whole_documents_only": True,
                    "exact_tokenization": exact, "tokenizer": {"name": tokenizer_name, "revision": tokenizer_revision,
                    "backend_sha256": tokenizer_hash, "add_special_tokens": False, "truncation": False},
                    "files": [file_receipts["training.jsonl"]] if ready else [],
                    "candidate_files": [file_receipts["documents.jsonl"]],
                    "exclusions": "protection.json", "exclusion_manifest_sha256": protection["sha256"],
                    "protection_sha256": protection["sha256"], "all_public_gold_excluded": protection["ready"],
                    "public_gold_sources": protection["public_gold_sources"],
                    "deduplication": "NFKC+casefold+whitespace exact normalized text; no semantic near-duplicate claim",
                    "language_metadata_field": language_field, "archived_language_enabled": archived_language,
                    "language_evidence_sha256": language_digest.hexdigest(), "archived_language_counts": dict(language_counts),
                    "archived_language_paths": ["PubmedArticle/MedlineCitation/Article/Language", "/article/@xml:lang"],
                    "nutrition_accuracy": "unmeasured",
                    "model_predictions_used_as_gold": False, "sealed_annotations_opened": False}
        _write_json(staging / "manifest.json", manifest)
        if output.exists():
            raise FileExistsError("Frozen output appeared during export")
        staging.rename(output)
        return {**manifest, "output": str(output)}
    except BaseException as exc:
        try:
            connection.close()
            seen.close()
            _write_json(staging / "failure.json", {"status": "incomplete", "error": type(exc).__name__ + ": " + str(exc)})
        except Exception:
            pass
        raise
