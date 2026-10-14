import gzip

import pytest

from nutrigenomics.platform_mapping import parse_annotation


def fixture(rows, platform="GPL1"):
    return gzip.compress(
        (
            "!Annotation_date = Jan 01 2016\n!Annotation_platform = "
            + platform
            + "\n!Annotation_platform_organism = Homo sapiens\n!platform_table_begin\nID\tGene ID\n"
            + rows
            + "!platform_table_end\n"
        ).encode()
    )


def test_multiple_gene_ids_are_preserved_and_missing_is_not_zero():
    rows, report = parse_annotation(
        fixture("p1\t207///208\np2\t\np3\t999\n"), "GPL1", {"p1", "p2", "absent"}
    )
    assert rows[0]["gene_identifiers"] == ["207", "208"]
    assert report["source_multiple_gene_features"] == 1
    assert report["source_unmapped_features"] == 1
    assert report["features_absent_from_annotation"] == 1
    assert report["current_gene_status_unverified"]


def test_platform_mismatch_is_rejected_even_with_shared_probe_id():
    with pytest.raises(ValueError, match="Platform"):
        parse_annotation(fixture("p1\t207\n", "GPL570"), "GPL7020", {"p1"})


@pytest.mark.parametrize("rows", ["p1\tENSG00000001\n", "p1\tGeneID:207\n", "p1\t207\np1\t208\n"])
def test_incompatible_namespace_and_duplicates_are_rejected(rows):
    with pytest.raises(ValueError):
        parse_annotation(fixture(rows), "GPL1", {"p1"})


def test_decompression_cap_fails_closed():
    with pytest.raises(ValueError, match="cap"):
        parse_annotation(fixture("p1\t207\n"), "GPL1", {"p1"}, max_expanded=20)
