import io
import json
import zipfile

import pytest

from nutrigenomics.context_joins import nutrient_codebook, verify_definition


def test_definition_requires_all_three_numeric_and_unit_keys():
    entity = {
        "identifier": "1003",
        "label": "Protein",
        "metadata_json": json.dumps({"number": "203", "unit": "g"}),
    }
    definition = {"id": "1003", "name": "Protein", "nutrient_nbr": "203", "unit_name": "g"}
    assert not verify_definition(entity, definition)["label_changed"]
    for key, value in [("id", "1004"), ("nutrient_nbr", "204"), ("unit_name", "mg")]:
        with pytest.raises(ValueError):
            verify_definition(entity, dict(definition, **{key: value}))


def test_exact_archive_member_ignores_other_csvs():
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("root/sample_food.csv", "not a nutrient codebook")
        archive.writestr(
            "root/nutrient.csv", "id,name,unit_name,nutrient_nbr\n1003,Protein,g,203\n"
        )
    rows, _ = nutrient_codebook(payload.getvalue())
    assert rows["1003"]["nutrient_nbr"] == "203"


def test_duplicate_nutrient_ids_rejected():
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(
            "nutrient.csv", "id,name,unit_name,nutrient_nbr\n1003,Protein,g,203\n1003,Other,g,204\n"
        )
    with pytest.raises(ValueError, match="Duplicate"):
        nutrient_codebook(payload.getvalue())
