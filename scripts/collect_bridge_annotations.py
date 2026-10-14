"""Collect bounded public chemical assays and adult-blood eQTMs into a staging DB.

No controlled data, whole-database downloads, inferred clinical effects, or
nearest-gene regulatory claims. Every admitted row references a frozen snapshot.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from nutrigenomics.expansion import connect, coverage, insert, migrate, validate

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "research" / "2026-10-01" / "annotations"
FHS_URL = "https://www.nature.com/articles/s41598-023-39936-3"
CHEMBL_API = "https://www.ebi.ac.uk/chembl/api/data/"
ALLOWED_HOSTS = {
    "pubchem.ncbi.nlm.nih.gov", "www.ebi.ac.uk", "www.nature.com",
    "media.springernature.com", "idp.nature.com", "www.encodeproject.org", "api.github.com",
    "raw.githubusercontent.com", "chembl.github.io", "chembl.gitbook.io", "www.hmdb.ca",
}
COMPOUNDS = [
    {"cid": 446284, "nutrient": "1278", "label": "EPA (20:5 n-3)", "number": "629",
     "inchikey": "JAZBEHYOTPTENJ-JLNKQSITSA-N"},
    {"cid": 445580, "nutrient": "1272", "label": "DHA (22:6 n-3)", "number": "621",
     "inchikey": "MBMBGCFOFBJSGT-KUBAVDMBSA-N"},
]


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class Snapshots:
    def __init__(self, output: Path, refresh: bool = False):
        self.output = output
        self.refresh = refresh
        self.records: dict[str, dict] = {}
        self.failures: list[dict] = []
        self.total_bytes = 0
        output.mkdir(parents=True, exist_ok=True)
        self.manifest_path = output / "source_manifest.json"
        if self.manifest_path.is_file():
            previous = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            self.records = previous.get("snapshots", {})
            self.failures = previous.get("failures", [])
        self.client = requests.Session()
        self.client.mount("https://", HTTPAdapter(max_retries=Retry(
            total=2, connect=2, read=0, backoff_factor=0.5,
            status_forcelist=[429, 502, 503, 504], allowed_methods={"GET"},
        )))
        self.client.headers["User-Agent"] = "NutriGenomicsResearch/0.3 (public annotation collector)"

    def fetch(self, url: str, slug: str, extension: str = ".json", max_bytes=20_000_000) -> tuple[dict, bytes]:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
            raise ValueError("URL outside the explicit public-source registry")
        existing = self.records.get(url)
        if existing and not self.refresh:
            path = Path(existing["local_path"])
            if path.is_file():
                raw = path.read_bytes()
                if hashlib.sha256(raw).hexdigest() == existing["sha256"]:
                    return existing, raw
        # Redirects are checked one hop at a time before requesting their target.
        current = url
        response = None
        for _ in range(5):
            response = self.client.get(current, timeout=(10, 45), allow_redirects=False, stream=True)
            if response.is_redirect:
                from urllib.parse import urljoin
                target = urljoin(current, response.headers["Location"])
                p = urlsplit(target)
                response.close()
                if p.scheme != "https" or p.hostname not in ALLOWED_HOSTS:
                    raise ValueError("redirect outside public-source registry")
                current = target
                continue
            break
        if response is None or response.is_redirect:
            raise ValueError("redirect limit exceeded")
        with response:
            response.raise_for_status()
            if int(response.headers.get("Content-Length", "0")) > max_bytes:
                raise ValueError("snapshot exceeds byte budget")
            chunks = []
            size = 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > max_bytes or self.total_bytes + size > 200_000_000:
                    raise ValueError("snapshot or run exceeds byte budget")
                chunks.append(chunk)
            raw = b"".join(chunks)
            if any(token in raw[:20000].lower() for token in [b"recaptcha", b"checking your browser", b"captcha challenge"]):
                raise ValueError("public source returned a browser challenge")
            if extension == ".json":
                json.loads(raw)
            if extension == ".xlsx" and not raw.startswith(b"PK"):
                raise ValueError("expected XLSX")
            content_type = response.headers.get("Content-Type", "")
        digest = hashlib.sha256(raw).hexdigest()
        if existing and existing["sha256"] == digest:
            # First retrieval provenance stays immutable when refresh merely
            # verifies identical bytes; idempotent DB releases must not change.
            return existing, raw
        path = self.output / "raw" / f"{slug}_{digest[:16]}{extension}"
        path.parent.mkdir(exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".part")
        temporary.write_bytes(raw)
        temporary.replace(path)
        self.total_bytes += len(raw)
        record = {"url": url, "final_url": current, "sha256": digest,
                  "local_path": str(path), "bytes": len(raw), "content_type": content_type,
                  "retrieved_at": datetime.now(UTC).isoformat()}
        self.records[url] = record
        self.save()
        return record, raw

    def save(self):
        temporary = self.manifest_path.with_suffix(".json.part")
        temporary.write_text(encode({"snapshots": self.records, "failures": self.failures}), encoding="utf-8")
        temporary.replace(self.manifest_path)

    def failure(self, stage: str, error: Exception):
        self.failures.append({"stage": stage, "error_type": type(error).__name__, "reason": str(error)})
        self.save()


def release(db, snapshot: dict, source: str, version: str, license_text: str,
            access_status: str = "public") -> str:
    identifier = f"ANN:{source}:{version}:{snapshot['sha256'][:16]}"
    insert(db, "ng_release", {"id": identifier, "source": source, "version": version,
        "url": snapshot["url"], "retrieved_at": snapshot["retrieved_at"],
        "sha256": snapshot["sha256"], "local_path": snapshot["local_path"],
        "license": license_text, "access_status": access_status})
    return identifier


def entity(db, identifier, kind, label, namespace, source_identifier, metadata=None):
    insert(db, "ng_entity", {"id": identifier, "kind": kind, "label": str(label),
        "namespace": namespace, "identifier": str(source_identifier),
        "organism": "Homo sapiens" if kind in {"gene", "protein", "molecular_feature"} else None,
        "metadata_json": encode(metadata or {})})


def chemical_identities(db, snapshots: Snapshots) -> list[dict]:
    url = "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/446284,445580/property/InChIKey,Title/JSON"
    snapshot, raw = snapshots.fetch(url, "pubchem_epa_dha")
    records = {row["CID"]: row for row in json.loads(raw)["PropertyTable"]["Properties"]}
    rid = release(db, snapshot, "PubChem", "PUG-REST-2026-10-01",
                  "Public chemical identifier facts; cite PubChem. https://pubchem.ncbi.nlm.nih.gov/docs/data-usage-policies")
    result = []
    for expected in COMPOUNDS:
        row = records[expected["cid"]]
        if row["InChIKey"] != expected["inchikey"]:
            raise ValueError("full stereochemical InChIKey identity mismatch")
        compound_id = f"PubChem:{row['CID']}"
        nutrient_id = f"FDC-nutrient:{expected['nutrient']}"
        entity(db, compound_id, "compound", row["Title"], "PubChem", row["CID"], row)
        entity(db, nutrient_id, "nutrient", expected["label"], "USDA-FDC-nutrient", expected["nutrient"],
               {"number": expected["number"], "unit": "g"})
        insert(db, "ng_mapping", {"id": f"ANN:related-identity:{expected['nutrient']}:{rid}",
            "from_id": nutrient_id, "to_id": compound_id, "relation": "related", "release_id": rid,
            "source_ref": f"CID={row['CID']};full InChIKey={row['InChIKey']}",
            "method": "EPA/DHA analytical nutrient related reference; FDC definition does not verify full E/Z stereochemistry",
            "review_status": "verified"})
        insert(db, "ng_evidence", {"id": f"ANN:related-identity:{row['CID']}:{rid}",
            "subject_id": nutrient_id, "object_id": compound_id, "relation": "related_chemical_reference",
            "layer": "molecular_identity", "release_id": rid, "source_ref": f"CID {row['CID']}",
            "tier": "identity", "inference": "descriptive", "value_status": "qualitative",
            "context_json": encode({"review_status": "source_checked", "export_allowed": True,
                "inchikey": row["InChIKey"], "identity_relation": "related",
                "assertion": f"{expected['label']} has related chemical reference PubChem CID {row['CID']}; exact nutrient stereochemistry is unverified.",
                "limitations": ["The PubChem compound's full InChIKey is verified; the analytical nutrient does not specify all E/Z positions.",
                    "Related identity does not establish compound exposure, bioavailability, or a health effect."]})})
        result.append({**expected, **row, "compound_id": compound_id})
    return result


def activity_rejection(activity: dict, assay: dict, target: dict) -> list[str]:
    reasons = []
    if activity.get("assay_type") not in {"B", "F"}:
        reasons.append("unsupported_assay_type")
    if assay.get("confidence_score") != 9:
        reasons.append("target_assignment_not_direct_single_protein")
    if target.get("target_type") != "SINGLE PROTEIN":
        reasons.append("not_single_protein")
    if str(target.get("tax_id")) != "9606" or target.get("organism") != "Homo sapiens":
        reasons.append("not_human_target")
    if activity.get("target_chembl_id") != assay.get("target_chembl_id"):
        reasons.append("assay_target_mismatch")
    if activity.get("standard_units") != "nM" or activity.get("standard_flag") != 1:
        reasons.append("not_standardized_nanomolar_activity")
    if activity.get("data_validity_comment") not in {None, "", "Manually validated"}:
        reasons.append("source_validity_flag")
    try:
        if not math.isfinite(float(activity.get("standard_value"))) or float(activity["standard_value"]) < 0:
            reasons.append("invalid_activity_value")
    except (TypeError, ValueError):
        reasons.append("missing_activity_value")
    if activity.get("standard_relation") not in {"=", "<", ">", "<=", ">="}:
        reasons.append("unsupported_relation")
    return reasons


def chembl_batch(snapshots: Snapshots, kind: str, identifiers: set[str]) -> tuple[dict, dict]:
    if not identifiers:
        return {}, {}
    if len(identifiers) > 200:
        raise ValueError("ChEMBL metadata batch exceeds bounded subset")
    url = CHEMBL_API + kind + ".json?" + urlencode({
        f"{kind}_chembl_id__in": ",".join(sorted(identifiers)), "limit": 200})
    digest = hashlib.sha256(url.encode()).hexdigest()[:12]
    try:
        snapshot, raw = snapshots.fetch(url, f"chembl_{kind}_batch_{digest}")
    except (requests.RequestException, OSError, ValueError) as error:
        snapshots.failure(f"ChEMBL {kind} batch", error)
        # Previously fetched and hash-verified individual public records remain
        # usable. Unavailable IDs are explicitly excluded, never reconstructed.
        records, sources = {}, {}
        for identifier in identifiers:
            individual_url = CHEMBL_API + f"{kind}/{identifier}.json"
            if individual_url not in snapshots.records:
                continue
            source, individual_raw = snapshots.fetch(individual_url, identifier)
            record = json.loads(individual_raw)
            if record.get(f"{kind}_chembl_id") == identifier:
                records[identifier] = record
                sources[identifier] = source
        return records, sources
    document = json.loads(raw)
    if document.get("page_meta", {}).get("next"):
        raise ValueError("unexpected paginated ChEMBL metadata batch")
    records = {row[f"{kind}_chembl_id"]: row for row in document.get(kind + "s", [])}
    return records, dict.fromkeys(records, snapshot)


def chembl_assays(db, snapshots: Snapshots, compounds: list[dict]) -> dict:
    status_snapshot, raw = snapshots.fetch(CHEMBL_API + "status.json", "chembl_status")
    status = json.loads(raw)
    version = str(status.get("chembl_db_version", status.get("chembl_version", "status-snapshot")))
    license_text = "CC BY-SA 3.0; attribution and share-alike apply to exported ChEMBL records. https://chembl.github.io/chembl-licensing/"
    terms_verified = False
    try:
        _, terms_raw = snapshots.fetch(
            "https://chembl.gitbook.io/chembl-interface-documentation/frequently-asked-questions/general-questions",
            "chembl_terms", ".html")
        terms_verified = b"ShareAlike" in terms_raw or b"BY-SA" in terms_raw or b"by-sa" in terms_raw
    except (requests.RequestException, OSError, ValueError) as error:
        snapshots.failure("ChEMBL license snapshot", error)
    release(db, status_snapshot, "ChEMBL-status", version, license_text, "metadata_only")
    exclusions, admitted = [], 0
    for compound in compounds:
        lookup = CHEMBL_API + "molecule.json?" + urlencode({
            "molecule_structures__standard_inchi_key": compound["InChIKey"], "limit": 10})
        _, raw = snapshots.fetch(lookup, f"chembl_molecule_{compound['cid']}")
        molecules = json.loads(raw).get("molecules", [])
        exact = [m for m in molecules if (m.get("molecule_structures") or {}).get("standard_inchi_key") == compound["InChIKey"]]
        if len(exact) != 1:
            raise ValueError("ChEMBL structure identity is absent or ambiguous")
        molecule = exact[0]["molecule_chembl_id"]
        url = CHEMBL_API + "activity.json?" + urlencode({"molecule_chembl_id": molecule,
            "standard_units": "nM", "assay_type__in": "B,F", "limit": 200})
        activity_snapshot, raw = snapshots.fetch(url, f"chembl_activity_{molecule}")
        document = json.loads(raw)
        rid = release(db, activity_snapshot, "ChEMBL-activities", version, license_text)
        if document.get("page_meta", {}).get("next"):
            exclusions.append({"molecule": molecule, "reason": "bounded_subset_only", "page_meta": document["page_meta"]})
        human_activities = [activity for activity in document.get("activities", [])
                            if str(activity.get("target_tax_id")) == "9606"]
        assays, assay_snapshot = chembl_batch(snapshots, "assay",
            {a["assay_chembl_id"] for a in human_activities})
        targets, target_snapshot = chembl_batch(snapshots, "target",
            {a["target_chembl_id"] for a in human_activities})
        for activity in document.get("activities", []):
            # Exclude nonhuman assignments before downloading additional assay metadata.
            if str(activity.get("target_tax_id")) != "9606":
                exclusions.append({"activity_id": activity["activity_id"], "reasons": ["not_human_target"]})
                continue
            assay = assays.get(activity["assay_chembl_id"])
            target = targets.get(activity["target_chembl_id"])
            if assay is None or target is None:
                exclusions.append({"activity_id": activity["activity_id"], "reasons": ["assay_or_target_metadata_unavailable"]})
                continue
            reasons = activity_rejection(activity, assay, target)
            if reasons:
                exclusions.append({"activity_id": activity["activity_id"], "reasons": reasons})
                continue
            target_id = f"ChEMBL-target:{activity['target_chembl_id']}"
            entity(db, target_id, "protein", target["pref_name"], "ChEMBL-target", activity["target_chembl_id"], target)
            relation = "binding_assay" if activity["assay_type"] == "B" else "functional_assay"
            exact_value = activity["standard_relation"] == "="
            value = float(activity["standard_value"])
            context = {"review_status": "source_checked", "export_allowed": terms_verified,
                "export_terms_snapshot_verified": terms_verified,
                "assertion": f"{compound['label']}: {relation} against human {target['pref_name']}; {activity['standard_type']} {activity['standard_relation']} {value:g} nM.",
                "activity": activity, "assay": assay, "target": target,
                "assay_snapshot": assay_snapshot[activity["assay_chembl_id"]],
                "target_snapshot": target_snapshot[activity["target_chembl_id"]],
                "confidence_score_meaning": "9 denotes direct single-protein target assignment, not a probability of biological or clinical truth.",
                "limitations": ["In vitro assigned human protein assay; not a human oral intervention.",
                    "Functional assays are not direct-binding evidence.", "Nanomolar assay activity cannot be inferred from ingested milligrams.",
                    "Inequality-qualified values remain bounds, not exact measurements."]}
            insert(db, "ng_evidence", {"id": f"ANN:ChEMBL:{activity['activity_id']}:{activity_snapshot['sha256'][:16]}",
                "subject_id": compound["compound_id"], "object_id": target_id, "relation": relation,
                "layer": "molecular_target", "release_id": rid, "source_ref": f"activity_id={activity['activity_id']};assay={activity['assay_chembl_id']}",
                "tier": "in_vitro", "inference": "descriptive", "organism": "Homo sapiens",
                "tissue": assay.get("assay_tissue"), "effect": value if exact_value else None,
                "effect_unit": f"{activity['standard_type']} nM" if exact_value else None,
                "value_status": "measured" if exact_value else "qualitative", "context_json": encode(context)})
            admitted += 1
    return {"admitted": admitted, "exclusions": exclusions, "version": version,
            "export_terms_snapshot_verified": terms_verified}


def fhs_row(row: dict, kind: str) -> dict:
    """Validate the actual eGene pair; never substitute the nearest-gene column."""
    cpg, gene = str(row.get("CpG", "")), str(row.get("eGene", ""))
    if not re.fullmatch(r"(?:cg\d{8}|ch\.(?:\d+|X|Y)\.\d+[FR])", cpg) or not re.fullmatch(r"ENSG\d+(?:\.\d+)?", gene):
        raise ValueError("invalid Illumina methylation probe or measured eGene identifier")
    estimate, p = float(row["Estimate"]), float(row["p-value"])
    if not math.isfinite(estimate) or not math.isfinite(p) or not 0 <= p <= 1:
        raise ValueError("invalid estimate or p value")
    clean = {str(key): (None if pd.isna(value) else value.item() if hasattr(value, "item") else value)
             for key, value in row.items()}
    return {"cpg": cpg, "gene": gene, "estimate": estimate, "p": p, "kind": kind,
            "probe_kind": "CpG" if cpg.startswith("cg") else "non_CpG_CH", "raw": clean}


def fhs_eqtms(db, snapshots: Snapshots) -> dict:
    article_snapshot, raw = snapshots.fetch(FHS_URL, "fhs_article", ".html")
    article = raw.decode("utf-8")
    links = re.findall(r'https://media\.springernature\.com/[^"<>\s]+MOESM1_ESM\.xlsx', article)
    if not links:
        raise ValueError("source article did not expose its supplementary workbook")
    url = links[0].replace("&amp;", "&")
    snapshot, _ = snapshots.fetch(url, "fhs_supplement", ".xlsx")
    rid = release(db, snapshot, "FHS-eQTM", "2023-published-supplement",
        "CC BY 4.0: Keshawarz et al., Scientific Reports 13,12952 (2023); DOI 10.1038/s41598-023-39936-3. https://creativecommons.org/licenses/by/4.0/")
    study_id = "ANN:FHS-eQTM-2023"
    insert(db, "ng_study", {"id": study_id,
        "title": "Framingham Heart Study adult whole-blood eQTM published summaries",
        "population": "2115 FHS adults with whole-blood DNA methylation and RNA-seq",
        "design": "observational", "participant_group": "FHS-eQTM-2023",
        "independence_status": "verified", "independence_basis": "Published FHS cohort; repeat imports and atlas annotations retain this same cohort identity.",
        "n_people": 2115, "pubmed_id": "37563237",
        "metadata_json": encode({"doi": "10.1038/s41598-023-39936-3", "article_snapshot": article_snapshot,
            "not_a_diet_intervention": True, "individual_raw_data": "dbGaP controlled; not requested"})})
    insert(db, "ng_dataset", {"id": "ANN:FHS-eQTM-ST1-ST2", "study_id": study_id,
        "accession": "FHS-eQTM-2023-ST1-ST2", "release_id": rid, "modality": "published_eQTM_summary",
        "access_status": "public", "ingest_status": "measurements", "n_assays": None,
        "metadata_json": encode({"selection": "Published top 10000 cis and top 10000 trans pairs, not all tested pairs",
            "ST3_ST4": "Frozen in workbook but not ingested; cohort-specific replication not conflated with FHS discovery",
            "no_nearest_gene_inference": True})})
    workbook = pd.ExcelFile(snapshot["local_path"])
    counts, excluded, seen = Counter(), [], set()
    covariates = ["age", "sex", "white blood cell count", "blood cell fractions", "platelet count",
                  "5 gene expression PCs", "10 DNA methylation PCs"]
    for sheet, kind in [("ST1", "cis"), ("ST2", "trans")]:
        frame = pd.read_excel(workbook, sheet_name=sheet, header=1)
        if not {"CpG", "eGene", "Estimate", "p-value"} <= set(frame.columns):
            raise ValueError("FHS supplemental header changed")
        for index, source_row in enumerate(frame.to_dict("records"), start=3):
            try:
                row = fhs_row(source_row, kind)
            except (ValueError, TypeError) as error:
                excluded.append({"sheet": sheet, "row": index, "reason": str(error)})
                continue
            pair = (kind, row["cpg"], row["gene"])
            if pair in seen:
                excluded.append({"sheet": sheet, "row": index, "reason": "duplicate pair"})
                continue
            seen.add(pair)
            probe_namespace = "Illumina-CpG" if row["probe_kind"] == "CpG" else "Illumina-CH-probe"
            cpg_id, gene_id = f"{probe_namespace}:{row['cpg']}", f"Ensembl:{row['gene']}"
            entity(db, cpg_id, "molecular_feature", row["cpg"], probe_namespace, row["cpg"],
                   {"probe_kind": row["probe_kind"]})
            # Full Ensembl annotation version is retained; no silent gene-symbol collapse.
            entity(db, gene_id, "gene", row["gene"], "Ensembl", row["gene"], {"source_annotation": "GENCODE 30"})
            context = {"review_status": "source_checked", "export_allowed": True,
                "assertion": f"Adult whole-blood FHS {kind} methylation-expression association: {row['cpg']} ({row['probe_kind']}) and {row['gene']}; not causal regulation.",
                "probe_kind": row["probe_kind"],
                "source_url": FHS_URL, "source_row": row["raw"], "sheet": sheet, "excel_row": index,
                "source_effect_scale": "Residualized log2(TMM-normalized expression+1) per residualized methylation beta unit",
                "genome_build": "not verified from supplemental coordinates; retain native coordinates without liftOver",
                "published_selection": "Top 10000 reported pairs in this relation class; unreported pairs are unknown, not zero",
                "cis_distance_definition": "within 1 Mb" if kind == "cis" else "more than 1 Mb",
                "limitations": ["Observational eQTM association, not evidence that methylation causally regulates this gene.",
                    "No food exposure or dietary intervention was tested by this association model.",
                    "DNA methylation gene is source proximity annotation; the eGene column is the measured association endpoint.",
                    "Source p-values preserved; no BH-FDR recalculated from the selected subset."]}
            insert(db, "ng_evidence", {"id": f"ANN:FHS:{kind}:{row['cpg']}:{row['gene']}:{snapshot['sha256'][:16]}",
                "subject_id": cpg_id, "object_id": gene_id, "relation": f"{kind}_eQTM_association",
                "layer": "molecular_measurement", "study_id": study_id, "release_id": rid,
                "source_ref": f"Supplementary {sheet}, row {index}", "tier": "human_observational",
                "inference": "association", "organism": "Homo sapiens", "tissue": "whole blood",
                "effect": row["estimate"], "effect_unit": "source-model expression coefficient per methylation beta unit",
                "p_value": row["p"], "value_status": "measured", "n_people": 2115,
                "covariates_json": encode(covariates), "context_json": encode(context)})
            counts[kind] += 1
    return {"admitted": dict(counts), "exclusions": excluded, "individual_data_ingested": False}


def metadata_registries(db, snapshots: Snapshots) -> dict:
    sources = [
        ("ENCODE-SCREEN", "https://www.encodeproject.org/software/screen/?format=json", ".json",
         "Source registry only; released ENCODE data policy https://www.encodeproject.org/help/getting-started/"),
        ("eQTM-Atlas-preprint", "https://api.github.com/repos/ads303/eQTM-Atlas", ".json",
         "Repository metadata only; 2026 preprint, bulk association terms not verified"),
        ("HMDB", "https://www.hmdb.ca/about", ".html", "Registry metadata only; HMDB data-use terms must be reviewed before bulk import"),
    ]
    results = {}
    for source, url, extension, terms in sources:
        try:
            snapshot, _ = snapshots.fetch(url, source.lower(), extension)
            release(db, snapshot, source, "registry-2026-10-01", terms, "metadata_only")
            results[source] = {"status": "metadata_only", "measured_links": 0, "url": url}
        except (requests.RequestException, OSError, ValueError) as error:
            snapshots.failure(source, error)
            results[source] = {"status": "failed", "reason": str(error)}
    return results


def collect(output: Path = OUTPUT, refresh: bool = False) -> dict:
    snapshots = Snapshots(output, refresh)
    db = connect(output / "annotations.sqlite3")
    migrate(db)
    results = {}
    try:
        with db:
            compounds = chemical_identities(db, snapshots)
        results["identity"] = {"admitted": len(compounds), "full_inchikey_verified": True}
        for stage, operation in [("chembl", lambda: chembl_assays(db, snapshots, compounds)),
                                 ("fhs_eqtm", lambda: fhs_eqtms(db, snapshots)),
                                 ("registries", lambda: metadata_registries(db, snapshots))]:
            try:
                with db:
                    results[stage] = operation()
            except (requests.RequestException, OSError, ValueError, KeyError, sqlite3.DatabaseError) as error:
                snapshots.failure(stage, error)
                results[stage] = {"status": "failed", "reason": str(error)}
        errors = validate(db)
        results["coverage"] = coverage(db)
        results["validation_errors"] = errors
        results["clinical_predictions"] = False
        results["finished_at"] = datetime.now(UTC).isoformat()
        (output / "collection_report.json").write_text(encode(results), encoding="utf-8")
        if errors:
            raise ValueError("staging evidence validation failed")
    finally:
        db.close()
        snapshots.save()
        snapshots.client.close()
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    result = collect(args.output.resolve(), args.refresh)
    print(json.dumps({key: value if key not in {"chembl", "fhs_eqtm"} else
        {k: v for k, v in value.items() if k != "exclusions"} for key, value in result.items()}, indent=2))


if __name__ == "__main__":
    main()
