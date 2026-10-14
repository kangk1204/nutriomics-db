from types import SimpleNamespace

from scripts.audit_geo_design import (
    _candidate_timepoint,
    _characteristics,
    _study_fields,
    _subject_id,
    _timepoint,
    audit_design,
)


def test_characteristics_keep_repeated_keys_and_colons():
    parsed = _characteristics(["tissue: whole blood", "time: 6 h", "note: before: after"])

    assert parsed == {
        "tissue": ["whole blood"],
        "time": ["6 h"],
        "note": ["before: after"],
    }


def test_subject_candidate_prefers_explicit_characteristic_then_title():
    chars = _characteristics(["subject identifier: P-07"])
    assert _subject_id("After intervention", chars) == ("P-07", "characteristic:subject identifier")
    assert _subject_id("Before MOF supplementation_Subject12", {}) == ("12", "title_label")


def test_numeric_suffix_is_reported_as_candidate_not_authoritative_id():
    assert _subject_id("PWBC_base_lowfat.3", {}) == ("3", "title_numeric_suffix_candidate")
    assert _subject_id("sample without identifier", {}) == (None, "unresolved")


def test_timepoint_candidates_cover_acute_and_longitudinal_labels():
    assert _timepoint("Subject 7, Fasting (0h)") == "0h"
    assert _timepoint("Subject 7, Postprandial (6 hours)") == "6h"
    assert _timepoint("Blood_30days_after_intervention") == "30d"
    assert _timepoint("After 8wk supplementation") == "8w"
    assert _timepoint("Before MOF supplementation") == "baseline"
    assert _timepoint("PWBC_5yr_lowfat.3") == "5y"
    assert _timepoint("Peripheral white blood cells five years low-fat") == "5y"
    assert (
        _candidate_timepoint(
            "PWBC_5_lowfat.1",
            {},
            {"source_name_ch1": ["Peripheral white blood cells five years low-fat"]},
        )
        == "5y"
    )


def test_super_series_with_multiple_platforms_is_reported_as_mixed():
    series = SimpleNamespace(
        title="Combined study",
        assay_type="Methylation and gene expression",
        platform_ids=["GPL10332", "GPL13534"],
    )

    assert _study_fields(series)[0] == "mixed assays"


def test_design_audit_preserves_raw_characteristics_and_marks_candidates():
    samples = [
        SimpleNamespace(
            sample_accession="GSM1",
            title="Subject 1, Fasting (0h)",
            characteristics=["tissue: whole blood", "metabolic status: normal weight"],
            metadata_json={"platform_id": ["GPL1"]},
        ),
        SimpleNamespace(
            sample_accession="GSM2",
            title="Subject 1, Postprandial (6h)",
            characteristics=["tissue: whole blood", "metabolic status: normal weight"],
            metadata_json={"platform_id": ["GPL1"]},
        ),
    ]
    series = SimpleNamespace(
        accession="GSE1",
        study_id="study1",
        title="Test series",
        assay_type="Expression profiling by array",
        platform_ids=["GPL1"],
        samples=samples,
        sample_count=2,
    )

    rows, summary = audit_design(series)

    assert [row["subject_candidate"] for row in rows] == ["1", "1"]
    assert [row["timepoint_candidate"] for row in rows] == ["0h", "6h"]
    assert '"tissue: whole blood"' in rows[0]["characteristics_json"]
    assert summary["repeated_subject_counts"] == {"1": 2}
    assert summary["assay_group"] == "gene expression"


def test_two_color_array_retains_timepoint_per_channel():
    samples = [
        SimpleNamespace(
            sample_accession="GSM1",
            title="Blood-Volunteer1",
            characteristics=["MOF supplementation: none | 8 weeks"],
            metadata_json={
                "channel_count": ["2"],
                "source_name_ch1": ["Blood at the beginning of the study (time 0)"],
                "source_name_ch2": ["Blood after 8 weeks of supplementation"],
                "label_ch1": ["Cy3"],
                "label_ch2": ["Cy5"],
            },
        )
    ]
    series = SimpleNamespace(
        accession="GSE54325",
        study_id="study",
        title="Paired two-color array",
        assay_type="Expression profiling by array",
        platform_ids=["GPL10332"],
        samples=samples,
        sample_count=1,
    )

    rows, summary = audit_design(series)

    assert rows[0]["timepoint_candidate"] is None
    assert rows[0]["channel_timepoints_json"] == '{"ch1": "baseline", "ch2": "8w"}'
    assert rows[0]["paired_channel_design_json"] == (
        '{"ch1": {"timepoint_candidate": "baseline", "label": "Cy3", '
        '"source_name": ["Blood at the beginning of the study (time 0)"]}, '
        '"ch2": {"timepoint_candidate": "8w", "label": "Cy5", '
        '"source_name": ["Blood after 8 weeks of supplementation"]}}'
    )
    assert summary["channel_timepoint_counts"] == {"baseline": 1, "8w": 1}


def test_diet_arm_is_part_of_numeric_suffix_subject_candidate():
    def sample(accession, title):
        return SimpleNamespace(
            sample_accession=accession,
            title=title,
            characteristics=["diet: Low-fat diet"],
            metadata_json={"platform_id": ["GPL13534"]},
        )

    samples = [
        sample("GSM1", "PWBC_base_lowfat.1"),
        sample("GSM2", "PWBC_5yr_lowfat.1"),
        sample("GSM3", "PWBC_base_lowfat.2"),
    ]
    series = SimpleNamespace(
        accession="GSE107205",
        study_id="study",
        title="PREDIMED methylation",
        assay_type="Methylation profiling by array",
        platform_ids=["GPL13534"],
        samples=samples,
        sample_count=3,
    )

    rows, summary = audit_design(series)

    assert [row["subject_candidate"] for row in rows] == [
        "low-fat diet#1",
        "low-fat diet#1",
        "low-fat diet#2",
    ]
    assert [row["timepoint_candidate"] for row in rows] == ["baseline", "5y", "baseline"]
    assert summary["subjects_parsed"] == 2
    assert summary["timepoint_counts"] == {"baseline": 2, "5y": 1}
