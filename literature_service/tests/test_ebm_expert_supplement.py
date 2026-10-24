"""Synthetic protocol/adapter fixtures; no empirical benchmark claims."""

import io
import json
import tarfile
from pathlib import Path

import pytest

from nutri_literature import benchmarks
from nutri_literature.db import connect, dumps, initialize
from nutri_literature.settings import PROJECT, Settings
from scripts import prepare_ebm_expert_supplement as supplement


def _archive(path, conflict=False):
    papers = [*supplement.EXCLUSIONS, "900001", "900002"]
    members = {}
    for pmid in papers:
        members[f"ebm/documents/{pmid}.txt"] = b"Adults received vitamin D."
        members[f"ebm/documents/{pmid}.tokens"] = b"Adults received vitamin D ."
        for task in supplement.TASKS:
            # Paper 900002 has no intervention/outcome expert file, rather than
            # an all-zero annotation. Its crowd files deliberately still exist.
            if pmid != "900002" or task == "participants":
                members[f"ebm/annotations/aggregated/starting_spans/{task}/test/gold/{pmid}.AGGREGATED.ann"] = (
                    b"1 0 0 0 0" if pmid == "900002" else b"0 0 0 0 0"
                )
            members[f"ebm/annotations/aggregated/starting_spans/{task}/test/crowd/{pmid}.AGGREGATED.ann"] = b"9 9 9 9 9"
    with tarfile.open(path, "w") as archive:
        for name, raw in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            archive.addfile(info, io.BytesIO(raw))
        if conflict:
            name = "ebm/annotations/aggregated/starting_spans/participants/test/gold/900001.AGGREGATED.ann"
            info = tarfile.TarInfo(name)
            info.size = 1
            archive.addfile(info, io.BytesIO(b"1"))


@pytest.fixture
def case(tmp_path, monkeypatch):
    settings = Settings(tmp_path / "runtime", PROJECT / "config/topics.json")
    initialize(settings)
    official = tmp_path / "official.tar"
    _archive(official)
    source_sha = supplement._file_sha(official)
    monkeypatch.setattr(supplement, "OFFICIAL_ARCHIVE_SHA256", source_sha)
    monkeypatch.setattr(supplement, "EXPECTED_GOLD_UNION", 6)
    monkeypatch.setattr(supplement, "EXPECTED_SELECTED", 2)
    provenance = {engine: {"engine_version": engine, "rules_version": "fixture-rules",
                           "evaluator_source_hashes": benchmarks.evaluator_source_hashes()}
                  for engine in ["heuristic-v1", "heuristic-v2"]}
    monkeypatch.setattr(benchmarks, "_provenance", lambda settings, engine, crosswalk_path=None: (None, provenance[engine]))
    freezes = {}
    for engine in provenance:
        record = {"schema_version": "literature-public-freeze-v1", "id": "fixture-" + engine,
                  "frozen_at": "2026-10-03T00:00:00+00:00", "corpus": "ebmnlp",
                  "release_sha256": source_sha, "provenance": provenance[engine], "projection": benchmarks.PROJECTION}
        path = tmp_path / (engine + ".json")
        path.write_text(dumps(record), encoding="utf-8")
        benchmarks._receipt(settings, "public_benchmark_freeze", record["id"], record)
        freezes[engine] = path
    return settings, official, freezes, tmp_path / "supplement"


def _register(case):
    settings, source, freezes, output = case
    return supplement.register_protocol(settings, source, freezes["heuristic-v1"], freezes["heuristic-v2"], output)


def test_protocol_audited_before_labels_extracted_and_no_excluded_or_crowd_reads(case, monkeypatch):
    settings, _, _, _ = case
    original = tarfile.TarFile.extractfile
    label_reads = []

    def inspect_extract(archive, member):
        name = member if isinstance(member, str) else member.name
        if name.endswith(".ann"):
            protocol = json.loads((case[3] / "protocol.json").read_bytes())
            assert benchmarks.verify_receipt(settings, supplement.PROTOCOL_KIND, protocol)
            assert "/test/gold/" in name and "/test/crowd/" not in name
            assert not any("/" + pmid + "." in name for pmid in supplement.EXCLUSIONS)
            label_reads.append(name)
        return original(archive, member)

    monkeypatch.setattr(tarfile.TarFile, "extractfile", inspect_extract)
    protocol = _register(case)
    assert not label_reads
    derived = supplement.derive_archive(settings, protocol["protocol_path"])
    assert len(label_reads) == 4
    receipt = derived["receipt"]
    assert all(row["original_sha256"] == row["derived_sha256"] for row in receipt["member_hashes"])
    assert receipt["annotated_papers_by_task"] == {"participants": 2, "interventions": 1, "outcomes": 1}
    assert benchmarks.verify_receipt(settings, supplement.DERIVATION_KIND, receipt)


def test_frozen_prepare_preserves_missing_tasks_and_all_zero_negative_labels(case):
    settings, _, _, _ = case
    protocol = _register(case)
    prepared = supplement.prepare_supplement(settings, protocol["protocol_path"])
    gold = json.loads(Path(prepared["gold_path"]).read_bytes())
    manifest = json.loads(Path(prepared["manifest_path"]).read_bytes())
    assert prepared["documents"] == 2 and manifest["official_split"] and not manifest["complete_partition"]
    assert prepared["original_expert_test_covered"] == 2 and not prepared["original_expert_test_complete"]
    coverage, _ = benchmarks._paper_annotation_tasks(gold, manifest, prepared["gold_path"])
    assert coverage == {"900001": set(supplement.TASKS), "900002": {"participants"}}
    papers = {paper["pmid"]: paper for paper in gold["documents"]}
    assert papers["900001"]["annotated_spans"] == []
    assert papers["900002"]["annotated_spans"] == [{"start": 0, "end": 6, "text": "Adults", "label": "participants"}]
    prediction = {"entities": [], "events": [], "relations": [], "scopes": [],
                  "annotated_spans": [{"start": 0, "end": 6, "text": "Adults", "label": task} for task in supplement.TASKS]}
    negative = benchmarks._paper_counts(papers["900001"], prediction, {}, "end_to_end", annotated_tasks=coverage["900001"])
    partial = benchmarks._paper_counts(papers["900002"], prediction, {}, "end_to_end", annotated_tasks=coverage["900002"])
    assert negative["annotated_spans:outcomes"]["fp"] == 1
    assert partial["annotated_spans:participants"]["tp"] == 1
    assert "annotated_spans:outcomes" not in partial and "annotated_spans:interventions" not in partial
    assert list((settings.home / "public_benchmarks/test_attempts").glob("*.json")) == []


def test_preparation_is_idempotent_without_recopy_or_new_protocol_receipts(case, monkeypatch):
    settings, _, _, _ = case
    registered = _register(case)
    first = supplement.prepare_supplement(settings, registered["protocol_path"])
    monkeypatch.setattr(tarfile.TarFile, "extractfile", lambda *args: pytest.fail("Idempotent derive must not read label members"))
    second_registration = _register(case)
    second = supplement.derive_archive(settings, second_registration["protocol_path"])
    assert registered == second_registration
    assert first["derived_archive"] == second["derived_archive"]
    with connect(settings) as con:
        assert con.execute("SELECT count(*) FROM audit WHERE kind=?", (supplement.PROTOCOL_KIND,)).fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM audit WHERE kind=?", (supplement.DERIVATION_KIND,)).fetchone()[0] == 1


def test_full_prepare_reuses_identical_pinned_inputs_gold_and_manifest(case):
    registered = _register(case)
    first = supplement.prepare_supplement(case[0], registered["protocol_path"])
    pinned = {key: Path(first[key]).read_bytes() for key in ["inputs_path", "gold_path", "manifest_path"]}
    second = supplement.prepare_supplement(case[0], registered["protocol_path"])
    assert second["status"] == "prepared_existing"
    assert all(first[key] == second[key] and Path(second[key]).read_bytes() == value for key, value in pinned.items())
    assert list((case[0].home / "public_benchmarks/test_attempts").glob("*.json")) == []


def test_wrong_source_hash_rejected_before_label_extraction(case, monkeypatch):
    case[1].write_bytes(case[1].read_bytes() + b"altered")
    monkeypatch.setattr(tarfile.TarFile, "extractfile", lambda *args: pytest.fail("Labels must remain unread"))
    with pytest.raises(ValueError, match="SHA-256"):
        _register(case)


@pytest.mark.parametrize("mutation", ["protocol", "source", "helper", "derived"])
def test_altered_protocol_source_helper_or_derived_archive_fails_closed(case, monkeypatch, mutation):
    settings, source, _, output = case
    registered = _register(case)
    if mutation == "protocol":
        path = Path(registered["protocol_path"])
        record = json.loads(path.read_bytes())
        record["exclusions"] = {}
        path.write_text(dumps(record), encoding="utf-8")
    elif mutation == "source":
        source.write_bytes(source.read_bytes() + b"changed")
    elif mutation == "helper":
        monkeypatch.setattr(supplement, "_helper_sha", lambda: "invalid-helper")
    else:
        supplement.derive_archive(settings, registered["protocol_path"])
        archive = output / "expert-supplement.tar"
        archive.write_bytes(archive.read_bytes() + b"changed")
    monkeypatch.setattr(tarfile.TarFile, "extractfile", lambda *args: pytest.fail("Changed protocol must not read labels"))
    with pytest.raises(ValueError, match="changed|differs"):
        supplement.derive_archive(settings, registered["protocol_path"])


def test_conflicting_archive_headers_fail_before_label_values(case, monkeypatch):
    _archive(case[1], conflict=True)
    monkeypatch.setattr(supplement, "OFFICIAL_ARCHIVE_SHA256", supplement._file_sha(case[1]))
    monkeypatch.setattr(tarfile.TarFile, "extractfile", lambda *args: pytest.fail("Conflicting labels must remain unread"))
    with pytest.raises(ValueError, match="duplicate"):
        _register(case)


def test_wrong_frozen_receipt_or_identity_rejected_before_labels(case, monkeypatch):
    path = case[2]["heuristic-v2"]
    frozen = json.loads(path.read_bytes())
    frozen["provenance"]["rules_version"] = "altered"
    path.write_text(dumps(frozen), encoding="utf-8")
    monkeypatch.setattr(tarfile.TarFile, "extractfile", lambda *args: pytest.fail("Labels must remain unread"))
    with pytest.raises(ValueError, match="freeze"):
        _register(case)


def test_selected_paper_exposure_is_rejected_and_not_silently_excluded(case):
    settings = case[0]
    folder = settings.home / "public_benchmarks/other/fixture"
    folder.mkdir(parents=True)
    (folder / "inputs.json").write_text(dumps({"corpus": "other", "split": "test", "documents": [{"pmid": "900001"}]}))
    with pytest.raises(ValueError, match="already exposed"):
        _register(case)


def test_incomplete_derivation_cannot_silently_overwrite_partial_archive(case):
    registered = _register(case)
    (case[3] / "expert-supplement.tar").write_bytes(b"partial")
    with pytest.raises(ValueError, match="Incomplete"):
        supplement.derive_archive(case[0], registered["protocol_path"])


def test_cli_home_override_preserves_environment_config(case, monkeypatch, capsys):
    settings = case[0]
    override = settings.home / "override"
    monkeypatch.setattr(supplement.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(supplement.sys, "argv", ["helper", "--home", str(override), "prepare", "protocol.json"])

    def prepare(selected_settings, protocol):
        assert selected_settings.home == override and selected_settings.config == settings.config
        assert protocol == Path("protocol.json")
        return {"fixture": "no prediction"}

    monkeypatch.setattr(supplement, "prepare_supplement", prepare)
    supplement.main()
    assert json.loads(capsys.readouterr().out) == {"fixture": "no prediction"}
