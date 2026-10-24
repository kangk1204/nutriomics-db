"""Stored API and immutable final-attempt contracts; no scientific claims."""
import json
import pytest
import test_api_v2 as fixtures
from nutri_literature.db import connect,dumps
from nutri_literature.registry_v4 import engine_rules_version
from nutri_literature.pubtator_v4 import align

workspace=fixtures.workspace
v2_workspace=fixtures.v2_workspace


def test_pubtator_exact_unicode_alignment():
    doc={"passages":[{"offset":10,"text":"α VDR","annotations":[{"text":"VDR","infons":{"type":"Gene","identifier":"7421"},
                            "locations":[{"offset":12,"length":3}]}]}]}
    result=align(doc,"Title\nα VDR")
    assert result[0]["start"]==8 and result[0]["normalized_id"]=="NCBIGene:7421"
    with pytest.raises(ValueError,match="uniquely"):
        align(doc,"α VDR\nα VDR")


def test_v4_api_reads_only_saved_results(v2_workspace,monkeypatch):
    client,settings=v2_workspace
    from nutri_literature import operations_v4
    monkeypatch.setattr(operations_v4,"components",lambda *args,**kwargs:pytest.fail("API must never load models"))
    artifact="f"*64
    path=settings.home/"engine-artifacts"/"heuristic-v4.json"
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(dumps({"artifact_identity":artifact,"dictionary_version":"synthetic-dictionary-v2"}))
    with connect(settings) as con:
        row=con.execute("SELECT * FROM extractions WHERE extraction_id='extraction-v2'").fetchone()
        result=json.loads(row["result_json"])
        result.update(engine_version="heuristic-v4",rules_version=engine_rules_version("heuristic-v4"))
        result["manifest"].update(artifact_identity=artifact,profile="local",oracle=False,
             candidate_provider={"available":True},clinical_models={"available":True},gene_dictionary={"records_sha256":"a"*64})
        con.execute("UPDATE extractions SET engine_version='heuristic-v4',result_json=? WHERE extraction_id='extraction-v2'",(dumps(result),))
        a=con.execute("SELECT payload FROM assertions WHERE assertion_id='assertion-v2'").fetchone()
        payload=json.loads(a[0]);payload["event"].update(assertion_state="observed",taxon_id=9606)
        con.execute("UPDATE assertions SET payload=? WHERE assertion_id='assertion-v2'",(dumps(payload),))
    assert client.get("/api/v1/papers?engine=heuristic-v4").status_code==200
    result=client.get("/api/v1/events?engine=heuristic-v4").json()
    assert any(e["event"].get("assertion_state")=="observed" for e in result["items"])
    assert client.get("/api/v1/experiments?engine=heuristic-v4").json()["engine"]=="heuristic-v4"
    assert client.get("/api/v1/experiments/diagnostics").status_code==200
    assert client.get("/healthz").json()["engine"]=="heuristic-v1"


def test_final_attempt_is_consumed_even_on_failure(tmp_path,monkeypatch):
    from nutri_literature import operations_v4,evaluation_v4,dictionaries
    monkeypatch.setattr(operations_v4,"components",lambda *a,**k:{})
    monkeypatch.setattr(operations_v4,"identity",lambda *a,**k:{"artifact_identity":"pinned"})
    monkeypatch.setattr(dictionaries,"load_dictionaries",lambda *a,**k:None)
    source=tmp_path/"inputs.json";source.write_text("{}")
    spec={"corpus":"nlmgene","inputs_path":str(source),"inputs_sha256":"wrong"}
    spec_path=tmp_path/"spec.json";spec_path.write_text(dumps(spec))
    freeze=tmp_path/"freeze.json";freeze.write_text(dumps({"schema":"v4-final-freeze","artifact":{"artifact_identity":"pinned"},"specs":[spec]}))
    with pytest.raises(ValueError,match="inputs changed"):
        evaluation_v4.final_evaluate(None,spec_path,freeze)
    with pytest.raises(FileExistsError):
        evaluation_v4.final_evaluate(None,spec_path,freeze)


def test_curated_bioc_surface_is_preserved_without_guessing_offsets():
    from nutri_literature.corpora_v4 import bioc_xml
    raw=b'<collection><document><id>1</id><passage><infon key="type">abstract</infon><offset>0</offset><text>AP-2&#223;</text><annotation><infon key="type">Gene</infon><infon key="NCBI Gene identifier">1</infon><location offset="0" length="5"/><text>AP-2b</text></annotation></passage></document></collection>'
    row=bioc_xml(raw,True)[0]
    assert row["entities"][0]["text"]==row["text"]=="AP-2ß"
    assert row["entities"][0]["published_annotation_text"]=="AP-2b"
    assert row["entities"][0]["source_surface_differs"]


def test_secondary_engine_dispatch_cannot_acquire_two_processing_slots(monkeypatch):
    from nutri_literature import pipeline,operations_v4
    monkeypatch.setattr(operations_v4,"processing_slot",lambda *args:pytest.fail("outer slot would deadlock"))
    monkeypatch.setattr(pipeline,"process_engine",lambda settings,engine,limit:{"engine":engine,"limit":limit})
    assert pipeline.process(None,engine="heuristic-v4",limit=7)=={"engine":"heuristic-v4","limit":7}
def test_study_count_keeps_unlinked_papers_unknown():
    from nutri_literature.reporting import _study_counts
    papers = [
        {"extraction_current": True, "extraction": {"result": {"study_families": [{"id": "NCT12345678"}]}}},
        {"extraction_current": True, "extraction": {"result": {"study_families": [{"id": "NCT12345678"}]}}},
        {"extraction_current": True, "extraction": {"result": {"study_families": []}}},
        {"extraction_current": False, "extraction": {"result": {"study_families": [{"id": "NCT87654321"}]}}},
    ]
    counts = _study_counts(papers)
    assert counts["identified_study_families"] == 1
    assert counts["papers_with_study_family"] == 2
    assert counts["papers_without_study_family"] == 2
    assert counts["study_count_status"] == "partial_explicit_NCT_only"

def test_seven_day_observation_resets_on_artifact_and_rejects_gaps(tmp_path, monkeypatch):
    from nutri_literature import operations_v4
    from nutri_literature.settings import Settings
    from nutri_literature.db import digest, dumps
    from datetime import datetime, timedelta, timezone
    settings = Settings(tmp_path, tmp_path)
    artifact = tmp_path / "engine-artifacts/heuristic-v4.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("new-version")
    current_sha = digest(artifact.read_bytes())
    end = datetime(2026, 10, 9, tzinfo=timezone.utc)
    monkeypatch.setattr(operations_v4, "now", lambda: end.isoformat())
    log = tmp_path / "experiments/v4/operation.jsonl"
    log.parent.mkdir(parents=True)
    def row(at, sha=current_sha):
        return {"observed_at": at.isoformat(), "artifact_sha256": sha, "processed_unique_papers": 1}
    log.write_text("\n".join(dumps(row(end-timedelta(days=i), "old-version")) for i in range(8)))
    assert operations_v4.operation_status(settings)["samples"] == 0
    log.write_text("\n".join(dumps(row(end-timedelta(days=i))) for i in range(8)))
    assert not operations_v4.operation_status(settings)["seven_days_observed"]
    log.write_text("\n".join(dumps(row(end-timedelta(minutes=i*15))) for i in range(7*24*4+1)))
    assert operations_v4.operation_status(settings)["seven_days_observed"]

def test_observation_does_not_count_previous_artifact_as_current(v2_workspace):
    from nutri_literature.operations_v4 import observe
    _, settings = v2_workspace
    artifact = settings.home / "engine-artifacts/heuristic-v4.json"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(dumps({"artifact_identity": "new-version"}))
    with connect(settings) as con:
        con.execute("UPDATE extractions SET engine_version='heuristic-v4',result_json=? WHERE extraction_id='extraction-v2'",
                    (dumps({"manifest": {"artifact_identity": "old-version"}}),))
    value = observe(settings)
    assert value["processed_unique_papers"] == value["active_papers"] == 0
    assert value["historical_processed_unique_papers"] == 1
    with connect(settings) as con:
        con.execute("UPDATE extractions SET result_json=? WHERE extraction_id='extraction-v2'",
                    (dumps({"manifest": {"artifact_identity": "new-version"}}),))
    assert observe(settings)["processed_unique_papers"] == 1

