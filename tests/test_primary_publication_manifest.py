"""A repository accession is provenance, not a primary publication."""
from scripts.build_human_exposure_manifest import manifest


def test_n06_primary_publication_is_distinct_from_original_geo_source():
    data = manifest()
    n06 = next(s for s in data["studies"] if s["id"] == "N06")
    primary = next(r for r in n06["sources"] if r["label"] == "Primary publication")
    assert primary["url"] == "https://pubmed.ncbi.nlm.nih.gov/21429719/"
    assert n06["pubmed_id"] == "21429719"
    assert any(r["label"] == "GSE27385" and "acc=GSE27385" in r["url"] for r in n06["sources"])
    assert data["expert_review_status"] == "pending"
    assert data["curation_revision"] == "2026-10-20-primary-reference-v2"


def test_registered_primary_publications_are_not_repository_urls():
    for study in manifest()["studies"]:
        for source in study["sources"]:
            if source["label"] == "Primary publication":
                assert "ncbi.nlm.nih.gov/geo/" not in source["url"]
                assert "ebi.ac.uk/biostudies/" not in source["url"]
