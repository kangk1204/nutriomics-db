from nutri_literature.automatic_features_v6 import training_labels, endpoint_features


def mention(kind, identifier=None, native=None, **extra):
    return {"kind": kind, "text": "surface", "normalized_id": identifier,
            "payload": {"native_id": native, **extra}}


def test_relation_training_reflects_automatic_wrong_id_and_unresolved_errors():
    gold = {"relations": [{"entity1": "D1", "entity2": "22", "predicate": "Association"}]}
    chemical = mention("component", "MESH:D1", "D1")
    pair = {"chemical": chemical, "gene": mention("gene", "NCBIGene:22", "22")}
    assert training_labels(gold, [pair]) == ["Association"]
    pair["gene"] = mention("gene", "NCBIGene:23", "23")
    assert training_labels(gold, [pair]) == []
    pair["gene"] = mention("gene", native="22")
    assert training_labels(gold, [pair]) == []


def test_endpoint_quality_does_not_include_identifier_or_erase_candidate():
    pair = {"chemical": mention("component", "MESH:SECRET", "SECRET"),
            "gene": mention("gene", automatic_selected=False, detection_priority_score=.3,
                            identity_reason="species_unresolved_or_conflicting")}
    features = endpoint_features([pair])
    assert "SECRET" not in repr(features)
    assert features["quality:gene:span_score"] == .3
    assert pair["gene"]["payload"]["automatic_selected"] is False


def test_fold_with_no_positive_rare_relation_type_keeps_constant_negative_head():
    from nutri_literature.automatic_models_v6 import NoiseAwareHead, TYPES
    model = NoiseAwareHead().fit([{"context": 1.}, {"context": 2.}], [[], []], [1., 1.], .1)
    assert model.predict([{"context": 3.}]) == [{key: 0. for key in TYPES}]
