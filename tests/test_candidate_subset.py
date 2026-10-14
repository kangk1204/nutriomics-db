import gzip

import pytest

from scripts.import_candidate_subset import parse


def test_processed_fractional_values_preserved():
    frame, report = parse(
        gzip.compress(b"HMPREF1_CDS\t0.0098\t0.0001\nHMPREF2_CDS\t0\t0\n"), "GSE46761"
    )
    assert frame.iloc[0, 1] == 0.0098 and report["fractional_values"] == 2
    assert report["reported_zero_values"] == 2


def test_human_source_alias_preserved():
    _frame, report = parse(gzip.compress(b"Geneid,AMa68A\nENSG00000160072,334\n"), "GSE323980")
    assert report["columns"] == ["Geneid", "AMa68A"]


def test_unresolved_source_ids_preserved_without_gene_mapping():
    frame, report = parse(
        gzip.compress(b"Geneid,AMa68A\nENSG00000160072,334\n123;456,9\n"), "GSE323980"
    )
    assert report["unresolved_identifiers"] == 1 and frame.iloc[1, 0] == "123;456"


def test_missing_preserved_separately_from_zero():
    _frame, report = parse(gzip.compress(b"Geneid,x\nENSG1,NaN\nENSG2,0\n"), "GSE323980")
    assert report["missing_values"] == 1 and report["reported_zero_values"] == 1


@pytest.mark.parametrize(
    "data", [b"Geneid,x\nENSG1,1\nENSG1,2\n", b"Geneid,x\nENSG1,-1\n", b"Geneid,x\nENSG1,inf\n"]
)
def test_invalid_measurements_rejected(data):
    with pytest.raises(ValueError):
        parse(gzip.compress(data), "GSE323980")
