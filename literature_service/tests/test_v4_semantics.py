"""Behavior regressions and failure accounting; fixtures do not measure accuracy."""
import copy
import pytest
from nutri_literature.candidates_v4 import GeneIndex, normalize_gene, source_chunks, taxon_context
from nutri_literature.clinical_v4 import assertion_state, bind_conditions, quantity_spans, summary
from nutri_literature.db import digest, dumps
from nutri_literature.engines import validate_source_spans
from nutri_literature.evaluation_v4 import failure_stages, select_provider, strict_gene_counts
from nutri_literature.extraction_v4 import _event_key
from nutri_literature.registry_v4 import engine_rules_version, engine_manifest_validation


@pytest.fixture
def genes():
    rows=[{"id":"NCBIGene:1","native_id":"1","symbol":"ABC","name":"alpha beta cytokine","aliases":["X"],"taxon_id":9606},
          {"id":"NCBIGene:2","native_id":"2","symbol":"Abc","name":"alpha beta cytokine","aliases":[],"taxon_id":10090},
          {"id":"NCBIGene:3","native_id":"3","symbol":"OTHER","name":"other gene","aliases":["X"],"taxon_id":9606}]
    return GeneIndex({"records":rows,"manifest":{"records_sha256":digest(dumps(rows))}})


def sentence(text,label="Results",start=0,**extra):
    return {"text":text,"start":start,"end":start+len(text),"label":label,**extra}


def test_long_unicode_source_coverage():
    text="α"*25000+"\n"+"VDR expression "*3000
    chunks=list(source_chunks(text))
    assert "".join(chunk for _,chunk in chunks)==text
    assert all(text[start:start+len(chunk)]==chunk for start,chunk in chunks)
    assert max(len(chunk) for _,chunk in chunks)<=12000


@pytest.mark.parametrize("line,taxon,model",[("In human cells",9606,"cell"),("In mice",10090,"animal"),
                                            ("In rats",10116,"animal"),("In mice and humans",None,"animal"),
                                            ("The protein was measured",None,"unknown")])
def test_species_and_model_are_independent(line,taxon,model):
    value=taxon_context(line,line,{})
    assert value[:2]==(taxon,model)


@pytest.mark.parametrize("term,line,expected,reason",[("ABC","human cells","NCBIGene:1",None),
       ("Abc","mice","NCBIGene:2",None),("ABC","cells",None,"species_unresolved"),
       ("X","human cells",None,"alias_collision")])
def test_native_identity_does_not_guess(genes,term,line,expected,reason):
    value=normalize_gene({"kind":"gene","text":term,"start":0,"end":len(term)},line,line,genes,{}, {})
    assert value["normalized_id"]==expected
    assert value["payload"]["identity_reason"]==reason


def test_abbreviation_conflict_overrides_symbol(genes):
    result=normalize_gene({"kind":"gene","text":"ABC","start":20,"end":23},"human cells","human cells",genes,{},
                          {"ABC":[{"short_start":5,"long":"annual blood count"}]})
    assert result["normalized_id"] is None and result["payload"]["identity_reason"]=="abbreviation_conflict"


@pytest.mark.parametrize("text,label,expected",[("We will test whether vitamin D reduced VDR.","Results","planned"),
    ("We aimed to determine whether blood pressure decreased.","Abstract","planned"),
    ("Vitamin D reduced VDR expression.","Results","observed"),
    ("Vitamin D reduced VDR expression.","Introduction","background"),
    ("Vitamin D may reduce VDR expression.","Results","interpretation"),
    ("Blood pressure was not significantly reduced.","Results","observed"),
    ("Results will be reported next year.","Results","planned"),
    ("Vitamin D may have increased expression.","Results","interpretation")])
def test_role_label_does_not_prove_observation(text,label,expected):
    assert assertion_state(sentence(text,label))==expected


def test_summary_never_fills_result_with_plan():
    row=sentence("We will assess whether caffeine lowers blood pressure.",assertion_state="planned",rhetorical_role="results")
    value=summary([row],[])
    assert value["results"]==[] and value["missing_results"]
    assert "관측 결과 미확인" in value["notice"]


def test_result_is_not_reused_as_design():
    row=sentence("In human cells, vitamin D reduced VDR expression.",assertion_state="observed",rhetorical_role="background")
    value=summary([row],[])
    assert value["method"]=="미확인" and len(value["results"])==1


def test_non_significance_and_iu_do_not_imply_conversion():
    row=sentence("Patients received 2000 IU of vitamin D; blood pressure was not significantly reduced (p=0.07).")
    quantities=quantity_spans(row)
    iu=next(q for q in quantities if q.get("unit_original")=="IU")
    assert iu["normalized_value"] is None and iu["ucum"]=="[IU]"
    assert next(q for q in quantities if q["quantity_role"]=="p_value")["value"]=="0.07"
    validate_source_spans(quantities,row["text"])


def test_multiple_events_prevent_number_assignment():
    row=sentence("Patients received vitamin D 20 mg for 4 weeks (p=0.03).")
    a=row["text"].index("vitamin D")
    mention={"kind":"nutrient","text":"vitamin D","start":a,"end":a+9,"normalized_id":"D"}
    event={"subject":"D","controller":mention}
    result=bind_conditions(event,row,[mention],unique_event=False)
    assert not result["quantities"] and not result["statistics"] and result["timepoint"] is None
    assert result["unassigned_quantities"]


def test_unknown_conditions_are_not_cross_quote_merge_proof():
    event={"subject":"A","object":"B","conditions":{"arm_id":None,"timepoint":None},
           "evidence_quotes":[{"start":0,"end":10}]}
    other=copy.deepcopy(event); other["evidence_quotes"][0].update(start=11,end=21)
    assert _event_key(event)!=_event_key(other)
    other=copy.deepcopy(event); other["direction"]="increase"
    assert _event_key(event)!=_event_key(other)


def test_strict_id_metrics_count_unresolved_candidates():
    doc={"entities":[{"kind":"gene","start":0,"end":3,"native_ids":["1"]}]}
    prediction={"entities":[{"kind":"gene","start":0,"end":3,"normalized_id":None}]}
    assert strict_gene_counts(doc,prediction,{})["fn"]==1
    assert strict_gene_counts(doc,prediction,{})["fp"]==1


def test_failure_waterfall_has_one_first_failure():
    text="ABC D"
    doc={"text":text,"entities":[{"kind":"gene","start":0,"end":3,"native_ids":["1"]},
          {"kind":"component","start":4,"end":5,"native_ids":["D"]}],
          "relations":[{"entity1":"D","entity2":"1","predicate":"Negative_Correlation"}]}
    pred={"source_hash":digest(text),"entities":[],"events":[],"relations":[]}
    rows=failure_stages(doc,pred,{})
    assert len(rows)==1 and rows[0]["first_failure"]=="entity_span"


def test_below_precision_goal_does_not_select_provider():
    report={"mode":"end_to_end","profile":"local","metrics":{"gene_detection":{"precision":.899}},"provider":"x"}
    assert select_provider([report])["selected"] is None


def test_v4_model_missing_fails_closed():
    value={"engine_version":"heuristic-v4","rules_version":engine_rules_version("heuristic-v4"),"manifest":{}}
    assert "candidate_provider_unavailable" in engine_manifest_validation(value,"heuristic-v4")


def test_source_relation_benchmark_does_not_approve_background_map():
    from nutri_literature.evaluation_v4 import project_source_event
    gene={"kind":"gene","normalized_id":"1","start":0,"end":1,"text":"B"}
    chem={"kind":"component","normalized_id":"D","start":2,"end":3,"text":"A"}
    event={"status":"background","assertion_state":"background","subject":"D","object":"1","direction":"decrease",
           "controller":chem,"theme":gene,"polarity":"positive","negation":False,"speculation":False,"triage_reasons":[]}
    assert project_source_event(event,{"entities":[gene,chem]}, {},True)
    assert event["status"]=="background"
    event["assertion_state"]="planned"
    assert project_source_event(event,{"entities":[gene,chem]}, {},True) is None


def test_frozen_scientific_engines_unchanged():
    from nutri_literature.extraction_v2 import RULES_VERSION as v2
    from nutri_literature.extraction_v3 import RULES_VERSION as v3
    assert v2=="rules-sha256:11b4f00cf76a1b56b5cc273bd150474e021b4ba400f274b8b168f6f9486eae66"
    assert v3=="rules-sha256:16852dbcb162dd105608ae4170bc4aa4fb5ae2781a2e1f6e6c1168b6cdde6fc4"
def test_tokenizer_removed_format_character_keeps_label_positions():
    from nutri_literature.candidates_v4 import source_word_labels
    tokens = [{"text": "VDR"}, {"text": "\ufeff"}, {"text": "ACE"}]
    assert source_word_labels(tokens, {0: 1, 2: 1}) == [1, 0, 1]


def test_tokenizer_printable_omission_is_not_silent():
    from nutri_literature.candidates_v4 import source_word_labels
    from nutri_literature.nlp_v2 import ParserUnavailable
    import pytest
    with pytest.raises(ParserUnavailable, match="Printable"):
        source_word_labels([{"text": "VDR"}, {"text": "ACE"}], {0: 1})

def test_ucum_molarity_and_length_keep_case_and_percentage_is_not_dose():
    from nutri_literature.clinical_v4 import quantity_spans
    text="Treatment at 10 mM reduced lesions by 5 mm and expression by 20%."
    values=quantity_spans({"start":0,"text":text})
    assert [(q["ucum"],q["quantity_role"]) for q in values]==[
        ("mmol/L","concentration"),("mm","length"),("%","unknown")]


def test_ambiguous_unit_capitals_are_preserved_without_assignment():
    from nutri_literature.clinical_v4 import quantity_spans
    value=quantity_spans({"start":0,"text":"Treatment used 10 MM."})[0]
    assert value["ucum"] is None and value["quantity_role"]=="unknown"
    assert value["hold_reason"]=="ambiguous_unit_case" and value["text"]=="10 MM"

