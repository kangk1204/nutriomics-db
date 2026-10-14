"""Scientific scope and public-download integrity checks, without network access."""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from nutrigenomics.annotations import search_annotations
from nutrigenomics.expansion import connect, migrate, validate
from scripts.collect_regulatory_expansion import (
    Snapshots,
    import_atlas_evo,
    import_screen,
    release,
    source_number,
)


def snapshot(tmp_path, raw, name="source", suffix=".gz"):
    path = tmp_path / (name + suffix)
    path.write_bytes(raw)
    return {"url": "https://downloads.wenglab.org/" + path.name,
        "sha256": hashlib.sha256(raw).hexdigest(), "local_path": str(path),
        "retrieved_at": "2026-10-01T00:00:00+00:00", "bytes": len(raw)}


@pytest.fixture
def db(tmp_path):
    connection = connect(tmp_path / "annotations.sqlite3")
    migrate(connection)
    yield connection
    connection.close()


def compress(rows):
    return gzip.compress(("\n".join("\t".join(row) for row in rows) + "\n").encode())


def crispr_row():
    return ["EH38E2911345", "ENSG00000108179", "PPIF ", "protein_coding",
        "chr10_78937627_78938127", "CRISPRi-FlowFISH", "ENCSR525DAZ", "Jurkat", "-0.0545", "0.81148"]


def graph_row(method="GraphRegLR", cell="K562", symbol="ABCA1", target="ENSG00000165029"):
    return ["EH38E2776539", target, symbol, "protein_coding", method,
        "chr1_778570_778919", "ENCFF431TFM", cell, "0.93"]


def test_crispr_is_tested_pair_not_significant_or_dietary_effect(db, tmp_path):
    raw = compress([crispr_row()])
    src = snapshot(tmp_path, raw)
    with db:
        result = import_screen(db, src, raw, "crispr")
    evidence = dict(db.execute("SELECT * FROM ng_evidence").fetchone())
    context = json.loads(evidence["context_json"])
    assert result["admitted_rows"] == 1
    assert evidence["relation"] == "experimentally_tested_ccre_gene_pair"
    assert evidence["tier"] == "in_vitro" and evidence["inference"] == "descriptive"
    assert evidence["tissue"] == "Jurkat"
    assert all(evidence[field] is None for field in ["effect", "p_value", "q_value", "n_people", "dose"])
    assert context["validation_status"] == "tested_pair_significance_not_classified"
    assert context["source_numeric_fields"]["column_10_untyped"] == "0.81148"
    assert context["source_symbol"] == "PPIF "  # original whitespace/identifier retained
    assert context["export_allowed"] is False
    assert context["genome_build"] == "GRCh38/hg38"
    assert validate(db) == []


def test_graphreg_filters_real_row_method_cell_panel_and_deduplicates(db, tmp_path):
    raw = compress([graph_row("rE2G_(DNase_only)"), graph_row(), graph_row(),
        graph_row(cell="neural_progenitor_cell"), graph_row(symbol="OTHER")])
    src = snapshot(tmp_path, raw)
    with db:
        result = import_screen(db, src, raw, "graphreg")
        retry = import_screen(db, src, raw, "graphreg")
    assert result == retry
    assert result["admitted_rows"] == 1
    assert result["source_counts"]["excluded_method:rE2G_(DNase_only)"] == 1
    assert result["source_counts"]["excluded_exact_duplicate_row"] == 1
    row = db.execute("SELECT * FROM ng_evidence").fetchone()
    context = json.loads(row["context_json"])
    assert row["relation"] == "predicted_ccre_gene_link" and row["inference"] == "association"
    assert context["validation_status"] == "computational_prediction_not_experimentally_validated"
    assert row["effect"] is None and context["source_numeric_fields"] == {"model_score_untyped": "0.93"}
    assert db.execute("SELECT count(*) FROM ng_evidence").fetchone()[0] == 1
    assert validate(db) == []


def test_native_loc_target_is_not_silently_converted_to_ensembl(db, tmp_path):
    raw = compress([graph_row(target="LOC100288069")])
    with db:
        import_screen(db, snapshot(tmp_path, raw), raw, "graphreg")
    row = db.execute("SELECT * FROM ng_entity WHERE kind='gene'").fetchone()
    assert row["identifier"] == "LOC100288069" and row["namespace"] == "SCREEN-native-gene"
    assert db.execute("SELECT count(*) FROM ng_mapping").fetchone()[0] == 0


@pytest.mark.parametrize("changed", ["NaN", "inf", "nan"])
def test_nonfinite_screen_fields_roll_back_whole_source(db, tmp_path, changed):
    bad = crispr_row()
    bad[-1] = changed
    raw = compress([crispr_row(), bad])
    with pytest.raises(ValueError), db:
        import_screen(db, snapshot(tmp_path, raw), raw, "crispr")
    assert db.execute("SELECT count(*) FROM ng_evidence").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM ng_release").fetchone()[0] == 0


def evo_sources(tmp_path, p="7e-06", original_p="7e-06", gene="BCR", population="Both"):
    output = io.BytesIO()
    table = pd.DataFrame([
        ["Table S3a. List of local eQTMs"],
        ["c For each local CpG-gene association the pvalue of the spearman's rank correlation is shown."],
        ["e Both means the merged dataset"],
        ["CpG", "Ensembl_ID", "Gene_symbol", "Chromosome", "CpG_Positiona", "Gene_Positionb", "Pvaluec", "Rhod", "Populatione"],
        ["cg00281467", "ENSG00000186716", "BCR", 22, 23522460, 23521891, original_p, "-0.51", population],
    ])
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        table.to_excel(writer, sheet_name="Table_S3a", header=False, index=False)
    original = output.getvalue()
    csv_output = io.StringIO()
    fields = ["probeid", "gene", "pvalue", "tissue", "cohort", "beta", "FDR", "num_controls", "num_cases", "eQTM_label"]
    writer = csv.DictWriter(csv_output, fieldnames=fields)
    writer.writeheader()
    writer.writerow(dict(zip(fields, ["cg00281467", gene, p, "Primary Monocyte", "EvoImmunoPop", "NA", "NA", "156", "0", "cis"], strict=True)))
    raw = csv_output.getvalue().encode()
    return (snapshot(tmp_path, raw, "atlas", ".csv"), raw,
        snapshot(tmp_path, original, "primary", ".xlsx"), original,
        snapshot(tmp_path, b"MIT code, not data license", "code", ".R"),
        snapshot(tmp_path, b"article", "article", ".html"))


def test_atlas_cross_verified_association_uses_primary_rho_not_missing_beta(db, tmp_path):
    args = evo_sources(tmp_path)
    with db:
        result = import_atlas_evo(db, *args)
        retry = import_atlas_evo(db, *args)
    assert result == retry
    assert result["admitted"] == {"source_checked": 1}
    assert result["cohorts_added"] == 1 and result["FHS_duplicates_added"] == 0
    row = dict(db.execute("SELECT * FROM ng_evidence").fetchone())
    context = json.loads(row["context_json"])
    assert row["object_id"] == "Ensembl:ENSG00000186716"
    assert row["effect"] == -.51 and row["effect_unit"] == "Spearman rank correlation rho"
    assert row["q_value"] is None and row["n_people"] == 156
    assert context["atlas_beta_status"] == "missing" and context["atlas_fdr_status"] == "missing"
    assert context["primary_publication_status"] == "peer_reviewed"
    assert context["atlas_publication_status"] == "preprint" and context["export_allowed"] is False
    assert row["inference"] == "association" and row["tier"] == "human_observational"
    assert db.execute("SELECT count(*) FROM ng_study").fetchone()[0] == 1
    assert validate(db) == []


def test_missing_primary_match_is_candidate_hidden_from_annotation_search(db, tmp_path):
    with db:
        result = import_atlas_evo(db, *evo_sources(tmp_path, gene="UNVERIFIED"))
    assert result["admitted"] == {"candidate": 1}
    row = db.execute("SELECT * FROM ng_evidence").fetchone()
    assert row["effect"] is None and row["n_people"] is None
    with Session(create_engine("sqlite:///" + str(tmp_path / "annotations.sqlite3"))) as session:
        assert search_annotations(session)["total"] == 0


def test_p_inequality_is_preserved_without_equal_p_or_recalculated_fdr(db, tmp_path):
    with db:
        result = import_atlas_evo(db, *evo_sources(tmp_path, p="<2.2e-16", original_p="<2.2e-16"))
    assert result["admitted"] == {"source_checked": 1}
    row = db.execute("SELECT * FROM ng_evidence").fetchone()
    assert row["p_value"] is None and row["q_value"] is None
    assert json.loads(row["context_json"])["source_p_qualifier"] == "<"


def test_missing_atlas_p_remains_missing_but_original_bound_is_recoverable(db, tmp_path):
    args = evo_sources(tmp_path, p="NA", original_p="<2.2e-16")
    source_bytes = [(Path(src["local_path"]), Path(src["local_path"]).read_bytes()) for src in (args[0], args[2], args[4], args[5])]
    with db:
        result = import_atlas_evo(db, *args)
    assert result["admitted"] == {"source_checked": 1}
    row = db.execute("SELECT * FROM ng_evidence").fetchone()
    context = json.loads(row["context_json"])
    assert context["source_row"]["pvalue"] == "NA" and context["atlas_p_status"] == "missing"
    assert context["source_p_qualifier"] == "<" and row["p_value"] is None
    assert row["effect"] == -.51
    assert all(path.read_bytes() == raw for path, raw in source_bytes)


@pytest.mark.parametrize("population", ["EUB", "AFB"])
def test_population_specific_association_does_not_inflate_n_to_whole_cohort(db, tmp_path, population):
    with db:
        import_atlas_evo(db, *evo_sources(tmp_path, population=population))
    row = db.execute("SELECT * FROM ng_evidence").fetchone()
    assert row["n_people"] == 78
    assert db.execute("SELECT n_people FROM ng_study").fetchone()[0] == 156


def test_wrong_tissue_rejects_source_and_preserves_existing_records(db, tmp_path):
    raw = compress([crispr_row()])
    with db:
        import_screen(db, snapshot(tmp_path, raw), raw, "crispr")
    args = list(evo_sources(tmp_path))
    args[1] = args[1].replace(b"Primary Monocyte", b"whole blood")
    args[0] = snapshot(tmp_path, args[1], "atlas_wrong", ".csv")
    with pytest.raises(ValueError, match="cohort/tissue/relation"), db:
        import_atlas_evo(db, *args)
    assert db.execute("SELECT count(*) FROM ng_evidence").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM ng_study").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM ng_release").fetchone()[0] == 1


def test_numeric_boundaries_and_missing_values_are_not_zero():
    assert source_number("NA") == (None, "missing")
    assert source_number("0") == (0., "=")
    assert source_number("<2.2e-16") == (2.2e-16, "<")
    with pytest.raises(ValueError):
        source_number("inf")


def test_metadata_registry_is_not_a_regulatory_link(db, tmp_path):
    src = snapshot(tmp_path, b"registry", "meta", ".html")
    with db:
        release(db, src, "SCREEN-registry", "v1", "metadata only", "metadata_only")
    assert db.execute("SELECT count(*) FROM ng_evidence").fetchone()[0] == 0


def test_source_hash_mismatch_rejects_before_release_or_evidence_update(db, tmp_path):
    raw = compress([crispr_row()])
    src = snapshot(tmp_path, raw)
    src["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="declared hash"), db:
        import_screen(db, src, raw, "crispr")
    assert db.execute("SELECT count(*) FROM ng_release").fetchone()[0] == 0


class FakeResponse:
    def __init__(self, raw, length=None, location=None):
        self.raw = raw
        self.is_redirect = bool(location)
        self.headers = {"Content-Length": str(length if length is not None else len(raw))}
        if location:
            self.headers["Location"] = location
        self.closed = False

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def raise_for_status(self):
        pass

    def iter_content(self, size):
        yield self.raw


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        return self.response


@pytest.mark.parametrize("advertised", [1000, 0])
def test_download_cap_covers_content_length_and_stream_bytes(tmp_path, advertised):
    sources = Snapshots(tmp_path)
    sources.client = FakeClient(FakeResponse(b"x" * 1000, length=advertised))
    with pytest.raises(ValueError, match="budget"):
        sources.fetch("https://downloads.wenglab.org/small", "test", ".txt", 10)
    assert not list(tmp_path.glob("raw/*")) and sources.records == {}


def test_redirect_does_not_request_unapproved_or_gated_target(tmp_path):
    sources = Snapshots(tmp_path)
    client = FakeClient(FakeResponse(b"", location="https://unapproved.example/private"))
    sources.client = client
    with pytest.raises(ValueError, match="redirect"):
        sources.fetch("https://downloads.wenglab.org/source", "test", ".txt")
    assert len(client.calls) == 1 and client.response.closed


def test_frozen_cache_is_idempotent_and_tamper_detection_fails_closed(tmp_path):
    sources = Snapshots(tmp_path)
    client = FakeClient(FakeResponse(b"real public bytes"))
    sources.client = client
    url = "https://downloads.wenglab.org/source"
    src, raw = sources.fetch(url, "test", ".txt")
    assert sources.fetch(url, "test", ".txt") == (src, raw)
    assert len(client.calls) == 1
    Path(src["local_path"]).write_bytes(b"changed bytes")
    with pytest.raises(ValueError, match="hash mismatch"):
        sources.fetch(url, "test", ".txt")
    assert len(client.calls) == 1
