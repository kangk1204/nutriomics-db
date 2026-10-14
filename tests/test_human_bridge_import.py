from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from nutrigenomics.bridge import meal_evidence
from nutrigenomics.bridge_models import Exposure, StudyContrast
from nutrigenomics.db import create_schema, get_session_factory
from nutrigenomics.models import AnalysisRun, Component, GeoSeries, ResearchStudy, StudyEffect
from scripts.build_human_exposure_manifest import manifest
from scripts.import_human_bridge import import_bridge


@pytest.mark.parametrize("dose", [1000, 1500])
def test_compact_vs500_comparator_is_preserved_as_its_actual_arm(tmp_path, dose):
    url = f"sqlite:///{(tmp_path / 'arms.sqlite3').as_posix()}"
    create_schema(url)
    name = f"dose_NormalWeight_{dose}_vs500_2h_change"
    with get_session_factory(url)() as session:
        session.add(ResearchStudy(id="S02", title="Energy crossover", description="Fixture"))
        session.flush()
        session.add(GeoSeries(accession="GSE56960", study_id="S02", title="Energy crossover"))
        session.flush()
        session.add(AnalysisRun(series_accession="GSE56960", analysis_version="v3", status="completed"))
        session.add(StudyEffect(series_accession="GSE56960", feature_type="gene", feature_id="A",
            contrast=name, analysis_version="v3", n_people=7, n_samples=168))
        session.commit()
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest()), encoding="utf-8")
    import_bridge(path, url)
    with get_session_factory(url)() as session:
        row = session.scalar(select(StudyContrast).where(StudyContrast.contrast == name))
        assert {arm.split(":")[2] for arm in row.arm_ids} == {"500", str(dose)}
        assert len(row.exposure_ids) == 2
        assert row.metadata_json["arm_selection_status"] == "identified_from_contrast"


def test_exact_results_are_referenced_not_duplicated_and_version_is_immutable(tmp_path):
    url = f"sqlite:///{(tmp_path / 'bridge.sqlite3').as_posix()}"
    create_schema(url)
    with get_session_factory(url)() as session:
        session.add(ResearchStudy(id="N15", title="Dairy", description="Fixture"))
        session.flush()
        session.add(GeoSeries(accession="GSE98645", study_id="N15", title="Dairy"))
        session.add(Component(component_key="FDC-NUTRIENT:1004", name="Fat", category="nutrient", canonical_unit="g"))
        session.flush()
        session.add(AnalysisRun(series_accession="GSE98645", analysis_version="v1", status="completed"))
        session.add(StudyEffect(series_accession="GSE98645", feature_type="gene", feature_id="A",
            contrast="yogurt_vs_milk_2h_change", analysis_version="v1", effect_estimate=-0.02,
            q_value=0.8, n_people=7, n_samples=56, tissue="whole blood", effect_unit="log2 fold change"))
        session.commit()
    path=tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest()),encoding="utf-8")
    first=import_bridge(path,url)
    second=import_bridge(path,url)
    assert first["contrasts"] == second["contrasts"] == 1
    assert second["legacy_links"] == 0
    with get_session_factory(url)() as session:
        assert session.scalar(select(func.count()).select_from(StudyEffect)) == 1
        contrast=session.scalar(select(StudyContrast))
        assert contrast.n_people == 7 and contrast.n_samples == 56
        assert len(contrast.exposure_ids) == 2  # Acute products, not 400g/day chronic regimen.
        fat=session.scalar(select(Component))
        result=meal_evidence(session,{"components":[{"component_id":fat.id,"amount":4,"unit":"g"}],"items":[]})
        observed=[r for r in result["observed_evidence"] if r["result_status"]=="reanalyzed"]
        assert len(observed)==2
        assert observed[0]["results"][0]["q_value"]==0.8
        assert observed[0]["clinical_endpoints"][0]["n_people"]==13
        assert observed[0]["clinical_endpoints"][1]["effect_estimate"] is None
        before=session.scalar(select(func.count()).select_from(Exposure))
    changed=manifest()
    changed["studies"][0]["title"]="Changed same published version"
    path.write_text(json.dumps(changed),encoding="utf-8")
    with pytest.raises(ValueError,match="new version"):
        import_bridge(path,url)
    with get_session_factory(url)() as session:
        assert session.scalar(select(func.count()).select_from(Exposure))==before
