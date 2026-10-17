"""Build a read-only Explorer release without modifying any source database.

The release is a presentation index, not a merged clinical cohort or an efficacy
model. Source IDs and original assertion payloads remain independently auditable.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE node(id TEXT PRIMARY KEY,kind TEXT NOT NULL,label TEXT NOT NULL,
 label_en TEXT,aliases_json TEXT NOT NULL,metadata_json TEXT NOT NULL,
 domains_json TEXT NOT NULL,degree INTEGER NOT NULL DEFAULT 0);
CREATE TABLE source(id TEXT PRIMARY KEY,metadata_json TEXT NOT NULL);
CREATE TABLE edge(id TEXT PRIMARY KEY,subject TEXT NOT NULL REFERENCES node(id),
 object TEXT NOT NULL REFERENCES node(id),relation TEXT NOT NULL,tier TEXT NOT NULL,
 review_status TEXT NOT NULL,taxon TEXT,context_json TEXT NOT NULL,
 source_ids_json TEXT NOT NULL,pmids_json TEXT NOT NULL,domains_json TEXT NOT NULL);
CREATE TABLE measurement(id TEXT PRIMARY KEY,food TEXT NOT NULL REFERENCES node(id),
 component TEXT NOT NULL REFERENCES node(id),amount REAL,unit TEXT,basis TEXT,
 status TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES source(id),raw_json TEXT NOT NULL);
CREATE TABLE node_domain(node_id TEXT NOT NULL REFERENCES node(id),domain TEXT NOT NULL,
 PRIMARY KEY(node_id,domain));
CREATE TABLE edge_domain(edge_id TEXT NOT NULL REFERENCES edge(id),domain TEXT NOT NULL,
 PRIMARY KEY(edge_id,domain));
CREATE TABLE meta(key TEXT PRIMARY KEY,value_json TEXT NOT NULL);
CREATE INDEX edge_subject_idx ON edge(subject);
CREATE INDEX edge_object_idx ON edge(object);
CREATE INDEX edge_relation_idx ON edge(relation,tier,review_status);
CREATE INDEX node_kind_label_idx ON node(kind,label);
CREATE INDEX node_label_idx ON node(label);
CREATE INDEX node_domain_lookup_idx ON node_domain(domain,node_id);
CREATE INDEX edge_domain_lookup_idx ON edge_domain(domain,edge_id);
CREATE INDEX measurement_food_idx ON measurement(food);
CREATE INDEX measurement_component_idx ON measurement(component);
"""


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def verify_closed_sqlite(path: Path) -> None:
    """Read committed pages through a fresh connection, outside the writer cache."""
    connection = read_only(path)
    try:
        cache_mib = int(os.environ.get('NUTRIGENOMICS_INTEGRITY_CACHE_MIB', '128'))
        if not 64 <= cache_mib <= 4096:
            raise ValueError('Integrity read-cache budget must be between 64 and 4096 MiB')
        connection.execute('PRAGMA cache_size=-' + str(cache_mib * 1024))
        integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchmany(20)
        if integrity != ["ok"] or foreign_keys:
            raise ValueError("Reopened SQLite integrity failed: " + canonical({
                "integrity": integrity[:20], "foreign_keys": [list(row) for row in foreign_keys]}))
    finally:
        connection.close()


def decode(value: str | None, default: Any = None) -> Any:
    return json.loads(value) if value else ({} if default is None else default)


def tables(connection: sqlite3.Connection) -> set[str]:
    return {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def normalize_name(label: str) -> str:
    # No synonym substitution, stemming, salt removal, or Greek-letter conversion.
    return " ".join(unicodedata.normalize("NFKC", label).casefold().split())


def mesh(identifier: str) -> str:
    return identifier if identifier.startswith("MESH:") else "MESH:" + identifier


def gene(identifier: str) -> str:
    return identifier if identifier.startswith("NCBIGene:") else "NCBIGene:" + identifier


class ReleaseWriter:
    def __init__(self, connection: sqlite3.Connection):
        self.db = connection
        self.domains: dict[str, set[str]] = defaultdict(set)

    def node(self, identifier: str, kind: str, label: str, metadata: dict | None = None,
             aliases: Iterable[str] = (), label_en: str | None = None,
             domains: Iterable[str] = ()) -> None:
        old = self.db.execute("SELECT * FROM node WHERE id=?", (identifier,)).fetchone()
        if old and old["kind"] != kind:
            raise ValueError("node kind conflict: " + identifier)
        meta = decode(old["metadata_json"]) if old else {}
        meta.update(metadata or {})
        names = set(decode(old["aliases_json"], [])) if old else set()
        names.update(a for a in aliases if a)
        if kind == "gene" and meta.get("gene_identity_verified") is True and meta.get("hgnc_symbol"):
            if label != meta["hgnc_symbol"]:
                names.add(label)
            label = meta["hgnc_symbol"]
        self.domains[identifier].update(domains)
        self.db.execute("""INSERT INTO node VALUES (?,?,?,?,?,?,?,0)
          ON CONFLICT(id) DO UPDATE SET label=excluded.label,label_en=excluded.label_en,
          aliases_json=excluded.aliases_json,metadata_json=excluded.metadata_json""",
                        (identifier, kind, label, label_en or (old["label_en"] if old else label),
                         canonical(sorted(names)), canonical(meta), "[]"))

    def source(self, identifier: str, metadata: dict) -> None:
        previous = self.db.execute("SELECT metadata_json FROM source WHERE id=?", (identifier,)).fetchone()
        if previous and decode(previous[0]).get("sha256") != metadata.get("sha256"):
            raise ValueError("source hash identity conflict: " + identifier)
        self.db.execute("INSERT OR IGNORE INTO source VALUES (?,?)", (identifier, canonical(metadata)))

    def edge(self, subject: str, target: str, relation: str, tier: str, context: dict,
             source_ids: Iterable[str], pmids: Iterable[str] = (), taxon: str | None = None,
             review_status: str = "source_recorded", domains: Iterable[str] = (),
             identifier: str | None = None) -> str:
        source_ids = sorted(set(source_ids))
        if not source_ids:
            raise ValueError("edge without provenance")
        if relation in {"direct_binding", "dti_positive"} and context.get("source") == "CTD":
            raise ValueError("CTD influence cannot become a binding label")
        pmids = sorted(set(str(p) for p in pmids if p))
        if any(not p.isdigit() for p in pmids):
            raise ValueError("invalid PMID")
        key = canonical([subject, target, relation, tier, taxon, context])
        identifier = identifier or hashlib.sha256(key.encode("utf-8")).hexdigest()
        old = self.db.execute("SELECT source_ids_json,pmids_json,domains_json FROM edge WHERE id=?",
                              (identifier,)).fetchone()
        if old:
            source_ids = sorted(set(source_ids) | set(decode(old[0], [])))
            pmids = sorted(set(pmids) | set(decode(old[1], [])))
            domains = set(domains) | set(decode(old[2], []))
        self.db.execute("""INSERT INTO edge VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET source_ids_json=excluded.source_ids_json,
           pmids_json=excluded.pmids_json,domains_json=excluded.domains_json,
           context_json=excluded.context_json""",
                        (identifier, subject, target, relation, tier, review_status, taxon,
                         canonical(context), canonical(source_ids), canonical(pmids), canonical(sorted(domains))))
        self.domains[subject].update(domains)
        self.domains[target].update(domains)
        return identifier


def import_graph(writer: ReleaseWriter, graph_path: Path) -> None:
    # SQL aggregation avoids 400k Python/SQLite lookups during the source copy.
    db = writer.db
    db.execute("ATTACH DATABASE ? AS original_graph", (graph_path.resolve().as_uri() + "?mode=ro",))
    db.execute("INSERT INTO source SELECT * FROM original_graph.source")
    db.execute("""INSERT INTO node SELECT id,kind,name,name,
        CASE WHEN json_type(metadata_json,'$.identifier_aliases')='array'
             THEN json_extract(metadata_json,'$.identifier_aliases') ELSE '[]' END,
        metadata_json,'[]',0 FROM original_graph.node""")
    db.execute("""INSERT INTO edge
      WITH sources AS (SELECT edge_id,json_group_array(source_id) ids FROM original_graph.edge_source GROUP BY edge_id),
      citations AS (SELECT edge_id,json_group_array(pmid) ids FROM original_graph.citation GROUP BY edge_id)
      SELECT e.id,e.subject,e.object,e.relation,e.tier,
       CASE WHEN e.tier IN ('extracted','predicted','inferred') THEN 'candidate' ELSE 'source_recorded' END,
       e.taxon,e.context_json,coalesce(s.ids,'[]'),coalesce(c.ids,'[]'),'[]'
       FROM original_graph.edge e LEFT JOIN sources s ON s.edge_id=e.id LEFT JOIN citations c ON c.edge_id=e.id""")
    db.execute("INSERT INTO measurement SELECT id,food,nutrient,amount,unit,basis,status,source_id,raw_json FROM original_graph.measurement")
    # Preserve source mappings without declaring chemical identity.
    if db.execute("SELECT 1 FROM original_graph.sqlite_master WHERE name='identifier_mapping'").fetchone():
        mappings: dict[str, list[dict]] = defaultdict(list)
        for row in db.execute("SELECT * FROM original_graph.identifier_mapping"):
            mappings[row["subject"]].append(dict(row))
        for identifier, rows in mappings.items():
            row = db.execute("SELECT metadata_json FROM node WHERE id=?", (identifier,)).fetchone()
            metadata = decode(row[0]); metadata["source_identifier_mappings"] = rows
            db.execute("UPDATE node SET metadata_json=? WHERE id=?", (canonical(metadata), identifier))
    db.commit()
    db.execute("DETACH DATABASE original_graph")


def import_core(writer: ReleaseWriter, core: Path, input_hash: str) -> dict:
    counts = {"exact_food_mapping": 0, "ambiguous_food_mapping": 0, "health_assertions": 0,
              "exposures": 0, "intervention_component_edges": 0}
    with read_only(core) as source:
        present = tables(source)
        core_foods: dict[str, list[sqlite3.Row]] = defaultdict(list)
        if "food" in present:
            for row in source.execute("SELECT * FROM food"):
                core_foods[row["source_code"]].append(row)
        # Match full namespaced source code, never display names.
        for row in list(writer.db.execute("SELECT * FROM node WHERE kind='food'")):
            metadata = decode(row["metadata_json"])
            matches = core_foods.get(row["id"], [])
            if not matches and row["id"].startswith("USDA-FDC:"):
                source_code = "FDC:" + row["id"].split(":", 1)[1]
                candidates = core_foods.get(source_code, [])
                matches = [food for food in candidates if food["source_id"] == "R03"
                           and str(decode(food["metadata_json"]).get("fdc_id")) == row["id"].split(":", 1)[1]
                           and metadata.get("original_source_code") == source_code]
            if len(matches) > 1:
                counts["ambiguous_food_mapping"] += 1
                continue
            if len(matches) != 1:
                continue
            food = matches[0]
            core_meta = decode(food["metadata_json"])
            if row["id"].startswith("K-FIND:"):
                # Full code includes dataset+food code+source-name hash; also audit
                # the human-readable metadata so a corrupted crosswalk abstains.
                dataset = row["id"].split(":")[1]
                if (str(core_meta.get("kfind_dataset_id")) != dataset or
                    core_meta.get("provider_food_code") != metadata.get("food_code") or
                    core_meta.get("provider_food_name") != metadata.get("source_name")):
                    counts["ambiguous_food_mapping"] += 1
                    continue
            metadata.update({"core_food_id": food["id"], "core_food_mapping_basis": "exact_namespaced_source_code",
                             "core_source_id": food["source_id"], "core_active_release": food["active_release"],
                             "edible_state": food["edible_state"], "preparation_state": food["preparation_state"],
                             "composition_mode": food["composition_mode"], "core_metadata": core_meta})
            writer.node(row["id"], "food", food["name"], metadata,
                        aliases=decode(food["aliases"], []), label_en=food["name_en"])
            counts["exact_food_mapping"] += 1
        component_map = {}
        if "component" in present:
            for row in source.execute("SELECT * FROM component"):
                identifier = row["component_key"].replace("FDC-NUTRIENT:", "FDCNutrient:")
                node = writer.db.execute("SELECT * FROM node WHERE id=?", (identifier,)).fetchone()
                if not node:
                    continue
                component_map[row["component_key"]] = identifier
                writer.node(identifier, node["kind"], row["name"],
                            {"core_component_id": row["id"], "component_key": row["component_key"],
                             "category": row["category"], "canonical_unit": row["canonical_unit"],
                             "mixture_or_nutrient_group": row["category"] != "compound",
                             "single_molecule_identity_verified": False},
                            aliases=decode(row["aliases"], []), label_en=row["name_en"])
        if "ng_bridge_source" in present:
            for row in source.execute("SELECT * FROM ng_bridge_source"):
                record = dict(row); record["metadata"] = decode(record.pop("metadata_json"))
                record["export_allowed"] = bool(record.get("export_allowed", False))
                writer.source(record["id"], record)
        if "ng_exposure" in present:
            contrasts: dict[str, list[dict]] = defaultdict(list)
            assertions: dict[str, list[dict]] = defaultdict(list)
            if "ng_contrast" in present:
                for row in source.execute("SELECT * FROM ng_contrast"):
                    contrast = dict(row)
                    for exposure in decode(row["exposure_ids"], []):
                        contrasts[exposure].append(contrast)
            if "ng_assertion" in present:
                for row in source.execute("SELECT * FROM ng_assertion"):
                    assertions[row["exposure_id"]].append(dict(row))
            for row in source.execute("SELECT * FROM ng_exposure"):
                record = dict(row)
                identifier = "Exposure:" + row["id"]
                metadata = {"entity_type": "exposure", "original_exposure": record,
                            "contrasts": contrasts[row["id"]], "assertions": assertions[row["id"]],
                            "effect_data_storage": "original StudyEffect; no effect rows copied",
                            "component_causality_identified": False}
                writer.node(identifier, "diet", row["label"], metadata, aliases=decode(row["food_terms"], []))
                counts["exposures"] += 1
                for key in decode(row["component_keys"], []):
                    if key not in component_map:
                        continue
                    writer.edge(component_map[key], identifier, "component_of_intervention", "observed_intervention",
                                {"source": "human_intervention_bridge", "original_exposure": record,
                                 "causal_component_effect": False, "identity_relation": False,
                                 "expert_review_status": "pending"}, [row["source_id"]],
                                taxon="9606", review_status="source_checked")
                    counts["intervention_component_edges"] += 1
        if {"ng_evidence", "ng_entity", "ng_release"}.issubset(present):
            for row in source.execute("SELECT * FROM ng_evidence WHERE layer='health_outcome'"):
                record = dict(row)
                release = source.execute("SELECT * FROM ng_release WHERE id=?", (row["release_id"],)).fetchone()
                if not release:
                    raise ValueError("health assertion source release missing")
                source_record = dict(release); source_record["export_allowed"] = False
                writer.source(release["id"], source_record)
                for key in ("subject_id", "object_id"):
                    entity = source.execute("SELECT * FROM ng_entity WHERE id=?", (row[key],)).fetchone()
                    if not entity:
                        raise ValueError("health assertion entity missing")
                    metadata = decode(entity["metadata_json"])
                    metadata.update({"original_entity_id": entity["id"], "namespace": entity["namespace"],
                                     "source_identifier": entity["identifier"], "source_id": release["id"]})
                    writer.node(entity["id"], entity["kind"], entity["label"], metadata)
                writer.edge(row["subject_id"], row["object_id"], row["relation"], row["tier"],
                            {"source": "ng_evidence", "original_assertion": record,
                             "clinical_outcome_is_separate_from_molecular_change": True},
                            [release["id"]], _pmids_from_context(source, row["study_id"]),
                            taxon="9606" if row["organism"] == "Homo sapiens" else None,
                            review_status="source_checked", domains=["blood_pressure"], identifier=row["id"])
                counts["health_assertions"] += 1
        # Source database hash is recorded separately; not a made-up primary source.
        writer.db.execute("INSERT INTO meta VALUES (?,?)", ("core_import", canonical({**counts, "input_sha256": input_hash})))
    return counts


def reconcile_kfind_measurements(connection: sqlite3.Connection, core_path: Path, core_hash: str) -> dict:
    """Retain exact core missing-state semantics for the fixed Korean food panel.

    A status overlay is permitted only when source code, active release, amount,
    unit and basis agree. No quantity is changed and the graph's original status
    remains recorded in the reconciliation payload.
    """
    counts = {"rows_compared": 0, "statuses_reconciled": 0, "ambiguous": 0}
    with read_only(core_path) as core:
        if "composition_measurement" not in tables(core):
            return counts
        rows = list(connection.execute("""SELECT m.*,f.metadata_json food_meta,n.metadata_json nutrient_meta
             FROM measurement m JOIN node f ON f.id=m.food JOIN node n ON n.id=m.component
             WHERE f.id LIKE 'K-FIND:%'"""))
        for row in rows:
            food, nutrient = decode(row["food_meta"]), decode(row["nutrient_meta"])
            if "core_food_id" not in food or "core_component_id" not in nutrient:
                continue
            candidates = core.execute("""SELECT * FROM composition_measurement
                 WHERE food_id=? AND component_id=? AND release=?""",
                 (food["core_food_id"], nutrient["core_component_id"], food["core_active_release"])).fetchall()
            candidates = [p for p in candidates if p["source_ref"].startswith(row["food"] + ":")]
            if len(candidates) != 1:
                counts["ambiguous"] += 1
                continue
            expected = candidates[0]
            normalized_unit = lambda value: (value or "").replace("µ", "u").replace("μ", "u")
            if ((row["amount"] is None) != (expected["amount_per_100g"] is None) or
                (row["amount"] is not None and abs(row["amount"] - expected["amount_per_100g"]) > 1e-9) or
                normalized_unit(row["unit"]) != normalized_unit(expected["unit"]) or row["basis"] != expected["basis"]):
                raise ValueError("exact-mapped K-FIND composition differs from core: " + row["id"])
            counts["rows_compared"] += 1
            if row["status"] == expected["status"]:
                continue
            audit = {"original_graph_status": row["status"], "core_status": expected["status"],
                     "core_measurement_id": expected["id"], "core_source_ref": expected["source_ref"],
                     "core_release": expected["release"], "core_input_sha256": core_hash,
                     "quantity_changed": False, "basis_changed": False}
            raw = decode(row["raw_json"]); raw["explorer_status_reconciliation"] = audit
            connection.execute("UPDATE measurement SET status=?,raw_json=? WHERE id=?",
                               (expected["status"], canonical(raw), row["id"]))
            for edge in list(connection.execute("""SELECT id,context_json FROM edge
                 WHERE subject=? AND object=? AND json_extract(context_json,'$.measurement_id')=?""",
                 (row["food"], row["component"], row["id"]))):
                context = decode(edge["context_json"])
                context["status"] = expected["status"]
                context["status_reconciliation"] = audit
                connection.execute("UPDATE edge SET context_json=? WHERE id=?", (canonical(context), edge["id"]))
            counts["statuses_reconciled"] += 1
    return counts


def _pmids_from_context(source: sqlite3.Connection, study_id: str | None) -> list[str]:
    if "ng_study" not in tables(source) or not study_id:
        return []
    row = source.execute("SELECT pubmed_id FROM ng_study WHERE id=?", (study_id,)).fetchone()
    return [row[0]] if row and row[0] and row[0].isdigit() else []


def parquet_rows(path: Path, where: str = "", parameters: Iterable = ()):
    import duckdb
    connection = duckdb.connect()
    try:
        connection.execute("SET threads=8")
        cursor = connection.execute("SELECT * FROM read_parquet(?) " + where, [str(path), *parameters])
        columns = [item[0] for item in cursor.description]
        while batch := cursor.fetchmany(10000):
            for row in batch:
                yield dict(zip(columns, row, strict=True))
    finally:
        connection.close()


def expand_ctd(writer: ReleaseWriter, directory: Path, scope: dict) -> dict:
    manifest = decode((directory / "manifest.json").read_text(encoding="utf-8"))
    files = manifest["files"]
    required = ("CTD_curated_chemicals_diseases.csv.gz", "CTD_curated_genes_diseases.csv.gz",
                "CTD_chem_gene_ixns.csv.gz", "CTD_genes_pathways.csv.gz", "CTD_chemicals.csv.gz", "CTD_diseases.csv.gz")
    for name in required:
        if name not in files:
            raise ValueError("missing required CTD artifact " + name)
        record = files[name]
        if Path(record["parquet"]).name != record["parquet"]:
            raise ValueError("unsafe parquet artifact name")
        if sha256(directory / record["parquet"]) != record["parquet_sha256"]:
            raise ValueError("CTD Parquet hash mismatch: " + record["parquet"])
        writer.source(record["id"], record)
    disease_map = {item["id"]: item for item in scope["diseases"]}
    ids = list(disease_map)
    placeholders = ",".join("?" for _ in ids)
    dictionaries = {mesh(row["DiseaseID"]): row for row in parquet_rows(directory / files[required[5]]["parquet"],
                    "WHERE DiseaseID IN (" + placeholders + ")", ids)}
    if set(dictionaries) != set(ids):
        raise ValueError("scope IDs not all present in CTD dictionary: " + repr(set(ids) - set(dictionaries)))
    for identifier, item in disease_map.items():
        writer.node(identifier, "disease", item.get("label_ko") or dictionaries[identifier]["DiseaseName"],
                    {"dictionary": dictionaries[identifier], "source_id": files[required[5]]["id"],
                     "health_node_type": "disease_association"},
                    aliases=[dictionaries[identifier]["DiseaseName"]], label_en=dictionaries[identifier]["DiseaseName"],
                    domains=[item["domain"]])
    chemicals, genes = set(), set()
    stats: dict[str, int] = defaultdict(int)
    for filename, kind in ((required[0], "chemical"), (required[1], "gene")):
        record = files[filename]
        for row in parquet_rows(directory / record["parquet"], "WHERE DiseaseID IN (" + placeholders + ")", ids):
            target = mesh(row["DiseaseID"])
            subject = mesh(row["ChemicalID"]) if kind == "chemical" else gene(row["GeneID"])
            (chemicals if kind == "chemical" else genes).add(subject)
            writer.node(subject, kind, row["ChemicalName"] if kind == "chemical" else row["GeneSymbol"],
                        domains=[disease_map[target]["domain"]])
            for evidence in filter(None, row["DirectEvidence"].split("|")):
                if evidence not in {"marker/mechanism", "therapeutic"}:
                    raise ValueError("invalid CTD direct-evidence category")
                writer.edge(subject, target, kind + "_disease_" + evidence.replace("/", "_"), "curated",
                            {"direct_evidence": evidence, "source": "CTD", "species": "not_encoded_in_disease_export"},
                            [record["id"]], row["PubMedIDs"].split("|"),
                            domains=[disease_map[target]["domain"]])
                stats[kind + "_disease_source_rows"] += 1
    if chemicals and genes:
        record = files[required[2]]
        chemicals_tokens = sorted(x.removeprefix("MESH:") for x in chemicals)
        genes_tokens = sorted(x.removeprefix("NCBIGene:") for x in genes)
        where = "WHERE ChemicalID IN (" + ",".join("?" for _ in chemicals_tokens) + ") AND GeneID IN (" + ",".join("?" for _ in genes_tokens) + ")"
        for row in parquet_rows(directory / record["parquet"], where, [*chemicals_tokens, *genes_tokens]):
            subject, target = mesh(row["ChemicalID"]), gene(row["GeneID"])
            common_domains = writer.domains[subject] & writer.domains[target]
            legacy_context = {"source": "CTD", "interaction": row["Interaction"],
                              "actions": sorted(set(row["InteractionActions"].split("|"))), "gene_forms": row["GeneForms"],
                              "binding_label": False, "human_assay_does_not_imply_clinical_trial": True}
            # Match the existing source graph key exactly; adding taxon labels must
            # not duplicate every already-imported human interaction.
            identifier = hashlib.sha256(canonical([subject, target, "chemical_gene_influence", "curated",
                                        row["OrganismID"] or None, legacy_context]).encode("utf-8")).hexdigest()
            writer.edge(subject, target, "chemical_gene_influence", "curated",
                        {**legacy_context, "organism": row["Organism"]},
                        [record["id"]], row["PubMedIDs"].split("|"), taxon=row["OrganismID"] or None,
                        domains=common_domains or writer.domains[subject] | writer.domains[target], identifier=identifier)
            stats["chemical_gene_source_rows"] += 1
    record = files[required[3]]
    for row in parquet_rows(directory / record["parquet"]):
        subject = gene(row["GeneID"])
        if subject not in genes:
            continue
        target = row["PathwayID"]
        writer.node(target, "pathway", row["PathwayName"], domains=writer.domains[subject])
        writer.edge(subject, target, "annotated_to_pathway", "curated",
                    {"source": "CTD", "clinical_effect": False}, [record["id"]],
                    domains=writer.domains[subject])
        stats["gene_pathway_source_rows"] += 1
    record = files[required[4]]
    for row in parquet_rows(directory / record["parquet"]):
        identifier = mesh(row["ChemicalID"])
        if identifier not in chemicals:
            continue
        aliases = [a for key in ("MESHSynonyms", "CTDCuratedSynonyms") for a in row.get(key, "").split("|") if a]
        writer.node(identifier, "chemical", row["ChemicalName"],
                    {"dictionary": row, "structure_verified": False, "source_id": record["id"]}, aliases=aliases)
    # Inferred CTD tables are imported only if an independently hashed artifact
    # exists. A missing inference file is a coverage gap, not evidence of no links.
    stats["full_inference_artifact_available"] = int("CTD_chemicals_diseases.csv.gz" in files)
    return dict(stats)


def add_name_candidates(writer: ReleaseWriter, input_hashes: dict) -> int:
    groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
    nutrient_labels: dict[str, int] = defaultdict(int)
    for row in writer.db.execute("SELECT * FROM node WHERE kind='chemical'"):
        groups[normalize_name(row["label_en"] or row["label"])].append(row)
    for row in writer.db.execute("SELECT label,label_en FROM node WHERE kind='nutrient'"):
        nutrient_labels[normalize_name(row["label_en"] or row["label"])] += 1
    source_id = "Explorer-name-correspondence:" + hashlib.sha256(canonical(input_hashes).encode()).hexdigest()
    writer.source(source_id, {"id": source_id, "source": "source-label correspondence audit", "sha256": input_hashes["graph_db"],
                              "input_hashes": input_hashes, "version": "normalized-nfkc-casefold-label-v1",
                              "export_allowed": True, "rights": "Research-generated correspondence; underlying source terms remain applicable"})
    count = 0
    for row in list(writer.db.execute("SELECT * FROM node WHERE kind='nutrient'")):
        normalized = normalize_name(row["label_en"] or row["label"])
        matches = groups.get(normalized, [])
        if len(matches) != 1 or nutrient_labels[normalized] != 1:
            continue
        target = matches[0]
        writer.edge(row["id"], target["id"], "candidate_name_correspondence", "candidate",
                    {"source": "normalized_source_labels", "source_label": row["label_en"] or row["label"],
                     "target_label": target["label_en"] or target["label"], "name_normalization": "NFKC+casefold+whitespace",
                     "molecular_equivalence": False, "quantity_transfer_allowed": False,
                     "requires_chemical_form_and_source_review": True}, [source_id],
                    review_status="candidate", domains=writer.domains[target["id"]])
        count += 1
    return count


def import_literature(writer: ReleaseWriter, path: Path, input_hash: str) -> dict:
    counts = {"candidate_assertions": 0, "human_approved_assertions": 0}
    source_id = "Explorer-literature:" + input_hash
    with read_only(path) as connection:
        snapshot = connection.execute("SELECT value_json FROM metadata WHERE key='snapshot'").fetchone()
        writer.source(source_id, {"id": source_id, "source": "frozen literature candidate snapshot", "sha256": input_hash,
                                 "path": str(path), "snapshot": decode(snapshot[0]) if snapshot else {},
                                 "export_allowed": False, "rights": "Source text and redistribution terms require source-level review"})
        for row in connection.execute("SELECT * FROM assertion"):
            record = decode(row["payload_json"])
            payload = record.get("payload", {})
            for side in ("subject", "object"):
                raw_identifier = row[side]
                identifier = "Literature:" + raw_identifier
                mention = payload.get(side + "_mention", {})
                kind = {"compound": "chemical", "protein": "gene"}.get(mention.get("kind"), mention.get("kind", "outcome"))
                label = mention.get("payload", {}).get("label") or mention.get("text") or raw_identifier
                writer.node(identifier, kind, label, {"original_identifier": raw_identifier,
                            "source_id": source_id, "mapping_status": "candidate_namespace_not_merged",
                            "review_status": "candidate"})
            domain = "blood_pressure" if "blood-pressure" in row["object"] or "hypertension" in row["object"] else None
            writer.edge("Literature:" + row["subject"], "Literature:" + row["object"], row["predicate"], "extracted",
                        {"source": "literature_candidate_snapshot", "original_assertion": record,
                         "causal_relation": False, "extraction_accuracy": "not_evaluated"}, [source_id], [row["pmid"]],
                        taxon=None, review_status="candidate", domains=[domain] if domain else [],
                        identifier="LiteratureAssertion:" + row["id"])
            counts["candidate_assertions"] += 1
    return counts


def finalize_domains(writer: ReleaseWriter, scope: dict) -> None:
    # Imported graph domains propagate from explicit disease seeds through recorded
    # molecular relations; food/composition domains remain navigation labels only.
    for item in scope["diseases"]:
        writer.domains[item["id"]].add(item["domain"])
    for row in writer.db.execute("SELECT subject,object FROM edge WHERE relation LIKE '%disease%'"):
        writer.domains[row["subject"]].update(writer.domains[row["object"]])
    for row in writer.db.execute("SELECT subject,object,relation FROM edge WHERE relation IN ('annotated_to_pathway','candidate_name_correspondence')"):
        if row["relation"] == "candidate_name_correspondence":
            writer.domains[row["subject"]].update(writer.domains[row["object"]])
        else:
            writer.domains[row["object"]].update(writer.domains[row["subject"]])
    for row in writer.db.execute("SELECT id,kind FROM node"):
        if row["kind"] in {"food", "nutrient", "diet"}:
            writer.domains[row["id"]].update(scope["domains"])
        domains = sorted(writer.domains[row["id"]])
        writer.db.execute("UPDATE node SET domains_json=? WHERE id=?", (canonical(domains), row["id"]))
        writer.db.executemany("INSERT OR IGNORE INTO node_domain VALUES (?,?)", [(row["id"], d) for d in domains])
    for row in writer.db.execute("SELECT id,subject,object,relation,domains_json FROM edge"):
        domains = set(decode(row["domains_json"], []))
        if not domains:
            domains = writer.domains[row["subject"]] & writer.domains[row["object"]]
        writer.db.execute("UPDATE edge SET domains_json=? WHERE id=?", (canonical(sorted(domains)), row["id"]))
        writer.db.executemany("INSERT OR IGNORE INTO edge_domain VALUES (?,?)", [(row["id"], d) for d in sorted(domains)])
    writer.db.execute("""UPDATE node SET degree=(SELECT count(*) FROM edge WHERE subject=node.id)
                       +(SELECT count(*) FROM edge WHERE object=node.id)""")


def build_release(graph_db: Path | str, core_db: Path | str, output_dir: Path | str,
                  release_id: str, scope_path: Path | str, ctd_dir: Path | str | None = None,
                  literature_db: Path | str | None = None) -> dict:
    graph_db, core_db, output_dir, scope_path = map(Path, (graph_db, core_db, output_dir, scope_path))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}", release_id):
        raise ValueError("invalid release_id")
    if output_dir.exists():
        raise FileExistsError("immutable release already exists: " + str(output_dir))
    # Preserve the exact builder bytes before a long ingest, even if deployment
    # updates the working source file while this process is still running.
    builder_source = Path(__file__).read_bytes()
    builder_hash = hashlib.sha256(builder_source).hexdigest()
    input_paths = {"graph_db": graph_db, "core_db": core_db, "scope": scope_path}
    if ctd_dir:
        input_paths["ctd_manifest"] = Path(ctd_dir) / "manifest.json"
    if literature_db:
        input_paths["literature_db"] = Path(literature_db)
    input_stats = {key: (path.stat().st_size, path.stat().st_mtime_ns) for key, path in input_paths.items()}
    scope = decode(scope_path.read_text(encoding="utf-8"))
    if set(scope["domains"]) != {"blood_pressure", "lipids", "obesity"}:
        raise ValueError("unexpected domain scope")
    hashes = {"graph_db": sha256(graph_db), "core_db": sha256(core_db), "scope": sha256(scope_path)}
    if ctd_dir:
        hashes["ctd_manifest"] = sha256(Path(ctd_dir) / "manifest.json")
    if literature_db:
        hashes["literature_db"] = sha256(Path(literature_db))
    staging = output_dir.parent / ("." + output_dir.name + ".building")
    if staging.exists():
        raise FileExistsError("staging directory already exists: " + str(staging))
    staging.mkdir(parents=True)
    connection = sqlite3.connect((staging / "explorer.sqlite").resolve().as_uri() + "?mode=rwc", uri=True)
    connection.row_factory = sqlite3.Row
    # Connection-local settings on the new derivative, never source databases.
    connection.execute("PRAGMA cache_size=-1048576")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.executescript(SCHEMA)
    writer = ReleaseWriter(connection)
    try:
        import_graph(writer, graph_db)
        core_counts = import_core(writer, core_db, hashes["core_db"])
        core_counts["kfind_composition_validation"] = reconcile_kfind_measurements(connection, core_db, hashes["core_db"])
        ctd_counts = expand_ctd(writer, Path(ctd_dir), scope) if ctd_dir else {"status": "not_supplied"}
        name_count = add_name_candidates(writer, hashes)
        lit_counts = import_literature(writer, Path(literature_db), hashes["literature_db"]) if literature_db else {"status": "not_supplied"}
        finalize_domains(writer, scope)
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = [list(row) for row in connection.execute("PRAGMA foreign_key_check")]
        missing_sources = connection.execute("""SELECT count(*) FROM edge WHERE json_array_length(source_ids_json)=0""").fetchone()[0]
        invalid_source_refs = connection.execute("""SELECT count(*) FROM edge,json_each(edge.source_ids_json) refs
                     LEFT JOIN source ON source.id=refs.value WHERE source.id IS NULL""").fetchone()[0]
        if integrity != "ok" or foreign_keys or missing_sources or invalid_source_refs:
            raise ValueError("release integrity/provenance failed: " + canonical({
                "integrity": integrity, "foreign_keys": foreign_keys[:5],
                "missing_sources": missing_sources, "invalid_source_refs": invalid_source_refs}))
        if any((path.stat().st_size, path.stat().st_mtime_ns) != input_stats[key]
               for key, path in input_paths.items()):
            raise ValueError("source inputs changed during release build; rebuild from stable sources")
        counts = {name: connection.execute("SELECT count(*) FROM " + name).fetchone()[0]
                  for name in ("node", "edge", "measurement", "source")}
        counts["by_kind"] = {row[0]: row[1] for row in connection.execute("SELECT kind,count(*) FROM node GROUP BY kind")}
        counts["by_relation"] = {row[0]: row[1] for row in connection.execute("SELECT relation,count(*) FROM edge GROUP BY relation")}
        counts["by_tier"] = {row[0]: row[1] for row in connection.execute("SELECT tier,count(*) FROM edge GROUP BY tier")}
        counts["unique_pmids"] = connection.execute("SELECT count(DISTINCT value) FROM edge,json_each(edge.pmids_json)").fetchone()[0]
        counts["independent_studies"] = None
        gaps = ["Relationships do not establish food efficacy, personal health effects, or causality.",
                "Food/nutrient domain membership is navigation scope, not disease evidence.",
                "Name correspondences are candidate links; quantity transfer and molecular identity are not established.",
                "Same-person dietary/metabolomic/expression/methylation clinical integration remains unverified.",
                "CTD disease-association exports do not encode species; unknown remains unknown."]
        if not ctd_dir or not ctd_counts.get("full_inference_artifact_available"):
            gaps.append("Full CTD inferred-association artifact not supplied; retained source graph inferences only.")
        manifest = {"schema_version": 1, "release_id": release_id,
                    "created_at": datetime.now(timezone.utc).isoformat(), "counts": counts,
                    "input_hashes": hashes, "source_paths": {"graph_db": str(graph_db), "core_db": str(core_db)},
                    "scope": scope, "gaps": gaps, "core_import": core_counts,
                    "ctd_expansion": ctd_counts, "candidate_name_correspondences": name_count,
                    "literature_import": lit_counts, "individual_prediction": False,
                    "count_definitions": {"edge": "Source-preserved relationship records; composition is included.",
                                          "unique_pmids": "Distinct cited PMID identifiers, not independently validated trials.",
                                          "independent_studies": "Not inferred across CTD, literature and clinical source namespaces.",
                                          "measurement": "Source-reported composition rows, including zeros and missing values."},
                    "sources": [dict(id=r[0], metadata=decode(r[1])) for r in connection.execute("SELECT * FROM source")],
                    "builder_sha256": builder_hash, "builder_archive": "builder_source.py",
                    "input_stat_verification": "unchanged_size_and_mtime_ns", "live_pointer_modified": False}
        for key, value in manifest.items():
            connection.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, canonical(value)))
        connection.commit()
        connection.execute("ANALYZE")
        connection.commit()
        connection.close()
        verify_closed_sqlite(staging / "explorer.sqlite")
        manifest["database_sha256"] = sha256(staging / "explorer.sqlite")
        (staging / "builder_source.py").write_bytes(builder_source)
        (staging / "manifest.json").write_text(canonical(manifest) + "\n", encoding="utf-8")
        coverage = {"release_id": release_id, "counts": counts, "core_food_mapped": core_counts["exact_food_mapping"],
                    "foods_without_core_calculator": counts["by_kind"].get("food", 0) - core_counts["exact_food_mapping"],
                    "candidate_name_correspondences": name_count, "health_assertions": core_counts["health_assertions"],
                    "literature": lit_counts, "integrity_check": integrity, "foreign_key_errors": foreign_keys,
                    "gaps": gaps, "food_to_health_causal_paths": 0}
        (staging / "coverage.json").write_text(canonical(coverage) + "\n", encoding="utf-8")
        (staging / "verification.json").write_text(canonical({
            "release_id": release_id, "database_sha256": manifest["database_sha256"],
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "integrity_check": integrity, "foreign_key_errors": foreign_keys,
            "source_stats_after_build": {key: {"bytes": path.stat().st_size,
                "mtime_ns": path.stat().st_mtime_ns} for key, path in input_paths.items()},
            "scope": "Build integrity and unchanged immutable inputs; not scientific review or API validation"
        }) + "\n", encoding="utf-8")
        # Atomic directory rename on the target filesystem; never changes current.
        os.replace(staging, output_dir)
        return manifest
    except BaseException as error:
        connection.close()
        # Keep failed derivatives auditable. They never receive an active pointer.
        if staging.parent.resolve() == output_dir.parent.resolve() and staging.name == "." + output_dir.name + ".building":
            (staging / "FAILED.json").write_text(canonical({"error_type": type(error).__name__,
                "error": str(error), "release_id": release_id}), encoding="utf-8")
            failed = staging.with_name("." + output_dir.name + ".failed-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f"))
            staging.rename(failed)
            error.add_note("Unactivated diagnostic files: " + str(failed))
        raise


def upgrade_release(base_dir: Path | str, core_db: Path | str, output_dir: Path | str,
                    release_id: str) -> dict:
    """Apply verified core status semantics to a fresh derivative release.

    This avoids re-reading the 19-GiB effect database when the parent receipt's
    exact path, size and nanosecond mtime still match. No source or parent release
    is changed; the source hash is inherited with explicit cache verification.
    """
    base_dir, core_db, output_dir = map(Path, (base_dir, core_db, output_dir))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}", release_id):
        raise ValueError("invalid release_id")
    if output_dir.exists():
        raise FileExistsError("immutable release already exists")
    original_manifest = base_dir / "manifest.json"
    manifest = decode(original_manifest.read_text(encoding="utf-8"))
    receipt = decode((base_dir / "verification.json").read_text(encoding="utf-8"))
    core_stat = core_db.stat()
    expected = receipt["source_stats_after_build"]["core_db"]
    if (core_db.resolve() != Path(manifest["source_paths"]["core_db"]).resolve() or
        core_stat.st_size != expected["bytes"] or core_stat.st_mtime_ns != expected["mtime_ns"]):
        raise ValueError("cached core hash not reusable: source path/size/mtime changed")
    source_body = Path(__file__).read_bytes()
    staging = output_dir.parent / ("." + output_dir.name + ".building")
    if staging.exists():
        raise FileExistsError("staging already exists")
    staging.mkdir(parents=True)
    db = sqlite3.connect(staging / "explorer.sqlite")
    db.row_factory = sqlite3.Row
    try:
        parent = read_only(base_dir / "explorer.sqlite")
        try:
            parent.backup(db)
        finally:
            parent.close()
        result = reconcile_kfind_measurements(db, core_db, manifest["input_hashes"]["core_db"])
        if (core_db.stat().st_size, core_db.stat().st_mtime_ns) != (core_stat.st_size, core_stat.st_mtime_ns):
            raise ValueError("core changed during derivative build")
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("derivative release integrity failed")
        manifest["derived_from"] = {"release_id": manifest["release_id"],
            "manifest_sha256": sha256(original_manifest), "database_sha256": manifest["database_sha256"]}
        manifest["release_id"] = release_id
        manifest["created_at"] = datetime.now(timezone.utc).isoformat()
        manifest["builder_sha256"] = hashlib.sha256(source_body).hexdigest()
        manifest["builder_archive"] = "builder_source.py"
        manifest["build_method"] = "SQLite backup of validated parent + exact K-FIND status reconciliation"
        manifest["core_import"]["kfind_composition_validation"] = result
        manifest["counts"]["independent_studies"] = None
        manifest["core_hash_cache_verification"] = {"source": str(core_db), "bytes": core_stat.st_size,
            "mtime_ns": core_stat.st_mtime_ns, "sha256": manifest["input_hashes"]["core_db"],
            "verification": "exact parent receipt path, size and mtime_ns match before/after upgrade"}
        manifest.pop("database_sha256", None)
        for key, value in manifest.items():
            db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, canonical(value)))
        db.commit()
        db.close()
        manifest["database_sha256"] = sha256(staging / "explorer.sqlite")
        (staging / "builder_source.py").write_bytes(source_body)
        (staging / "manifest.json").write_text(canonical(manifest) + "\n", encoding="utf-8")
        coverage = decode((base_dir / "coverage.json").read_text(encoding="utf-8"))
        coverage.update({"release_id": release_id, "kfind_composition_validation": result})
        (staging / "coverage.json").write_text(canonical(coverage) + "\n", encoding="utf-8")
        receipt.update({"release_id": release_id, "verified_at": manifest["created_at"],
                        "korean_composition_rows_compared": result["rows_compared"],
                        "korean_composition_statuses_reconciled": result["statuses_reconciled"],
                        "builder_archive_sha256": manifest["builder_sha256"],
                        "builder_archive_matches_manifest": True,
                        "core_hash_cache_verification": manifest["core_hash_cache_verification"],
                        "parent_release_id": manifest["derived_from"]["release_id"]})
        (staging / "verification.json").write_text(canonical(receipt) + "\n", encoding="utf-8")
        os.replace(staging, output_dir)
        return manifest
    except BaseException:
        db.close()
        if staging.parent.resolve() == output_dir.parent.resolve() and staging.name == "." + output_dir.name + ".building":
            shutil.rmtree(staging)
        raise


def annotation_targets(annotation: Any, exact_labels: dict[str, list[str]]) -> list[tuple[str, str]]:
    """Map only exact semicolon-delimited source gene tokens to current labels."""
    if not isinstance(annotation, str):
        return []
    return sorted({(node_id, token) for token in (part.strip() for part in annotation.split(";"))
                   if token for node_id in exact_labels.get(token, [])})


def effect_record_hash(record: dict) -> str:
    body = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def add_omics_references(connection: sqlite3.Connection, core_path: Path, chunk_size: int = 5000) -> dict:
    """Index source CpG annotation links; effect values remain only in core."""
    connection.executescript("""
        CREATE TABLE omics_reference(node_id TEXT NOT NULL REFERENCES node(id),effect_id INTEGER NOT NULL,
          series_accession TEXT NOT NULL,feature_type TEXT NOT NULL,feature_id TEXT NOT NULL,
          contrast TEXT NOT NULL,analysis_version TEXT NOT NULL,relation TEXT NOT NULL,context_json TEXT NOT NULL,
          PRIMARY KEY(node_id,effect_id));
        CREATE INDEX omics_reference_node_idx ON omics_reference(node_id);
        CREATE INDEX omics_reference_series_idx ON omics_reference(series_accession);
        CREATE TABLE omics_annotation(id TEXT PRIMARY KEY,feature_type TEXT NOT NULL,feature_id TEXT NOT NULL,
          annotation_gene TEXT NOT NULL,genome_build TEXT,source_file_sha256 TEXT,metadata_json TEXT NOT NULL);
    """)
    labels: dict[str, list[str]] = defaultdict(list)
    for row in connection.execute("SELECT id,label FROM node WHERE kind='gene'"):
        labels[row["label"]].append(row["id"])
    counts = {"cpg_rows_scanned": 0, "matched_effects": 0, "reference_count": 0,
              "annotation_count": 0, "gene_nodes_referenced": 0}
    started = time.monotonic()
    with read_only(core_path) as core:
        if "study_effect" not in tables(core):
            counts["duration_seconds"] = round(time.monotonic() - started, 3)
            return counts
        # Indexed source row order; one process, one read-only connection, bounded
        # batches. Whole rows are needed only to authenticate matched records.
        cursor = core.execute("SELECT * FROM study_effect WHERE feature_type='cpg'")
        while batch := cursor.fetchmany(chunk_size):
            reference_rows, annotations = [], []
            for row in batch:
                counts["cpg_rows_scanned"] += 1
                metadata = decode(row["metadata_json"])
                annotation = metadata.get("annotation_gene")
                matched = annotation_targets(annotation, labels)
                if not matched:
                    continue
                record = dict(row)
                genome = metadata.get("genome_build")
                source_hash = metadata.get("source_file_sha256")
                annotation_key = hashlib.sha256(canonical([row["feature_type"], row["feature_id"],
                                                          annotation, genome, source_hash]).encode()).hexdigest()
                annotation_context = {"annotation_gene": annotation, "genome_build": genome,
                    "source_file_sha256": source_hash, "source_filename": metadata.get("source_filename"),
                    "chromosome": metadata.get("chromosome"), "position": metadata.get("position"),
                    "annotation_kind": metadata.get("annotation_kind", "unknown"),
                    "relation": "platform_probe_gene_annotation",
                    "expression_regulation_evidence": False, "nearest_gene_claim": False,
                    "source_hash_scope": "original effect-metadata source file; original platform annotation file not established"}
                annotations.append((annotation_key, row["feature_type"], row["feature_id"], annotation,
                                    genome, source_hash, canonical(annotation_context)))
                fingerprint = effect_record_hash(record)
                for node_id, token in matched:
                    context = {**annotation_context, "annotation_id": annotation_key,
                               "matched_exact_gene_token": token,
                               "match_basis": "exact current node.label and semicolon-delimited annotation_gene token",
                               "effect_record_sha256": fingerprint,
                               "effect_source": "core study_effect; no values copied",
                               "gene_entity_identity_from_label": "not independently verified"}
                    reference_rows.append((node_id, row["id"], row["series_accession"], row["feature_type"],
                        row["feature_id"], row["contrast"], row["analysis_version"],
                        "platform_probe_gene_annotation", canonical(context)))
                counts["matched_effects"] += 1
            connection.executemany("INSERT OR IGNORE INTO omics_annotation VALUES (?,?,?,?,?,?,?)", annotations)
            connection.executemany("INSERT OR IGNORE INTO omics_reference VALUES (?,?,?,?,?,?,?,?,?)", reference_rows)
            if counts["cpg_rows_scanned"] % 250000 < chunk_size:
                print(canonical({"stage": "omics_reference_scan", "scanned": counts["cpg_rows_scanned"],
                                 "matched_effects": counts["matched_effects"],
                                 "elapsed_seconds": round(time.monotonic() - started, 1)}), flush=True)
        counts["reference_count"] = connection.execute("SELECT count(*) FROM omics_reference").fetchone()[0]
        counts["annotation_count"] = connection.execute("SELECT count(*) FROM omics_annotation").fetchone()[0]
        counts["gene_nodes_referenced"] = connection.execute("SELECT count(DISTINCT node_id) FROM omics_reference").fetchone()[0]
    counts["duration_seconds"] = round(time.monotonic() - started, 3)
    return counts


def upgrade_omics_release(base_dir: Path | str, core_db: Path | str, output_dir: Path | str,
                          release_id: str) -> dict:
    """Publish a derivative containing annotation references to immutable effects."""
    base_dir, core_db, output_dir = map(Path, (base_dir, core_db, output_dir))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}", release_id):
        raise ValueError("invalid release_id")
    if output_dir.exists():
        raise FileExistsError("immutable release already exists")
    manifest = decode((base_dir / "manifest.json").read_text(encoding="utf-8"))
    receipt = decode((base_dir / "verification.json").read_text(encoding="utf-8"))
    core_stat = core_db.stat()
    expected = receipt["source_stats_after_build"]["core_db"]
    if (core_db.resolve() != Path(manifest["source_paths"]["core_db"]).resolve() or
        core_stat.st_size != expected["bytes"] or core_stat.st_mtime_ns != expected["mtime_ns"]):
        raise ValueError("cached core hash not reusable: source path/size/mtime changed")
    staging = output_dir.parent / ("." + output_dir.name + ".building")
    if staging.exists():
        raise FileExistsError("staging already exists")
    staging.mkdir(parents=True)
    body = Path(__file__).read_bytes()
    db = sqlite3.connect(staging / "explorer.sqlite")
    db.row_factory = sqlite3.Row
    try:
        parent = read_only(base_dir / "explorer.sqlite")
        try:
            parent.backup(db)
        finally:
            parent.close()
        db.execute("PRAGMA foreign_keys=ON")
        result = add_omics_references(db, core_db)
        if (core_db.stat().st_size, core_db.stat().st_mtime_ns) != (core_stat.st_size, core_stat.st_mtime_ns):
            raise ValueError("core changed during annotation indexing")
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("omics derivative release integrity failed")
        manifest["derived_from"] = {"release_id": manifest["release_id"],
            "manifest_sha256": sha256(base_dir / "manifest.json"), "database_sha256": manifest["database_sha256"]}
        manifest.update({"release_id": release_id, "created_at": datetime.now(timezone.utc).isoformat(),
                         "builder_sha256": hashlib.sha256(body).hexdigest(), "builder_archive": "builder_source.py",
                         "build_method": "Validated parent SQLite backup + exact probe-gene annotation effect references",
                         "omics_reference_build": result,
                         "omics_reference_source_identity": {"bytes": core_stat.st_size, "mtime_ns": core_stat.st_mtime_ns,
                                                               "sha256": manifest["input_hashes"]["core_db"]},
                         "omics_reference_runtime_verification": "Verify exact effect.id, five context keys and whole original effect-record SHA256; core mtime alone is not a runtime gate."})
        manifest["counts"]["omics_reference"] = result["reference_count"]
        manifest["counts"]["omics_annotation"] = result["annotation_count"]
        manifest["gaps"].append("Probe-gene annotations are not expression regulation, eQTM, nearest-gene proof, or dietary health-effect evidence.")
        manifest.pop("database_sha256", None)
        for key, value in manifest.items():
            db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, canonical(value)))
        db.commit()
        db.execute("ANALYZE omics_reference")
        db.commit()
        db.close()
        manifest["database_sha256"] = sha256(staging / "explorer.sqlite")
        (staging / "builder_source.py").write_bytes(body)
        (staging / "manifest.json").write_text(canonical(manifest) + "\n", encoding="utf-8")
        coverage = decode((base_dir / "coverage.json").read_text(encoding="utf-8"))
        coverage.update({"release_id": release_id, "omics_reference_build": result, "counts": manifest["counts"]})
        (staging / "coverage.json").write_text(canonical(coverage) + "\n", encoding="utf-8")
        receipt.update({"release_id": release_id, "verified_at": manifest["created_at"],
                        "omics_reference_build": result, "builder_archive_sha256": manifest["builder_sha256"],
                        "builder_archive_matches_manifest": True,
                        "parent_release_id": manifest["derived_from"]["release_id"]})
        (staging / "verification.json").write_text(canonical(receipt) + "\n", encoding="utf-8")
        os.replace(staging, output_dir)
        return manifest
    except BaseException:
        db.close()
        if staging.parent.resolve() == output_dir.parent.resolve() and staging.name == "." + output_dir.name + ".building":
            shutil.rmtree(staging)
        raise
