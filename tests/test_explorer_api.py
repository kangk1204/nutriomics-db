"""Explorer contract and claim-boundary checks against a small independent release."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nutrigenomics.explorer import attach_explorer

RELEASE = "fixture-release-20261003"
PRIVATE = "/home/research/private-do-not-export/source.tsv"
SECRET = "restricted-assay-payload"
ATTACK = "</script><script>window.harmlessFixtureMarker=1</script>"


def test_cached_candidate_eligibility_keeps_filters_and_invalidates_on_source_change(explorer):
    with sqlite3.connect(explorer["db"]) as db:
        db.execute(
            "INSERT INTO edge SELECT 'extracted-only-gene',subject,'NCBIGene:99',relation,tier,"
            "review_status,taxon,context_json,source_ids_json,pmids_json,domains_json "
            "FROM edge WHERE id='extracted'"
        )
    client = explorer["client"]
    query = {"kind": "gene", "q": "NO_RESULT"}
    assert client.get("/api/v1/explorer/entities", params=query).json()["total"] == 0
    assert (
        client.get("/api/v1/explorer/entities", params={**query, "include_extracted": True}).json()[
            "total"
        ]
        == 1
    )
    assert (
        client.get("/api/v1/explorer/entities", params={**query, "tier": "extracted"}).json()[
            "total"
        ]
        == 1
    )
    # A different query must not borrow a prior query's selected IDs.
    assert (
        client.get("/api/v1/explorer/entities", params={"kind": "gene", "q": "ENTPD1"}).json()[
            "total"
        ]
        == 1
    )
    before = explorer["db"].stat()
    with sqlite3.connect(explorer["db"]) as db:
        db.execute("UPDATE edge SET tier='curated' WHERE id='extracted-only-gene'")
    after = explorer["db"].stat()
    os.utime(
        explorer["db"],
        ns=(after.st_atime_ns, max(after.st_mtime_ns, before.st_mtime_ns + 1_000_000)),
    )
    assert client.get("/api/v1/explorer/entities", params=query).json()["total"] == 1


@pytest.fixture
def explorer(tmp_path: Path, monkeypatch):
    projection = tmp_path / "explorer.sqlite"
    with sqlite3.connect(projection) as db:
        db.executescript("""
            CREATE TABLE meta(key TEXT PRIMARY KEY,value_json TEXT NOT NULL);
            CREATE TABLE source(id TEXT PRIMARY KEY,metadata_json TEXT NOT NULL);
            CREATE TABLE node(id TEXT PRIMARY KEY,kind TEXT,label TEXT,label_en TEXT,
                aliases_json TEXT,metadata_json TEXT,domains_json TEXT,degree INTEGER);
            CREATE TABLE edge(id TEXT PRIMARY KEY,subject TEXT,object TEXT,relation TEXT,
                tier TEXT,review_status TEXT,taxon TEXT,context_json TEXT,
                source_ids_json TEXT,pmids_json TEXT,domains_json TEXT);
            CREATE TABLE measurement(id TEXT PRIMARY KEY,food TEXT,component TEXT,
                amount REAL,unit TEXT,basis TEXT,status TEXT,source_id TEXT,raw_json TEXT);
            CREATE TABLE node_domain(node_id TEXT,domain TEXT);
            CREATE TABLE edge_domain(edge_id TEXT,domain TEXT);
            CREATE INDEX edge_subject_idx ON edge(subject);
            CREATE INDEX edge_object_idx ON edge(object);
            CREATE INDEX measurement_food_idx ON measurement(food);
        """)
        jd = lambda value: json.dumps(value, ensure_ascii=False)
        db.executemany(
            "INSERT INTO meta VALUES (?,?)",
            [
                ("release_id", jd(RELEASE)),
                ("created_at", jd("2026-10-03T00:00:00Z")),
            ],
        )
        for identifier, permission in [("open", True), ("closed", False), ("unknown", None)]:
            metadata = {
                "source": identifier,
                "source_url": "https://example.org/" + identifier,
                "version": "fixture-v1",
                "sha256": "a" * 64,
                "local_path": PRIVATE,
            }
            if permission is not None:
                metadata["export_allowed"] = permission
            db.execute("INSERT INTO source VALUES (?,?)", (identifier, jd(metadata)))
        nodes = [
            ("food:rice", "food", "쌀밥", ["밥", "Rice, cooked"], {"core_food_id": 1}),
            ("food:fruit", "food", "=Harmless, fruit", ["과일"], {}),
            ("diet:DASH", "diet", "DASH diet", ["대시 식단"], {}),
            ("food:percent", "food", "100% cocoa", [], {}),
            ("food:underscore", "food", "Food_A", [], {}),
            ("food:gap", "food", "연결 없는 음식", [], {}),
            ("food:script", "food", ATTACK, [], {}),
            ("nutrient:fat", "nutrient", "총지방", [], {"entity_type": "component_group"}),
            ("MESH:C001", "chemical", "Fixture compound", ["검증 성분"], {}),
            ("NCBIGene:41", "gene", "ENTPD1", [], {"verified_feature_ids": ["ENSG_FIXTURE"]}),
            ("NCBIGene:99", "gene", "NO_RESULT", [], {}),
            ("MESH:D006973", "disease", "Hypertension", ["고혈압"], {}),
            ("outcome:LDL", "outcome", "LDL-C", [], {}),
            ("pathway:R1", "pathway", "Lipid pathway", [], {}),
        ]
        for number, (identifier, kind, label, aliases, metadata) in enumerate(nodes):
            metadata = {
                **metadata,
                "source_ids": ["open"],
                "source_file": PRIVATE,
                "nested": {"local_path": PRIVATE},
            }
            domains = ["lipids"] if identifier == "outcome:LDL" else ["blood_pressure"]
            db.execute(
                "INSERT INTO node VALUES (?,?,?,?,?,?,?,?)",
                (identifier, kind, label, label, jd(aliases), jd(metadata), jd(domains), number),
            )
            for domain in domains:
                db.execute("INSERT INTO node_domain VALUES (?,?)", (identifier, domain))
        edges = [
            (
                "composition",
                "food:rice",
                "nutrient:fat",
                "contains_nutrient",
                "observed_composition",
                ["open"],
                None,
            ),
            (
                "mapping",
                "nutrient:fat",
                "MESH:C001",
                "candidate_name_correspondence",
                "candidate",
                ["open"],
                None,
            ),
            (
                "gene",
                "MESH:C001",
                "NCBIGene:41",
                "chemical_gene_influence",
                "curated",
                ["open"],
                "9606",
            ),
            (
                "disease",
                "NCBIGene:41",
                "MESH:D006973",
                "gene_disease_marker_mechanism",
                "curated",
                ["open"],
                None,
            ),
            ("cycle", "NCBIGene:41", "MESH:C001", "association", "curated", ["open"], None),
            (
                "extracted",
                "MESH:C001",
                "MESH:D006973",
                "candidate_relationship",
                "extracted",
                ["open"],
                None,
            ),
            ("restricted", "food:rice", "MESH:D006973", SECRET, "curated", ["closed"], None),
            ("mixed", "food:rice", "outcome:LDL", SECRET, "curated", ["open", "unknown"], None),
        ]
        for identifier, subject, target, relation, tier, source_ids, taxon in edges:
            domain = "lipids" if identifier == "mixed" else "blood_pressure"
            context = {
                "source_location": "Table 2",
                "dose": "800 g",
                "tissue": None,
                "raw_file": PRIVATE,
                "nested": {"download_path": PRIVATE},
                "payload": SECRET if identifier in {"restricted", "mixed"} else "source-observed",
            }
            db.execute(
                "INSERT INTO edge VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    subject,
                    target,
                    relation,
                    tier,
                    "candidate" if tier == "extracted" else "source_recorded",
                    taxon,
                    jd(context),
                    jd(source_ids),
                    jd(["123456", "123456"]),
                    jd([domain]),
                ),
            )
            db.execute("INSERT INTO edge_domain VALUES (?,?)", (identifier, domain))
        db.executemany(
            "INSERT INTO measurement VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (
                    "m-open",
                    "food:rice",
                    "nutrient:fat",
                    2.5,
                    "g",
                    "edible_portion_100g",
                    "measured",
                    "open",
                    jd({"file": PRIVATE}),
                ),
                (
                    "m-closed",
                    "food:rice",
                    "nutrient:fat",
                    98765,
                    "g",
                    "edible_portion_100g",
                    "measured",
                    "closed",
                    jd({"secret": SECRET}),
                ),
                (
                    "m-zero",
                    "food:fruit",
                    "nutrient:fat",
                    0.0,
                    "g",
                    "edible_portion_100g",
                    "measured_zero",
                    "open",
                    "{}",
                ),
                (
                    "m-missing",
                    "food:fruit",
                    "MESH:C001",
                    None,
                    "mg",
                    "edible_portion_100g",
                    "missing",
                    "open",
                    "{}",
                ),
            ],
        )
    core = tmp_path / "core.sqlite"
    with sqlite3.connect(core) as db:
        db.executescript("""
            CREATE TABLE study_effect(id INTEGER PRIMARY KEY,series_accession TEXT,feature_type TEXT,feature_id TEXT,
                feature_label TEXT,contrast TEXT,tissue TEXT,timepoint TEXT,intervention TEXT,
                dose TEXT,duration TEXT,effect_estimate REAL,standard_error REAL,p_value REAL,
                q_value REAL,effect_unit TEXT,status TEXT,n_people INTEGER,n_samples INTEGER,
                analysis_version TEXT,metadata_json TEXT);
            CREATE INDEX feature_lookup ON study_effect(feature_id,feature_type);
        """)
        for feature_type, feature_id, estimate, q_value, version in [
            ("gene_expression", "ENTPD1", -0.25, 0.72, "v1"),
            ("gene_expression", "ENSG_FIXTURE", 0.14, 0.34, "v2"),
            ("cpg", "ENTPD1", 0.91, 0.00001, "collision"),
            ("pathway", "ENTPD1", 1.2, 0.001, "collision"),
        ]:
            metadata = {"export_allowed": True, "source_file": PRIVATE, "nested": {"path": PRIVATE}}
            db.execute(
                "INSERT INTO study_effect VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "GSE_FIXTURE",
                    feature_type,
                    feature_id,
                    "ENTPD1",
                    "6h minus baseline",
                    "whole blood",
                    "6h",
                    "mixed meal",
                    "800 g",
                    "acute",
                    estimate,
                    0.12,
                    0.41,
                    q_value,
                    "log2FC",
                    "measured",
                    9,
                    72,
                    version,
                    jd(metadata),
                ),
            )
    assets = tmp_path / "dist"
    assets.mkdir()
    (assets / "index.html").write_text(
        "<!doctype html><title>Explorer fixture</title>", encoding="utf-8"
    )
    (assets / "offline-runtime.js").write_text(
        "window.fixtureRuntimeLoaded=true;", encoding="utf-8"
    )
    (assets / "offline-runtime.css").write_text("body{color:#123;}", encoding="utf-8")
    monkeypatch.setenv("NUTRIGENOMICS_EXPLORER_DATABASE", str(projection))
    monkeypatch.setenv("NUTRIGENOMICS_EXPLORER_CORE_DATABASE", str(core))
    monkeypatch.setenv("NUTRIGENOMICS_EXPLORER_DIST", str(assets))
    application = FastAPI()
    attach_explorer(application)

    @application.get("/api/v1/legacy-health")
    def legacy_health():
        return {"status": "ok"}

    with TestClient(application) as client:
        yield {"client": client, "db": projection, "core": core, "assets": assets}


def export(client, format="json", **overrides):
    payload = {
        "format": format,
        "release_id": RELEASE,
        "node_ids": ["food:rice", "nutrient:fat", "MESH:D006973", "outcome:LDL"],
        "edge_ids": ["composition", "restricted", "mixed"],
        "positions": {"food:rice": {"x": 100, "y": 200}},
        "filters": {"domain": "all"},
    }
    payload.update(overrides)
    return client.post("/api/v1/explorer/export", json=payload)

def test_restricted_effect_previews_are_not_reported_as_original_absence(explorer):
    with sqlite3.connect(explorer['core']) as db:
        db.execute("UPDATE study_effect SET metadata_json=json_set(metadata_json,'$.export_allowed',json('false')) WHERE feature_type='gene_expression'")
    original=explorer['client'].get('/api/v1/explorer/nodes/NCBIGene:41').json()
    assert len(original['effects'])==2
    result=export(explorer['client'],node_ids=['NCBIGene:41'],edge_ids=[]).json()
    detail=result['node_details']['NCBIGene:41']
    assert detail['effects']==[] and detail['withheld_effect_previews']==2
    assert any('결과 부재를 뜻하지 않습니다' in text for text in detail['gaps'])


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("food", {"food", "diet"}),
        ("component", {"nutrient", "chemical"}),
        ("health", {"disease", "outcome"}),
        ("gene", {"gene"}),
        ("pathway", {"pathway"}),
    ],
)
def test_kind_tabs_include_semantic_groups(explorer, kind, expected):
    response = explorer["client"].get("/api/v1/explorer/entities", params={"kind": kind})
    assert response.status_code == 200
    assert {n["kind"] for n in response.json()["items"]} == expected


@pytest.mark.parametrize("query,expected", [("고혈압", "MESH:D006973"), ("검증 성분", "MESH:C001")])
def test_alias_search_preserves_korean_identifiers(explorer, query, expected):
    kind = "health" if expected.startswith("MESH:D") else "component"
    response = explorer["client"].get(
        "/api/v1/explorer/entities", params={"kind": kind, "q": query}
    )
    assert [r["id"] for r in response.json()["items"]] == [expected]


@pytest.mark.parametrize("query,expected", [("%", "food:percent"), ("_", "food:underscore")])
def test_percent_and_underscore_are_literal_search(explorer, query, expected):
    body = explorer["client"].get("/api/v1/explorer/entities", params={"q": query}).json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == expected


def test_pagination_and_sort_are_stable(explorer):
    client = explorer["client"]
    full = client.get(
        "/api/v1/explorer/entities", params={"sort": "degree", "order": "desc"}
    ).json()
    pages = [
        client.get(
            "/api/v1/explorer/entities",
            params={"sort": "degree", "order": "desc", "page_size": 2, "page": page},
        ).json()
        for page in range(1, 5)
    ]
    assert [row["id"] for page in pages for row in page["items"]] == [
        row["id"] for row in full["items"]
    ]
    assert all(page["total"] == full["total"] for page in pages)
    assert (
        client.get("/api/v1/explorer/entities", params={"page": 100, "page_size": 2}).json()[
            "items"
        ]
        == []
    )


def test_domain_tier_and_candidate_filter(explorer):
    client = explorer["client"]
    lipids = client.get(
        "/api/v1/explorer/entities", params={"kind": "health", "domain": "lipids"}
    ).json()
    assert [row["id"] for row in lipids["items"]] == ["outcome:LDL"]
    observed = client.get(
        "/api/v1/explorer/entities", params={"kind": "component", "tier": "observed"}
    ).json()
    assert [row["id"] for row in observed["items"]] == ["nutrient:fat"]
    default = client.get("/api/v1/explorer/graph", params={"seed_id": "MESH:C001"}).json()
    assert "extracted" not in {e["id"] for e in default["edges"]}
    candidates = client.get(
        "/api/v1/explorer/graph", params={"seed_id": "MESH:C001", "tier": "extracted"}
    ).json()
    assert {e["id"] for e in candidates["edges"]} == {"extracted"}


def test_meta_distinguishes_graph_records_from_prediction(explorer):
    meta = explorer["client"].get("/api/v1/explorer/meta").json()
    assert meta["release_id"] == RELEASE
    assert meta["prediction_available"] is False
    assert meta["counts"]["nodes"] == 14
    assert meta["counts"]["edges"] == 8
    assert "n_people" not in meta["counts"]
    assert "개인의 건강" in meta["note"]


def test_unavailable_release_returns_503_without_disabling_legacy(explorer, monkeypatch, tmp_path):
    monkeypatch.setenv("NUTRIGENOMICS_EXPLORER_DATABASE", str(tmp_path / "not-created.sqlite"))
    client = explorer["client"]
    assert client.get("/api/v1/explorer/meta").status_code == 503
    assert client.get("/api/v1/legacy-health").json() == {"status": "ok"}
    assert not (tmp_path / "not-created.sqlite").exists()


def test_online_details_keep_provenance_but_no_local_paths(explorer):
    client = explorer["client"]
    for endpoint in ("nodes/food:rice", "edges/gene", "graph?seed_id=food:rice"):
        response = client.get("/api/v1/explorer/" + endpoint)
        assert response.status_code == 200
        assert PRIVATE not in response.text
    provenance = client.get("/api/v1/explorer/edges/gene").json()["provenance"]
    assert provenance[0]["url"] == "https://example.org/open"
    assert provenance[0]["version"] == "fixture-v1"
    assert provenance[0]["sha256"] == "a" * 64


@pytest.mark.parametrize(
    "url",
    [
        "file:///home/research/private-do-not-export/source.tsv",
        "javascript:window.harmlessFixtureMarker=1",
        "data:text/html,fixture",
    ],
)
def test_provenance_links_only_allow_http_schemes(explorer, url):
    with sqlite3.connect(explorer["db"]) as db:
        metadata = json.loads(
            db.execute("SELECT metadata_json FROM source WHERE id='open'").fetchone()[0]
        )
        metadata["source_url"] = url
        db.execute("UPDATE source SET metadata_json=? WHERE id='open'", (json.dumps(metadata),))
    response = explorer["client"].get("/api/v1/explorer/edges/gene")
    assert response.status_code == 200
    assert response.json()["provenance"][0]["url"] is None
    assert url not in response.text


def test_explicit_export_denial_overrides_other_verified_flags(explorer):
    with sqlite3.connect(explorer["db"]) as db:
        metadata = json.loads(
            db.execute("SELECT metadata_json FROM source WHERE id='closed'").fetchone()[0]
        )
        metadata.update(
            {"export_allowed": False, "redistribution_verified": True, "rights_status": "verified"}
        )
        db.execute("UPDATE source SET metadata_json=? WHERE id='closed'", (json.dumps(metadata),))
    result = explorer["client"].get("/api/v1/explorer/edges/restricted").json()
    assert result["export_allowed"] is False
    response = export(explorer["client"])
    assert SECRET not in response.text


def test_gene_effects_preserve_non_significance_people_and_version(explorer):
    result = explorer["client"].get("/api/v1/explorer/nodes/NCBIGene:41").json()
    effects = result["effects"]
    assert {e["analysis_version"] for e in effects} == {"v1", "v2"}
    assert {e["feature_type"] for e in effects} == {"gene_expression"}
    assert effects[0]["effect_estimate"] == -0.25
    assert effects[0]["q_value"] == 0.72
    assert effects[0]["n_people"] == 9
    assert effects[0]["n_samples"] == 72
    assert "개인 건강" in effects[0]["interpretation"]
    assert PRIVATE not in json.dumps(result)


def test_absent_gene_effect_is_missing_not_a_zero_prediction(explorer):
    result = explorer["client"].get("/api/v1/explorer/nodes/NCBIGene:99").json()
    assert result["effects"] == []
    assert any("오믹스" in gap for gap in result["gaps"])


@pytest.mark.parametrize("index_column", [None, "feature_id", "feature_type"])
def test_gene_preview_pins_only_verified_selective_index(explorer, monkeypatch, index_column):
    from nutrigenomics.explorer import _effects
    from nutrigenomics.explorer_performance import ProfileCursor

    node = {"id": "NCBIGene:41", "kind": "gene", "label": "ENTPD1",
            "metadata": {"verified_feature_ids": ["ENSG_FIXTURE"]}}
    expected = _effects(node)
    with sqlite3.connect(explorer["core"]) as db:
        db.execute("CREATE INDEX broad_type ON study_effect(feature_type)")
        if index_column:
            db.execute("CREATE INDEX ix_study_effect_feature_id ON study_effect(" + index_column + ")")
    captured = []
    original = ProfileCursor.execute

    def capture(cursor, sql, parameters=()):
        if "SELECT series_accession,feature_type,feature_id" in sql:
            captured.append((sql, parameters))
        return original(cursor, sql, parameters)

    monkeypatch.setattr(ProfileCursor, "execute", capture)
    assert _effects(node) == expected
    sql, args = captured[-1]
    assert ("INDEXED BY ix_study_effect_feature_id" in sql) == (index_column == "feature_id")
    if index_column == "feature_id":
        with sqlite3.connect(explorer["core"]) as db:
            plan = str(db.execute("EXPLAIN QUERY PLAN " + sql, args).fetchall())
        assert "ix_study_effect_feature_id (feature_id=?)" in plan


def frozen_cpg_reference(explorer, export_allowed=False):
    """Create one reference to an actual frozen core row, with five-key identity."""
    with sqlite3.connect(explorer["core"]) as core:
        core.row_factory = sqlite3.Row
        cursor = core.execute(
            "INSERT INTO study_effect VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "GSE_CPG_FIXTURE",
                "cpg",
                "cg_fixture",
                "cg_fixture",
                "18m minus baseline",
                "whole blood",
                "18 months",
                "multicomponent diet",
                "unknown",
                "18 months",
                0.013,
                0.007,
                0.07,
                0.8,
                "delta_beta",
                "measured",
                20,
                40,
                "v-cpg-fixture",
                json.dumps(
                    {
                        "export_allowed": export_allowed,
                        "annotation_gene": "ENTPD1",
                        "source_file": PRIVATE,
                    }
                ),
            ),
        )
        row = dict(
            core.execute("SELECT * FROM study_effect WHERE id=?", (cursor.lastrowid,)).fetchone()
        )
    digest = hashlib.sha256(
        json.dumps(
            row, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
        ).encode()
    ).hexdigest()
    with sqlite3.connect(explorer["db"]) as db:
        db.execute("""CREATE TABLE omics_reference(node_id TEXT,effect_id INTEGER,
                   series_accession TEXT,feature_type TEXT,feature_id TEXT,contrast TEXT,
                   analysis_version TEXT,relation TEXT,context_json TEXT)""")
        db.execute(
            "INSERT INTO omics_reference VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "NCBIGene:41",
                row["id"],
                row["series_accession"],
                row["feature_type"],
                row["feature_id"],
                row["contrast"],
                row["analysis_version"],
                "cpg_platform_gene_annotation",
                json.dumps(
                    {
                        "effect_record_sha256": digest,
                        "probe_annotation": "fixture-platform",
                        "local_path": PRIVATE,
                    }
                ),
            ),
        )
    return row["id"], digest


def test_exact_frozen_cpg_annotation_stays_separate_from_expression_and_regulation(explorer):
    frozen_cpg_reference(explorer)
    result = explorer["client"].get("/api/v1/explorer/nodes/NCBIGene:41").json()
    assert {effect["feature_type"] for effect in result["effects"]} == {"gene_expression"}
    annotation = result["methylation_annotations"]
    assert len(annotation) == 1
    assert result["total_methylation_annotations"] == 1
    assert annotation[0]["feature_id"] == "cg_fixture"
    assert annotation[0]["effect_estimate"] == 0.013
    assert annotation[0]["q_value"] == 0.8
    assert annotation[0]["n_people"] == 20
    assert annotation[0]["n_samples"] == 40
    assert annotation[0]["gene_mapping_relation"] == "cpg_platform_gene_annotation"
    assert "발현 조절" in annotation[0]["gene_mapping_note"]
    assert "아닙니다" in annotation[0]["gene_mapping_note"]
    assert PRIVATE not in json.dumps(result)


def test_missing_reference_table_remains_backward_compatible(explorer):
    result = explorer["client"].get("/api/v1/explorer/nodes/NCBIGene:41").json()
    assert result["methylation_annotations"] == []
    assert result["total_methylation_annotations"] == 0
    assert len(result["effects"]) == 2


def test_mutated_core_cpg_row_fails_frozen_hash_closed(explorer):
    effect_id, _ = frozen_cpg_reference(explorer)
    with sqlite3.connect(explorer["core"]) as db:
        db.execute("UPDATE study_effect SET effect_estimate=0.5 WHERE id=?", (effect_id,))
    result = explorer["client"].get("/api/v1/explorer/nodes/NCBIGene:41").json()
    assert result["methylation_annotations"] == []
    assert result["total_methylation_annotations"] == 1
    assert len(result["effects"]) == 2


@pytest.mark.parametrize(
    "field,replacement",
    [
        ("series_accession", "GSE_OTHER"),
        ("feature_type", "dmr"),
        ("feature_id", "cg_wrong"),
        ("contrast", "wrong contrast"),
        ("analysis_version", "wrong version"),
    ],
)
def test_cpg_reference_identity_mismatch_is_rejected_even_with_right_hash(
    explorer, field, replacement
):
    frozen_cpg_reference(explorer)
    with sqlite3.connect(explorer["db"]) as db:
        db.execute("UPDATE omics_reference SET " + field + "=?", (replacement,))
    result = explorer["client"].get("/api/v1/explorer/nodes/NCBIGene:41").json()
    assert result["methylation_annotations"] == []
    assert result["total_methylation_annotations"] == 1


def test_missing_cpg_hash_is_not_treated_as_a_verified_reference(explorer):
    frozen_cpg_reference(explorer)
    with sqlite3.connect(explorer["db"]) as db:
        db.execute("UPDATE omics_reference SET context_json='{}'")
    result = explorer["client"].get("/api/v1/explorer/nodes/NCBIGene:41").json()
    assert result["methylation_annotations"] == []


@pytest.mark.parametrize("format", ["json", "html"])
def test_unpermitted_dnam_annotation_is_withheld_from_export(explorer, format):
    frozen_cpg_reference(explorer, export_allowed=False)
    response = export(explorer["client"], format=format, node_ids=["NCBIGene:41"], edge_ids=[])
    assert response.status_code == 200
    assert "cg_fixture" not in response.text
    if format == "json":
        assert response.json()["node_details"]["NCBIGene:41"]["methylation_annotations"] == []


def test_permitted_dnam_annotation_exports_with_its_nonregulatory_note(explorer):
    frozen_cpg_reference(explorer, export_allowed=True)
    snapshot = export(explorer["client"], node_ids=["NCBIGene:41"], edge_ids=[]).json()
    annotations = snapshot["node_details"]["NCBIGene:41"]["methylation_annotations"]
    assert annotations[0]["feature_id"] == "cg_fixture"
    assert "인과성" in annotations[0]["gene_mapping_note"]
    assert PRIVATE not in json.dumps(snapshot)


def test_raw_zero_and_missing_composition_remain_distinct(explorer):
    result = explorer["client"].get("/api/v1/explorer/nodes/food:fruit").json()
    values = {m["id"]: m for m in result["measurements"]}
    assert values["m-zero"]["amount"] == 0
    assert values["m-zero"]["status"] == "measured_zero"
    assert values["m-missing"]["amount"] is None
    assert values["m-missing"]["status"] == "missing"
    assert all("raw_json" not in m for m in values.values())


def test_paths_are_composed_candidates_cycles_do_not_fabricate_edges(explorer):
    graph = (
        explorer["client"]
        .get("/api/v1/explorer/graph", params={"seed_id": "food:rice", "depth": 5})
        .json()
    )
    assert graph["paths"]
    actual = {e["id"] for e in graph["edges"]}
    assert actual <= {"composition", "mapping", "gene", "disease", "cycle", "restricted", "mixed"}
    paths = [p for p in graph["paths"] if "mapping" in p["edge_ids"]]
    assert paths
    assert paths[0]["weakest_link"] == "mapping"
    assert "후보 경로" in paths[0]["interpretation"]
    assert "검증되지 않음" in paths[0]["interpretation"]
    assert all(len(p["node_ids"]) == len(set(p["node_ids"])) for p in graph["paths"])
    assert all(set(p["edge_ids"]) <= actual for p in graph["paths"])


def test_graph_is_bounded_and_endpoint_closed(explorer):
    result = (
        explorer["client"]
        .get(
            "/api/v1/explorer/graph",
            params={"seed_id": "food:rice", "max_nodes": 3, "max_edges": 2},
        )
        .json()
    )
    ids = {n["id"] for n in result["nodes"]}
    assert len(ids) <= 3
    assert len(result["edges"]) <= 2
    assert all(e["subject"] in ids and e["object"] in ids for e in result["edges"])
    assert result["truncated"]
    assert result["omitted"]["count_type"] == "encountered_lower_bound"


def dense_composition_frontier(explorer):
    """A real source chain sorts after enough measurements to fill an 80-node view."""
    jd = lambda value: json.dumps(value, ensure_ascii=False)
    with sqlite3.connect(explorer["db"]) as db:
        db.execute("DELETE FROM edge_domain")
        db.execute("DELETE FROM edge")
        for number in range(93):
            identifier = f"nutrient:dense:{number:03}"
            db.execute(
                "INSERT INTO node VALUES (?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    "nutrient",
                    f"Measured component {number}",
                    None,
                    "[]",
                    "{}",
                    jd(["blood_pressure"]),
                    1,
                ),
            )
            db.execute(
                "INSERT INTO edge VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"dense:{number:03}",
                    "food:rice",
                    identifier,
                    "contains_nutrient",
                    "observed_composition",
                    "source_recorded",
                    None,
                    "{}",
                    jd(["open"]),
                    "[]",
                    jd(["blood_pressure"]),
                ),
            )
            db.execute(
                "INSERT INTO edge_domain VALUES (?,?)", (f"dense:{number:03}", "blood_pressure")
            )
        path = [
            (
                "z-real-composition",
                "food:rice",
                "nutrient:fat",
                "contains_nutrient",
                "observed_composition",
            ),
            (
                "z-real-candidate",
                "nutrient:fat",
                "MESH:C001",
                "candidate_name_correspondence",
                "candidate",
            ),
            ("z-real-gene", "MESH:C001", "NCBIGene:41", "chemical_gene_influence", "curated"),
            (
                "z-real-health",
                "NCBIGene:41",
                "MESH:D006973",
                "gene_disease_marker_mechanism",
                "curated",
            ),
        ]
        for identifier, subject, target, relation, tier in path:
            db.execute(
                "INSERT INTO edge VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    subject,
                    target,
                    relation,
                    tier,
                    "source_recorded",
                    None,
                    "{}",
                    jd(["open"]),
                    jd(["123456"]),
                    jd(["blood_pressure"]),
                ),
            )
            db.execute("INSERT INTO edge_domain VALUES (?,?)", (identifier, "blood_pressure"))
        db.execute(
            "INSERT INTO edge VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "shortcut-extracted",
                "food:rice",
                "outcome:LDL",
                "candidate_relationship",
                "extracted",
                "candidate",
                None,
                "{}",
                jd(["open"]),
                jd(["123456"]),
                jd(["lipids"]),
            ),
        )
        db.execute("INSERT INTO edge_domain VALUES (?,?)", ("shortcut-extracted", "lipids"))
    return {row[0] for row in path}


def test_dense_composition_keeps_real_candidate_health_path_visible_at_default_limit(explorer):
    required = dense_composition_frontier(explorer)
    result = (
        explorer["client"].get("/api/v1/explorer/graph", params={"seed_id": "food:rice"}).json()
    )
    edges = {edge["id"] for edge in result["edges"]}
    assert len(result["nodes"]) <= 80
    assert len(result["edges"]) <= 200
    assert result["truncated"]
    assert required <= edges
    assert "shortcut-extracted" not in edges
    path = next(p for p in result["paths"] if set(p["edge_ids"]) == required)
    assert path["weakest_link"] == "z-real-candidate"
    assert "검증되지 않음" in path["interpretation"]
    with sqlite3.connect(explorer["db"]) as db:
        recorded = {row[0] for row in db.execute("SELECT id FROM edge")}
    assert edges <= recorded


@pytest.mark.parametrize("max_nodes,max_edges", [(1, 1), (2, 1), (4, 3), (5, 2)])
def test_dense_path_reservation_obeys_tiny_graph_budgets(explorer, max_nodes, max_edges):
    dense_composition_frontier(explorer)
    result = (
        explorer["client"]
        .get(
            "/api/v1/explorer/graph",
            params={
                "seed_id": "food:rice",
                "max_nodes": max_nodes,
                "max_edges": max_edges,
            },
        )
        .json()
    )
    nodes = {node["id"] for node in result["nodes"]}
    assert len(nodes) <= max_nodes
    assert len(result["edges"]) <= max_edges
    assert all(edge["subject"] in nodes and edge["object"] in nodes for edge in result["edges"])
    assert result["paths"] == []


@pytest.mark.parametrize(
    "params,allowed",
    [
        ({"tier": "observed"}, {"observed_composition"}),
        ({"tier": "candidate"}, {"candidate"}),
        ({"tier": "curated"}, {"curated"}),
        ({"domain": "lipids", "include_extracted": True}, {"extracted"}),
    ],
)
def test_reserved_path_never_restores_filtered_out_edges(explorer, params, allowed):
    dense_composition_frontier(explorer)
    result = (
        explorer["client"]
        .get("/api/v1/explorer/graph", params={"seed_id": "food:rice", **params})
        .json()
    )
    assert {edge["tier"] for edge in result["edges"]} <= allowed
    if params.get("domain") == "lipids":
        assert {edge["id"] for edge in result["edges"]} == {"shortcut-extracted"}
    else:
        assert result["paths"] == []


@pytest.mark.parametrize(
    "amount,status,has_path",
    [
        (0.0, "measured_zero", False),
        (None, "missing", False),
        (None, "not_measured", False),
        (2.5, "measured", True),
    ],
)
def test_known_zero_or_missing_amount_is_visible_but_not_an_intake_health_path(
    explorer,
    amount,
    status,
    has_path,
):
    required = dense_composition_frontier(explorer)
    with sqlite3.connect(explorer["db"]) as db:
        db.execute("UPDATE measurement SET amount=?,status=? WHERE id='m-open'", (amount, status))
        db.execute(
            "UPDATE edge SET context_json=? WHERE id='z-real-composition'",
            (json.dumps({"measurement_id": "m-open"}),),
        )
    client = explorer["client"]
    result = client.get(
        "/api/v1/explorer/graph", params={"seed_id": "food:rice", "max_nodes": 250}
    ).json()
    composition = next(edge for edge in result["edges"] if edge["id"] == "z-real-composition")
    assert composition["context"]["measurement"] == {
        "amount": amount,
        "unit": "g",
        "basis": "edible_portion_100g",
        "status": status,
    }
    paths = [path for path in result["paths"] if set(path["edge_ids"]) == required]
    assert bool(paths) is has_path
    detail = client.get("/api/v1/explorer/edges/z-real-composition").json()
    assert detail["context"]["measurement"]["amount"] == amount
    assert detail["context"]["measurement"]["status"] == status
    snapshot = export(
        client,
        node_ids=["food:rice", "nutrient:fat", "MESH:C001", "NCBIGene:41", "MESH:D006973"],
        edge_ids=sorted(required),
    ).json()
    assert bool(snapshot["paths"]) is has_path


def test_empty_graph_displays_gap(explorer):
    result = explorer["client"].get("/api/v1/explorer/graph", params={"seed_id": "food:gap"}).json()
    assert result["edges"] == []
    assert result["paths"] == []
    assert any("연결 근거 없음" in gap for gap in result["gaps"])


def test_unknown_species_not_promoted_to_human(explorer):
    edge = explorer["client"].get("/api/v1/explorer/edges/disease").json()
    assert edge["taxon"] is None
    assert edge["context"]["tissue"] is None
    assert edge["tier"] == "curated"


@pytest.mark.parametrize("format", ["json", "csv", "html"])
def test_export_withholds_restricted_and_unverified_dependency_payloads(explorer, format):
    response = export(explorer["client"], format=format)
    assert response.status_code == 200
    assert SECRET not in response.text
    assert "98765" not in response.text
    assert PRIVATE not in response.text
    assert "restricted_source_reference" in response.text
    assert response.headers["x-nutriomics-release"] == RELEASE
    if format == "json":
        snapshot = response.json()
        assert snapshot["export_withheld_edges"] == 2
        assert {m["id"] for m in snapshot["node_details"]["food:rice"]["measurements"]} == {
            "m-open"
        }
        assert not any(p for p in snapshot["paths"] if "restricted" in p["edge_ids"])


def test_csv_quotes_commas_blocks_formula_and_includes_allowed_measurements(explorer):
    response = export(
        explorer["client"],
        format="csv",
        node_ids=["food:fruit", "food:rice", "nutrient:fat"],
        edge_ids=["composition"],
    )
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.text.lstrip("\ufeff"))))
    label = next(r["label"] for r in rows if r["id"] == "food:fruit")
    assert label == "'=Harmless, fruit"
    measurement = next(r for r in rows if r["id"] == "m-open")
    assert measurement["record_type"] == "measurement"
    assert float(measurement["amount"]) == 2.5
    assert measurement["unit"] == "g"
    assert measurement["basis"] == "edible_portion_100g"


def test_html_snapshot_safely_encodes_script_labels_and_has_no_runtime_url(explorer):
    response = export(explorer["client"], format="html", node_ids=["food:script"], edge_ids=[])
    assert response.status_code == 200
    assert ATTACK not in response.text
    assert "\\u003c/script>" in response.text
    assert "window.__NUTRIOMICS_SNAPSHOT__=" in response.text
    assert "window.fixtureRuntimeLoaded=true;" in response.text
    assert "<script src=" not in response.text
    assert "<link href=" not in response.text


def test_stale_export_release_is_rejected(explorer):
    assert export(explorer["client"], release_id="old-release").status_code == 409


def test_export_edge_requires_both_endpoints(explorer):
    response = export(explorer["client"], node_ids=["food:rice"], edge_ids=["composition"])
    assert response.status_code == 422


@pytest.mark.parametrize("number", ["NaN", "Infinity", "1e309"])
def test_nonfinite_coordinate_is_a_validation_error(explorer, number):
    # Include valid-JSON exponential overflow as well as Python JSON extensions.
    # The error response must not crash while serializing an invalid input value.
    payload = (
        '{"format":"json","release_id":"' + RELEASE + '",'
        '"node_ids":["food:rice"],"positions":{"food:rice":{"x":' + number + ',"y":0}}}'
    )
    response = explorer["client"].post(
        "/api/v1/explorer/export", content=payload, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "overrides",
    [
        {"node_ids": ["food:rice"] * 251},
        {"edge_ids": ["composition"] * 751},
        {"positions": {"food:rice": {"x": 1000001, "y": 0}}},
        {"positions": {"food:rice": {"x": 0, "y": 0, "secret": "unexpected"}}},
        {"filters": {"huge": "a" * 16001}},
        {"format": "pdf"},
    ],
)
def test_export_rejects_invalid_positions_and_oversized_selection(explorer, overrides):
    assert export(explorer["client"], **overrides).status_code == 422


@pytest.mark.parametrize(
    "params",
    [
        {"max_nodes": 251},
        {"max_edges": 751},
        {"depth": 6},
        {"domain": "unknown"},
        {"tier": "proven_efficacy"},
    ],
)
def test_graph_rejects_invalid_scope_and_limits(explorer, params):
    assert (
        explorer["client"]
        .get("/api/v1/explorer/graph", params={"seed_id": "food:rice", **params})
        .status_code
        == 422
    )


def test_static_asset_traversal_is_rejected(explorer, tmp_path):
    (tmp_path / "private.txt").write_text("private fixture", encoding="utf-8")
    client = explorer["client"]
    assert client.get("/explorer/").status_code == 200
    for path in (
        "/explorer/%2e%2e/private.txt",
        "/explorer/..%2fprivate.txt",
        "/explorer/%2e%2e%5cprivate.txt",
    ):
        assert client.get(path).status_code == 404


def test_api_does_not_mutate_projection_or_core(explorer):
    before = {key: explorer[key].read_bytes() for key in ("db", "core")}
    client = explorer["client"]
    client.get("/api/v1/explorer/graph", params={"seed_id": "food:rice"})
    client.get("/api/v1/explorer/nodes/NCBIGene:41")
    export(client)
    assert {key: explorer[key].read_bytes() for key in ("db", "core")} == before
