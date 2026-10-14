import pytest

from nutrigenomics.metabolomics import parse_source


def fixture():
    records = {}
    rows = []
    for index, condition in enumerate(["Pre Chicken", "Post Chicken", "Pre Pork", "Post Pork"]):
        title = "S31-119 " + ["Pre-Ch", "Post Po", "Pre-Po", "Post-Ch"][index]
        group = "Sample type:Urine | Food type:urine | Pre or Post and food:" + condition
        records[str(index)] = {
            "study_id": "ST001257",
            "local_sample_id": title,
            "mb_sample_id": "SA" + str(index),
            "sample_source": "Urine",
            "factors": group,
        }
        rows.append(title + "\t" + group + "\tNA\t0")
    group = "Sample type:Food | Food type:Apple | Pre or Post and food:na"
    records["4"] = {
        "study_id": "ST001257",
        "local_sample_id": "Apple",
        "mb_sample_id": "SA4",
        "sample_source": "Urine",
        "factors": group,
    }
    rows.append("Apple\t" + group + "\t3.5\t-1")
    return (
        ("Samples\tgroup\t137.0487_1.5530007\t187.0602_5.261997\n" + "\n".join(rows)).encode(),
        records,
    )


ANALYSIS = {
    "study_id": "ST001257",
    "analysis_id": "AN002086",
    "units": "Abundance (Counts Log2 Transformed)",
}
MWTAB = "Has m/z:Neutral masses RT units:Minutes"


def test_factor_context_overrides_conflicting_generic_fields_without_renaming_features():
    raw, metadata = fixture()
    features, samples, report = parse_source(raw, metadata, ANALYSIS, MWTAB)
    assert features[0] == "137.0487_1.5530007"
    assert report["n_people"] == 1 and report["food"] == 1 and report["urine"] == 4
    assert report["missing"] == 4 and report["zero"] == 4
    assert len(report["conflicts"]) == 3
    assert samples[-1]["sample_type"] == "food" and samples[-1]["participant"] is None
    assert samples[-1]["values"] == [3.5, -1.0]
    assert samples[1]["arm"] == "Chicken"


@pytest.mark.parametrize(
    "change",
    ["units", "identity", "duplicate", "group", "missing_token", "feature_semantics", "incomplete"],
)
def test_unreviewed_context_fails(change):
    raw, metadata = fixture()
    analysis = dict(ANALYSIS)
    mwtab = MWTAB
    if change == "units":
        analysis["units"] = "Counts"
    if change == "identity":
        analysis["analysis_id"] = "AN-other"
    if change == "duplicate":
        metadata["4"]["mb_sample_id"] = "SA0"
    if change == "group":
        metadata["0"]["factors"] = metadata["1"]["factors"]
    if change == "missing_token":
        raw = raw.replace(b"NA", b"nan")
    if change == "feature_semantics":
        mwtab = "Has m/z:Yes"
    if change == "incomplete":
        raw = b"\n".join(raw.splitlines()[:-2])
        metadata.pop("3")
        metadata.pop("4")
    with pytest.raises(ValueError):
        parse_source(raw, metadata, analysis, mwtab)
