"""Bounded public regulatory annotations, isolated from production and prediction.

SCREEN CRISPR pairs are tested pairs, not automatically validated regulation.
GraphRegLR predictions and Atlas eQTM associations remain distinct evidence.
No nearest-gene edges, controlled individual data, or cohort double counting.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import math
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from nutrigenomics.expansion import connect, coverage, insert, migrate, validate

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "research" / "2026-10-01" / "regulatory_expansion"
SCREEN = "https://screen.wenglab.org"
CRISPR_URL = "https://downloads.wenglab.org/V4-hg38.Gene-Links.CRISPR.txt.gz"
GRAPHREG_URL = "https://downloads.wenglab.org/V4-hg38.Gene-Links.Computational-Methods.GraphRegLR.txt.gz"
ATLAS_REPO = "https://api.github.com/repos/ads303/eQTM-Atlas"
ATLAS_UI = "https://shiny.crc.pitt.edu/eqtm_browser/?tab=downloads"
EVO_PAPER = "https://link.springer.com/article/10.1186/s13059-018-1601-3"
EVO_TABLE = "https://media.springernature.com/original/springer-static/esm/art%3A10.1186%2Fs13059-018-1601-3/MediaObjects/13059_2018_1601_MOESM5_ESM.xlsx"
HOSTS = {
    "screen.wenglab.org", "downloads.wenglab.org", "www.encodeproject.org",
    "api.github.com", "raw.githubusercontent.com", "shiny.crc.pitt.edu",
    "link.springer.com", "link.springernature.com", "media.springernature.com", "idp.springer.com",
    "eutils.ncbi.nlm.nih.gov", "pubmed.ncbi.nlm.nih.gov",
}
MAX_BYTES = 100_000_000
PANEL = frozenset({"ABCA1", "ABCG1", "APOE", "LDLR", "SREBF1", "PPARG", "PPARA",
                   "IL6", "TNF", "NFKB1", "NFE2L2", "FASN", "LPL", "CPT1A", "HMGCR", "SCD"})


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class Snapshots:
    """Explicit-host, capped, content-addressed public downloads; no auth headers."""

    def __init__(self, output: Path):
        self.output = output
        self.output.mkdir(parents=True, exist_ok=True)
        self.manifest_path = output / "source_manifest.json"
        previous = json.loads(self.manifest_path.read_text(encoding="utf-8")) if self.manifest_path.exists() else {}
        self.records = previous.get("snapshots", {})
        self.failures = previous.get("failures", [])
        self.total_bytes = 0
        self.client = requests.Session()
        self.client.headers["User-Agent"] = "NutriGenomicsResearch/0.4 public regulatory collector"
        self.client.mount("https://", HTTPAdapter(max_retries=Retry(
            total=1, connect=1, read=0, status_forcelist=[502, 503, 504], allowed_methods={"GET"})))

    def save(self):
        temporary = self.manifest_path.with_suffix(".json.part")
        temporary.write_text(encode({"snapshots": self.records, "failures": self.failures}), encoding="utf-8")
        temporary.replace(self.manifest_path)

    def failure(self, stage: str, error: Exception):
        self.failures.append({"stage": stage, "error_type": type(error).__name__, "reason": str(error)})
        self.save()

    def fetch(self, url: str, slug: str, suffix: str, max_bytes=MAX_BYTES):
        if not 0 < max_bytes <= MAX_BYTES:
            raise ValueError("per-file cap must be at most 100 MB")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in HOSTS:
            raise ValueError("URL outside public source registry")
        old = self.records.get(url)
        if old:
            cached = Path(old["local_path"])
            if cached.is_file() and cached.stat().st_size <= max_bytes:
                raw = cached.read_bytes()
                if hashlib.sha256(raw).hexdigest() != old["sha256"]:
                    raise ValueError("cached source hash mismatch; preserve and investigate")
                return old, raw
        current = url
        response = None
        for _ in range(5):
            response = self.client.get(current, timeout=(10, 40), stream=True, allow_redirects=False)
            if not response.is_redirect:
                break
            target = urljoin(current, response.headers["Location"])
            response.close()
            p = urlsplit(target)
            if p.scheme != "https" or p.hostname not in HOSTS:
                raise ValueError("redirect outside public source registry")
            current = target
        if response is None or response.is_redirect:
            raise ValueError("public source redirect limit exceeded")
        with response:
            response.raise_for_status()
            if int(response.headers.get("Content-Length", "0")) > max_bytes:
                raise ValueError("download exceeds 100 MB file budget")
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > max_bytes or self.total_bytes + size > 200_000_000:
                    raise ValueError("download exceeds file or run byte budget")
                chunks.append(chunk)
            raw = b"".join(chunks)
            if any(x in raw[:20000].lower() for x in (b"recaptcha", b"checking your browser", b"captcha challenge")):
                raise ValueError("browser challenge returned; no bypass")
            if suffix == ".json":
                json.loads(raw)
            if suffix == ".xlsx" and not raw.startswith(b"PK"):
                raise ValueError("expected XLSX")
            if suffix == ".gz" and not raw.startswith(b"\x1f\x8b"):
                raise ValueError("expected gzip")
            content_type = response.headers.get("Content-Type", "")
        digest = hashlib.sha256(raw).hexdigest()
        path = self.output / "raw" / f"{slug}_{digest[:16]}{suffix}"
        path.parent.mkdir(exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".part")
        temporary.write_bytes(raw)
        temporary.replace(path)
        record = {"url": url, "final_url": current, "bytes": size, "sha256": digest,
                  "local_path": str(path.resolve()), "content_type": content_type,
                  "retrieved_at": datetime.now(UTC).isoformat()}
        self.records[url] = record
        self.total_bytes += size
        self.save()
        return record, raw


def release(db, snapshot, source, version, terms, access="public"):
    rid = f"REG:{source}:{version}:{snapshot['sha256'][:16]}"
    insert(db, "ng_release", {"id": rid, "source": source, "version": version,
        "url": snapshot["url"], "retrieved_at": snapshot["retrieved_at"],
        "sha256": snapshot["sha256"], "local_path": snapshot["local_path"],
        "license": terms, "access_status": access})
    return rid


def entity(db, namespace, identifier, kind, metadata=None):
    eid = f"{namespace}:{identifier}"
    insert(db, "ng_entity", {"id": eid, "namespace": namespace, "identifier": identifier,
        "kind": kind, "label": identifier, "organism": "Homo sapiens",
        "metadata_json": encode(metadata or {})})
    return eid


def gene_entity(db, identifier):
    if re.fullmatch(r"ENSG\d{11}(?:\.\d+)?", identifier):
        return entity(db, "Ensembl", identifier, "gene")
    # Native LOC/symbol targets are preserved; no unverified HGNC conversion.
    return entity(db, "SCREEN-native-gene", identifier, "gene")


def verify_snapshot(snapshot, raw):
    if hashlib.sha256(raw).hexdigest() != snapshot["sha256"]:
        raise ValueError("source snapshot bytes do not match declared hash")


def screen_rows(raw):
    expanded_bytes = 0
    with gzip.open(io.BytesIO(raw), "rt", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            expanded_bytes += len(line.encode("utf-8"))
            if expanded_bytes > 200_000_000:
                raise ValueError("SCREEN expanded stream exceeds 200 MB parser budget")
            if not line.strip():
                continue
            fields = line.rstrip("\r\n").split("\t")
            if len(fields) not in {9, 10}:
                raise ValueError(f"undocumented SCREEN row shape at line {line_number}")
            if not re.fullmatch(r"EH38E\d+", fields[0]) or not fields[1].strip():
                raise ValueError(f"invalid native SCREEN identifier at line {line_number}")
            yield line_number, fields


def import_screen(db, snapshot, raw, mode, panel=PANEL, limit=2000):
    verify_snapshot(snapshot, raw)
    if mode not in {"crispr", "graphreg"} or not 0 < limit <= 5000:
        raise ValueError("unknown SCREEN subset or unsupported limit")
    rid = release(db, snapshot, f"SCREEN-{mode}", "V4-hg38-r3.2026.3",
        "Public download; SCREEN/ENCODE derivative redistribution terms not verified. Export disabled.")
    counts = Counter()
    admitted = 0
    seen = set()
    for line_number, fields in screen_rows(raw):
        counts["source_rows"] += 1
        if mode == "graphreg":
            if len(fields) != 9:
                raise ValueError("GraphReg source requires nine fields")
            ccre, target, symbol, biotype, method, interval, accession, cell, score = fields
            if method != "GraphRegLR":
                counts[f"excluded_method:{method}"] += 1
                continue
            if symbol.strip() not in panel or cell != "K562":
                counts["excluded_panel_or_cell"] += 1
                continue
            if admitted >= limit:
                counts["excluded_limit"] += 1
                continue
            relation, tier = "predicted_ccre_gene_link", "curated_mechanism"
            validation = "computational_prediction_not_experimentally_validated"
            assertion = f"SCREEN GraphRegLR predicts a cCRE–gene link between {ccre} and {target} in {cell}; no experimental validation inferred."
            source_numeric = {"model_score_untyped": score}
        else:
            if len(fields) != 10:
                raise ValueError("CRISPR source requires ten fields")
            ccre, target, symbol, biotype, interval, method, accession, cell, numeric_1, numeric_2 = fields
            if not method.startswith("CRISPR"):
                raise ValueError("CRISPR row assay type is inconsistent")
            relation, tier = "experimentally_tested_ccre_gene_pair", "in_vitro"
            validation = "tested_pair_significance_not_classified"
            assertion = f"SCREEN lists a {method} tested cCRE–gene pair {ccre} and {target} in {cell}; the listing alone does not establish a significant regulatory effect."
            source_numeric = {"column_9_untyped": numeric_1, "column_10_untyped": numeric_2}
        for value in source_numeric.values():
            if not math.isfinite(float(value)):
                raise ValueError("nonfinite SCREEN numeric field")
        frozen_fields = tuple(fields)
        if frozen_fields in seen:
            counts["excluded_exact_duplicate_row"] += 1
            continue
        seen.add(frozen_fields)
        ccre_id = entity(db, "ENCODE-cCRE", ccre, "molecular_feature", {"genome_build": "GRCh38/hg38"})
        target_id = gene_entity(db, target)
        row_digest = hashlib.sha256(encode(fields).encode()).hexdigest()[:20]
        context = {"review_status": "source_checked", "export_allowed": False,
            "assertion": assertion, "source_url": SCREEN + "/downloads",
            "validation_status": validation, "method": method, "cell_type": cell,
            "genome_build": "GRCh38/hg38", "source_symbol": symbol,
            "source_target_id": target, "source_biotype": biotype, "source_interval": interval,
            "source_accession": accession, "source_raw_fields": fields,
            "source_numeric_fields": source_numeric, "line_number": line_number,
            "subset": {"gene_symbols": sorted(panel), "cell": "K562", "limit": limit} if mode == "graphreg" else {"all_tested_pairs": True},
            "limitations": ["A cCRE is a candidate regulatory element, not necessarily an enhancer.",
                "No nearest-gene proximity edge is imported as regulation.",
                "Cell-line annotation is not an adult whole-blood dietary intervention.",
                "Numeric source columns without verified definitions are preserved untyped; no p-value, FDR or effect scale is guessed.",
                "No dietary exposure, causal methylation mechanism or clinical effect is asserted."]}
        insert(db, "ng_evidence", {"id": f"REG:SCREEN:{mode}:{snapshot['sha256'][:16]}:{row_digest}",
            "subject_id": ccre_id, "object_id": target_id, "relation": relation,
            "layer": "molecular_target", "release_id": rid,
            "source_ref": f"SCREEN V4 hg38 {mode} source line {line_number}; native fields frozen",
            "tier": tier, "inference": "descriptive" if mode == "crispr" else "association",
            "organism": "Homo sapiens", "tissue": cell, "value_status": "qualitative",
            "context_json": encode(context)})
        admitted += 1
    return {"admitted_rows": admitted, "source_counts": dict(counts), "release_id": rid}


def source_number(value):
    text = str(value).strip()
    if text in {"", "NA", "nan", "None"}:
        return None, "missing"
    match = re.fullmatch(r"([<>]=?)?\s*([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)", text)
    if not match:
        raise ValueError("unsupported numeric qualifier")
    number = float(match[2])
    if not math.isfinite(number):
        raise ValueError("nonfinite source number")
    return number, match[1] or "="


def evo_original_rows(raw):
    # Header and legends are part of the immutable source, not inferred schema.
    frame = pd.read_excel(io.BytesIO(raw), sheet_name="Table_S3a", header=None)
    header_rows = [i for i, row in frame.iterrows() if str(row.iloc[0]).strip() == "CpG" and str(row.iloc[1]).strip() == "Ensembl_ID"]
    if len(header_rows) != 1:
        raise ValueError("original EvoImmunoPop header not unique")
    header = header_rows[0]
    legends = " ".join(str(v) for v in frame.iloc[:header, 0])
    if "spearman's rank correlation" not in legends or "merged dataset" not in legends:
        raise ValueError("Spearman statistic/population definition not verified")
    result = {}
    for index, row in frame.iloc[header + 1:].iterrows():
        vals = [str(v).strip() for v in row.tolist()]
        if not re.fullmatch(r"cg\d{8}", vals[0]):
            raise ValueError("invalid original CpG identifier")
        p, qualifier = source_number(vals[6])
        rho, rho_qualifier = source_number(vals[7])
        if p is None or not 0 <= p <= 1 or rho is None or abs(rho) > 1 or rho_qualifier != "=":
            raise ValueError("invalid original eQTM statistic")
        if not re.fullmatch(r"ENSG\d{11}(?:\.\d+)?", vals[1]):
            raise ValueError("invalid original measured eGene identifier")
        record = {"cpg": vals[0], "gene": vals[1], "symbol": vals[2], "p": p,
            "p_qualifier": qualifier, "rho": rho, "population": vals[8],
            "sheet": "Table_S3a", "excel_row": int(index + 1), "source_raw_fields": vals}
        result.setdefault((vals[0], vals[2]), []).append(record)
    return result


def atlas_rows(raw):
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    expected = {"probeid", "gene", "pvalue", "tissue", "cohort", "beta", "FDR", "num_controls", "num_cases", "eQTM_label"}
    if not reader.fieldnames or not expected <= set(reader.fieldnames):
        raise ValueError("Atlas downloadable schema lacks required fields")
    return list(reader)


def import_atlas_evo(db, snapshot, raw, original_snapshot, original_raw, code_snapshot, paper_snapshot):
    verify_snapshot(snapshot, raw)
    verify_snapshot(original_snapshot, original_raw)
    original = evo_original_rows(original_raw)
    rows = atlas_rows(raw)
    primary_rid = release(db, original_snapshot, "EvoImmunoPop-primary-eQTM", "Husquin-2018-TableS3a",
        "Article CC-BY4.0; data CC0 unless otherwise stated. " + EVO_PAPER)
    rid = release(db, snapshot, "eQTM-Atlas-preprint-EvoImmunoPop", "public-download-2026-10-01",
        "Atlas derivative data redistribution rights not separately verified; MIT applies to repository code only. Export disabled.")
    study_id = "REG:study:EvoImmunoPop-2018"
    insert(db, "ng_study", {"id": study_id, "title": "EvoImmunoPop adult primary-monocyte methylation–expression associations",
        "population": "156 healthy male adults, 78 African and 78 European ancestry; primary monocytes",
        "design": "observational", "participant_group": "EvoImmunoPop-156-adults",
        "independence_status": "verified", "independence_basis": "Original cohort distinct from FHS; Atlas derivative is the same cohort, never a new independent replication.",
        "n_people": 156, "metadata_json": encode({"primary_publication": EVO_PAPER,
            "primary_publication_status": "peer_reviewed", "atlas_publication_status": "preprint",
            "atlas_preprint_doi": "10.64898/2026.06.07.730721", "individual_data_ingested": False,
            "primary_article_sha256": paper_snapshot["sha256"]})})
    insert(db, "ng_dataset", {"id": "REG:dataset:Atlas-EvoImmunoPop", "study_id": study_id,
        "accession": "eQTM-Atlas:EvoImmunoPop:public-summary", "release_id": rid,
        "modality": "published_eQTM_summary", "access_status": "public", "ingest_status": "measurements",
        "metadata_json": encode({"raw_download_rows": len(rows), "source_application": ATLAS_UI,
            "code_sha256": code_snapshot["sha256"], "source_selection": "Atlas cohort download; not all tested CpG–gene hypotheses", "n_people_is_cohort_size": True})})
    counts = Counter()
    seen = set()
    for line, row in enumerate(rows, 2):
        if row["cohort"] != "EvoImmunoPop" or row["tissue"] != "Primary Monocyte" or row["eQTM_label"] != "cis":
            raise ValueError("Atlas cohort/tissue/relation mismatch")
        if row["num_controls"] != "156" or row["num_cases"] != "0":
            raise ValueError("Atlas participant metadata mismatch")
        if not re.fullmatch(r"cg\d{8}", row["probeid"]):
            raise ValueError("invalid Atlas source identifiers")
        if row["gene"].strip() in {"", "NA", "nan"}:
            counts["excluded_missing_gene_identifier"] += 1
            continue
        p, qualifier = source_number(row["pvalue"])
        if p is not None and not 0 <= p <= 1:
            raise ValueError("invalid Atlas eQTM p-value")
        if (row["probeid"], row["gene"]) in seen:
            raise ValueError("duplicate Atlas pair; no repeated independent cohort")
        seen.add((row["probeid"], row["gene"]))
        matches = [r for r in original.get((row["probeid"], row["gene"]), [])
                   if p is None or (qualifier == r["p_qualifier"] and math.isclose(p, r["p"], rel_tol=1e-5, abs_tol=1e-20))]
        verified = len(matches) == 1
        match = matches[0] if verified else None
        cpg_id = entity(db, "Illumina-CpG", row["probeid"], "molecular_feature", {"probe_kind": "CpG"})
        gene_id = gene_entity(db, match["gene"]) if match else entity(db, "eQTM-Atlas-native-symbol", row["gene"], "gene")
        population = match["population"] if match else "unknown"
        n_people = 156 if population == "Both" else 78 if population in {"AFB", "EUB"} else None
        context = {"review_status": "source_checked" if verified else "candidate", "export_allowed": False,
            "assertion": f"EvoImmunoPop observational primary-monocyte cis eQTM association between {row['probeid']} and {row['gene']}; not causal regulation or a food response.",
            "source_url": ATLAS_UI, "source_row": row, "csv_row": line,
            "primary_publication": EVO_PAPER, "primary_publication_status": "peer_reviewed",
            "atlas_publication_status": "preprint", "atlas_preprint_doi": "10.64898/2026.06.07.730721",
            "primary_release_id": primary_rid, "primary_sha256": original_snapshot["sha256"],
            "cross_validation": ("exact unique CpG/symbol match to original Table S3a; missing Atlas p preserved, original statistic recovered" if p is None else "exact CpG/symbol and p-value match to original Table S3a") if verified else "unique primary table match not verified; hidden candidate",
            "primary_table_row": match, "source_population": population,
            "source_p_qualifier": match["p_qualifier"] if match else qualifier,
            "atlas_p_status": "missing" if p is None else "reported", "source_code_sha256": code_snapshot["sha256"],
            "atlas_beta_status": "missing" if row["beta"] == "NA" else "untyped",
            "atlas_fdr_status": "missing" if row["FDR"] == "NA" else "untyped",
            "genome_build": "source coordinates preserved; no liftOver or inferred build",
            "assay_condition": "unstimulated monocytes; original Table S3a, not stimulated response reQTM" if verified else "unknown",
            "limitations": ["Atlas is a preprint aggregator; the original cohort publication is peer reviewed.",
                "Selected published associations are not the full multiple-testing family; no BH-FDR recalculation.",
                "Missing Atlas beta/FDR remain missing. Spearman rho, where verified, is not a methylation-change or expression-effect coefficient.",
                "No nearest-gene annotation is treated as measured regulation.",
                "This is one EvoImmunoPop cohort, not a new cohort in addition to its original publication.",
                "The association is not evidence of a causal dietary, methylation or health effect."]}
        insert(db, "ng_evidence", {"id": f"REG:Atlas:EvoImmunoPop:{snapshot['sha256'][:16]}:{row['probeid']}:{row['gene']}",
            "subject_id": cpg_id, "object_id": gene_id, "relation": "cis_eQTM_association",
            "layer": "molecular_measurement", "study_id": study_id, "release_id": rid,
            "source_ref": f"Atlas EvoImmunoPop CSV row {line}; original Table S3a row {match['excel_row'] if match else 'unverified'}",
            "tier": "human_observational", "inference": "association", "organism": "Homo sapiens",
            "tissue": "primary monocytes", "effect": match["rho"] if match else None,
            "effect_unit": "Spearman rank correlation rho" if match else None,
            "p_value": match["p"] if match and match["p_qualifier"] == "=" else p if not match and qualifier == "=" else None,
            "value_status": "measured" if match else "qualitative", "n_people": n_people,
            "context_json": encode(context)})
        counts["source_checked" if verified else "candidate"] += 1
    return {"raw_rows": len(rows), "admitted": dict(counts), "study_id": study_id, "release_id": rid,
            "primary_source_release_id": primary_rid, "cohorts_added": 1, "FHS_duplicates_added": 0}


def registries(db, snapshots):
    result = {}
    for source, url, suffix in [
        ("SCREEN-about", SCREEN + "/about", ".html"),
        ("SCREEN-downloads", SCREEN + "/downloads", ".html"),
        ("ENCODE-data-policy", "https://www.encodeproject.org/help/data-use-policy/", ".html"),
        ("ENCODE-REST-data-terms", "https://www.encodeproject.org/help/rest-api/", ".html"),
        ("Atlas-repository", ATLAS_REPO, ".json"),
        ("Atlas-preprint-PubMed", "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pubmed&id=42327319&retmode=xml", ".xml"),
    ]:
        try:
            snapshot, _ = snapshots.fetch(url, source.lower(), suffix, 5_000_000)
            release(db, snapshot, source, "registry-2026-10-01", "Source registry only; no measured regulatory links inferred.", "metadata_only")
            result[source] = snapshot
        except (OSError, ValueError, requests.RequestException) as error:
            snapshots.failure(source, error)
            result[source] = {"status": "failed", "reason": str(error)}
    commit_snap, commit_raw = snapshots.fetch(ATLAS_REPO + "/commits/main", "atlas_commit", ".json", 5_000_000)
    commit = json.loads(commit_raw)["sha"]
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("invalid repository commit")
    for path in ("LICENSE", "README.md", "eQTM_catalog_application.R"):
        snapshot, _ = snapshots.fetch(f"https://raw.githubusercontent.com/ads303/eQTM-Atlas/{commit}/{path}", "atlas_" + Path(path).stem, ".txt", 5_000_000)
        release(db, snapshot, "Atlas-repository-" + path, commit, "MIT repository code license; not a bulk cohort-data license.", "metadata_only")
        result[path] = snapshot
    result["commit"] = commit
    result["commit_snapshot"] = commit_snap
    return result


def collect(output=OUTPUT, atlas_download_url=None):
    snapshots = Snapshots(Path(output))
    db = connect(Path(output) / "regulatory_annotations.sqlite3")
    migrate(db)
    results = {}
    try:
        with db:
            results["registries"] = registries(db, snapshots)
        for mode, url in (("crispr", CRISPR_URL), ("graphreg", GRAPHREG_URL)):
            try:
                snapshot, raw = snapshots.fetch(url, "screen_" + mode, ".gz")
                with db:
                    results[mode] = import_screen(db, snapshot, raw, mode)
            except (OSError, ValueError, requests.RequestException) as error:
                snapshots.failure(mode, error)
                results[mode] = {"status": "failed", "reason": str(error)}
        if atlas_download_url:
            try:
                parsed = urlsplit(atlas_download_url)
                if parsed.hostname != "shiny.crc.pitt.edu" or not re.fullmatch(r"/eqtm_browser/session/[a-f0-9]+/download/download_EvoImmunoPop", parsed.path):
                    raise ValueError("use only observed public EvoImmunoPop download link")
                snapshot, raw = snapshots.fetch(atlas_download_url, "atlas_evo", ".csv", 10_000_000)
                original, original_raw = snapshots.fetch(EVO_TABLE, "evo_primary_table_s3", ".xlsx", 5_000_000)
                paper, paper_raw = snapshots.fetch(EVO_PAPER, "evo_primary_article", ".html", 5_000_000)
                if b"creativecommons.org/licenses/by/4.0" not in paper_raw or b"publicdomain/zero/1.0" not in paper_raw:
                    raise ValueError("original publication data license not verified")
                with db:
                    results["atlas_evo"] = import_atlas_evo(db, snapshot, raw, original, original_raw,
                        results["registries"]["eQTM_catalog_application.R"], paper)
            except (OSError, ValueError, requests.RequestException) as error:
                snapshots.failure("atlas_evo", error)
                results["atlas_evo"] = {"status": "failed", "reason": str(error)}
        else:
            results["atlas_evo"] = {"status": "metadata_only", "reason": "No current public UI download URL supplied; no imaginary links added."}
        results["excluded_sources"] = {"SCREEN_ABC": "4.6 GB exceeds 100 MB cap", "SCREEN_rE2G": "4.7 GB exceeds cap",
            "SCREEN_all_experimental": "463.3 MB exceeds cap", "SCREEN_3D": "242.5 MB exceeds cap",
            "SCREEN_eQTL": "220.8 MB exceeds cap", "SCREEN_API": "Requires key; not used",
            "SCREEN_nearest_gene": "Proximity annotation is not regulatory evidence; not collected",
            "Atlas_FHS": "Existing FHS cohort and annotations; excluded from additional cohort count"}
        results["coverage"] = coverage(db)
        results["validation_errors"] = validate(db)
        if results["validation_errors"]:
            raise ValueError("regulatory sidecar failed scientific schema validation")
        results["collection_config"] = {"collector_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "per_file_download_cap_bytes": MAX_BYTES, "run_download_cap_bytes": 200_000_000,
            "screen_expanded_stream_cap_bytes": 200_000_000, "graphreg_panel": sorted(PANEL),
            "graphreg_cell": "K562", "graphreg_limit": 2000, "reference_genome": "GRCh38/hg38",
            "candidate_visibility": "hidden", "dietary_or_health_predictions": False}
        rights = []
        registered_urls = set()
        for row in db.execute("SELECT * FROM ng_release ORDER BY id"):
            registered_urls.add(row["url"])
            rights.append({"release_id": row["id"], "source": row["source"], "version": row["version"],
                "url": row["url"], "sha256": row["sha256"], "local_path": row["local_path"],
                "bytes": Path(row["local_path"]).stat().st_size, "terms": row["license"],
                "access_status": row["access_status"], "annotation_export_allowed": False})
        for url, snapshot in sorted(snapshots.records.items()):
            if url in registered_urls:
                continue
            rights.append({"release_id": None, "source": "supporting-source-snapshot", "url": url,
                "sha256": snapshot["sha256"], "local_path": snapshot["local_path"], "bytes": snapshot["bytes"],
                "terms": "Article CC-BY4.0; article data CC0 unless otherwise stated." if url == EVO_PAPER else "Supporting metadata snapshot; redistribution rights not separately verified.",
                "access_status": "metadata_only", "annotation_export_allowed": False})
        (Path(output) / "source_rights.json").write_text(encode(rights), encoding="utf-8")
        report = Path(output) / "collection_report.json"
        report.write_text(encode(results), encoding="utf-8")
        snapshots.save()
        return results
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--atlas-download-url", help="Public EvoImmunoPop download URL observed in Atlas Downloads UI; session may expire")
    args = parser.parse_args()
    result = collect(args.output, args.atlas_download_url)
    print(encode({key: value for key, value in result.items() if key != "registries"}))


if __name__ == "__main__":
    main()
