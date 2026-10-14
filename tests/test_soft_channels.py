from nutrigenomics.ingest import parse_soft_samples


def test_soft_parser_preserves_channel_specific_characteristics(tmp_path):
    path = tmp_path / "paired.soft.txt"
    path.write_text(
        """^SAMPLE = GSM000001
!Sample_title = Volunteer1 paired array
!Sample_characteristics_ch1 = treatment: baseline
!Sample_characteristics_ch2 = treatment: week 8
!Sample_label_ch1 = Cy3
!Sample_label_ch2 = Cy5
!Sample_source_name_ch1 = blood at baseline
!Sample_source_name_ch2 = blood after 8 weeks
""",
        encoding="utf-8",
    )

    sample = parse_soft_samples(path)["GSM000001"]

    assert sample["characteristics"] == ["treatment: baseline", "treatment: week 8"]
    assert sample["channel_characteristics"] == {
        "ch1": ["treatment: baseline"],
        "ch2": ["treatment: week 8"],
    }
    assert sample["metadata"]["source_name_ch1"] == ["blood at baseline"]
    assert sample["metadata"]["source_name_ch2"] == ["blood after 8 weeks"]
    assert sample["metadata"]["label_ch1"] == ["Cy3"]
    assert sample["metadata"]["label_ch2"] == ["Cy5"]
