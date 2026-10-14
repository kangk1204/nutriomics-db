import gzip

import pytest

from scripts.import_gse32357_primary import paired_contexts


def matrix(titles):
    rows = [
        "!Sample_geo_accession\t" + "\t".join('"GSM' + str(i) + '"' for i in range(len(titles))),
        "!Sample_title\t" + "\t".join('"' + title + '"' for title in titles),
        "!Sample_data_processing\t" + "\t".join('"RMA in Oligo"' for _ in titles),
        "!Sample_treatment_protocol_ch1\t"
        + "\t".join('"150mg/day trans-resveratrol"' for _ in titles),
    ]
    return gzip.compress(("\n".join(rows) + "\n!series_matrix_table_begin\n").encode())


def test_pairs_use_source_subject_and_arm_not_column_order():
    rows = paired_contexts(
        matrix(
            [
                "Subject 2_t=30days_placebo",
                "Subject 1_t=30days_resveratrol",
                "Subject 2_t=30days_resveratrol",
                "Subject 1_t=30days_placebo",
            ]
        )
    )
    assert [(row["participant"], row["treatment"]) for row in rows] == [
        ("2", "PLACEBO"),
        ("1", "RESVERATROL"),
        ("2", "RESVERATROL"),
        ("1", "PLACEBO"),
    ]


@pytest.mark.parametrize(
    "titles",
    [
        ["Subject 1_t=30days_resveratrol"],
        ["Subject 1_t=30days_resveratrol", "Subject 1_t=30days_resveratrol"],
        ["Subject 1_t=30days_resveratrol", "Subject 2_t=30days_placebo"],
        ["Subject 1_t=baseline_placebo", "Subject 1_t=30days_resveratrol"],
    ],
)
def test_incomplete_duplicate_or_unrecognized_contexts_are_rejected(titles):
    with pytest.raises(ValueError):
        paired_contexts(matrix(titles))
