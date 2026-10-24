"""V4: model candidates, native identity, typed events and observation-gated summaries.

Frozen v1/v2/v3 are reused as pure components, never edited. Missing models fail
closed. All final semantic decisions and relationships are explicit own rules.
"""
import hashlib
from pathlib import Path
import re

from . import extraction_v3 as v3
from .db import digest, dumps
from .text import sentence_spans
from .nlp_v2 import ParserUnavailable, parser_for, parser_manifest, validate_tokens
from .candidates_v4 import collect_candidates
from .clinical_v4 import assertion_state, bind_conditions, summary
from .grammar_v3 import modifiers

ENGINE_VERSION="heuristic-v4"
_ROOT=Path(__file__).resolve().parents[1]
FILES=["nutri_literature/"+name+".py" for name in
       ("candidates_v4","clinical_v4","extraction_v4","registry_v4","evaluation_v4","training_v4","operations_v4","pubtator_v4","cli_v4")]


def source_manifest():
    paths=sorted(set(FILES+v3._SOURCES))
    hashes={}
    value=hashlib.sha256()
    for name in paths:
        raw=(_ROOT/name).read_bytes()
        hashes[name]=digest(raw)
        value.update(name.encode()+b"\0"+raw+b"\0")
    return hashes,"rules-sha256:"+value.hexdigest()


def _event_key(event):
    fields={key:event.get(key) for key in ("subject","object","relation_type","outcome_type","direction",
            "negation","speculation","significance","species","study_model","taxon_id","assertion_state","conditions")}
    # Unknown arm/time/dose is not proof of shared conditions across different sentences.
    conditions=event.get("conditions",{})
    if not all(conditions.get(key) for key in ("arm_id","timepoint")):
        fields["source_spans"]=[(q["start"],q["end"]) for q in event["evidence_quotes"]]
    return digest(dumps(fields))


def nested_events(sentence, mentions, tokens):
    """Reduction in expression of B caused by treatment with A; dependency proof.

    No nearest-entity or co-occurrence relation fallback is permitted.
    """
    result=[]
    names={"reduction":"decrease","inhibition":"decrease","suppression":"decrease",
           "increase":"increase","elevation":"increase","activation":"increase"}
    for token in tokens:
        kind=names.get(token["text"].casefold())
        if not kind:
            continue
        themes=v3._preposition_objects(tokens,token,{"in","of","on"})
        controllers=v3._preposition_objects(tokens,token,{"by"})
        parent=tokens[token["head"]]
        if not controllers and parent["lemma"] in {"cause","lead","result","produce","induce","show","demonstrate"}:
            controllers=v3._subject_roots(tokens,parent)
        if not controllers or not themes:
            continue
        left=[m for root in controllers for m in mentions if m["kind"] in {"food","diet","component","nutrient"}
              and any(m["start"]<=tokens[i]["start"] and tokens[i]["end"]<=m["end"]
                      for i in v3._descendants(tokens,root["i"],{"conj","ccomp","advcl"}))]
        right=[m for root in themes for m in mentions if m["kind"] in {"gene","outcome"}
               and any(m["start"]<=tokens[i]["start"] and tokens[i]["end"]<=m["end"]
                       for i in v3._descendants(tokens,root["i"],{"conj","ccomp","advcl"}))]
        left={ (m["start"],m["end"]):m for m in left }
        right={ (m["start"],m["end"]):m for m in right }
        if len(left)!=1 or len(right)!=1:
            continue
        controller,theme=next(iter(left.values())),next(iter(right.values()))
        scope=v3._scope(sentence,tokens,token)
        direction,negation,speculation,significance=modifiers(sentence,tokens,token,scope,kind)
        result.append({"subject":v3._endpoint(controller),"object":v3._endpoint(theme),
                       "controller":controller,"theme":theme,"trigger":{k:token[k] for k in ("start","end","text","lemma")},
                       "relation_type":"effect","outcome_type":v3._outcome_type(theme,sentence["text"]),
                       "direction":direction,"association_direction":"unspecified","negation":negation,
                       "speculation":speculation,"speculative":speculation,"significance":significance,
                       "statistical_significance":significance,"polarity":"negated" if negation else "positive",
                       "scope":scope,"evidence_quotes":[{"quote":sentence["text"],"start":sentence["start"],"end":sentence["end"]}],
                       "rule_id":"typed-nested-regulation-v4","triage_reasons":[],"abstention_reasons":[],
                       "interpretation":"source statement candidate; dietary causality is not inferred"})
    return result


def adjacent_candidates(text, previous, current, mentions, tokens):
    """Explicit unique anaphora only; separate human-review candidates."""
    reference=re.match(r"\s*(This treatment|This intervention|This supplementation)\b",current["text"],re.I)
    if not reference or current.get("assertion_state")!="observed":
        return []
    prior=[m for m in mentions if previous["start"]<=m["start"] and m["end"]<=previous["end"]
           and m["kind"] in {"component","nutrient","food","diet"}]
    identities={m.get("normalized_id") or m["text"] for m in prior}
    if len(identities)!=1 or not prior:
        return []
    controller=prior[0]
    if not controller.get("normalized_id"):
        return []
    if re.search(r"\b(?:versus|compared|placebo|different doses|two groups)\b",previous["text"],re.I):
        return []
    proxy={"kind":"component","text":reference[1],"start":current["start"]+reference.start(1),
           "end":current["start"]+reference.end(1),"normalized_id":controller["normalized_id"],"payload":{},"status":"candidate"}
    local=[m for m in mentions if current["start"]<=m["start"] and m["end"]<=current["end"]]+[proxy]
    results=[]
    for token in tokens:
        kind=v3._TRIGGERS.get(token["lemma"])
        if kind not in {"increase","decrease"}:
            continue
        left,right,reasons,_=v3.roles_v3(tokens,token,kind,local)
        if reasons or len(left)!=1 or len(right)!=1 or left[0]["start"]!=proxy["start"]:
            continue
        theme=right[0]
        if theme["kind"]=="gene" and not theme.get("normalized_id"):
            continue
        scope=v3._scope(current,tokens,token)
        direction,negation,speculation,significance=modifiers(current,tokens,token,scope,kind)
        event={"subject":v3._endpoint(controller),"object":v3._endpoint(theme),"controller":controller,"theme":theme,
               "trigger":{k:token[k] for k in ("start","end","text","lemma")},"relation_type":"effect",
               "outcome_type":v3._outcome_type(theme,current["text"]),"direction":direction,"negation":negation,
               "speculation":speculation,"speculative":speculation,"significance":significance,"statistical_significance":significance,
               "polarity":"negated" if negation else "positive","association_direction":"unspecified",
               "taxon_id":theme.get("payload",{}).get("taxon_id"),"species":theme.get("payload",{}).get("species","unknown"),
               "study_model":theme.get("payload",{}).get("study_model","unknown"),"assertion_state":"observed",
               "status":"candidate","triage_reasons":["adjacent_coreference_requires_review"],"abstention_reasons":[],
               "evidence_quotes":[{"start":previous["start"],"end":current["end"],"quote":text[previous["start"]:current["end"]]}],
               "coreference_evidence":proxy,"source_relation_candidate":False,"rule_id":"explicit-adjacent-reference-v4",
               "conditions":bind_conditions({"controller":controller,"subject":v3._endpoint(controller)},previous,prior),
               "interpretation":"adjacent-sentence reference candidate requiring individual human review"}
        event.update({k:event["conditions"].get(k) for k in ("arm_id","timepoint","comparator")})
        event["organism"]=event["species"]
        event["event_id"]="event-sha256:"+_event_key(event)
        results.append(event)
    return results


def extract(text, sections, dictionaries, metadata=None):
    metadata=dict(metadata or {})
    provider=metadata.get("_candidate_provider")
    clinical=metadata.get("_clinical_models")
    gene_index=metadata.get("_gene_index")
    if not provider or not clinical or not gene_index:
        raise ParserUnavailable("V4 candidate, clinical and native gene artifacts are mandatory")
    sentences=sentence_spans(text,sections)
    mentions,definitions=collect_candidates(text,sentences,dictionaries,metadata,provider,gene_index)
    annotated=clinical.predict(sentences)
    for sentence in annotated:
        sentence["assertion_state"]=assertion_state(sentence,metadata)
    # Candidate injection reuses only V3 dependency rules. It is NOT a gold oracle.
    molecular=list(mentions)
    for sentence in annotated:
        for span in sentence.get("pico_spans",[]):
            if span["label"] in {"interventions","outcomes"} and not any(
                m["start"]<span["end"] and span["start"]<m["end"] for m in molecular):
                molecular.append({"kind":"component" if span["label"]=="interventions" else "outcome",
                                  "start":span["start"],"end":span["end"],"text":span["text"],
                                  "normalized_id":None,"status":"needs_review","payload":{"origin":"pico-crf-v4"}})
    base=v3.extract(text,sections,dictionaries,{**metadata,"_gold_entities":molecular})
    if base["status"]!="complete":
        raise ParserUnavailable("V4 dependency processing is incomplete")
    parser=parser_for(metadata)
    for sentence in annotated:
        local=[m for m in molecular if sentence["start"]<=m["start"] and m["end"]<=sentence["end"]]
        tokens=validate_tokens(parser.parse(sentence),sentence)
        base["events"].extend(nested_events(sentence,local,tokens))
    groups={}
    for event in base["events"]:
        quote=event["evidence_quotes"][0]
        sentence=next(s for s in annotated if s["start"]==quote["start"] and s["end"]==quote["end"])
        # A planning clause cannot inherit a past observation in another clause.
        trigger_offset=event["trigger"]["start"]-sentence["start"]
        boundaries=list(re.finditer(r";|\b(?:but|however|whereas)\b",sentence["text"],re.I))
        lo=max([0]+[m.end() for m in boundaries if m.end()<=trigger_offset])
        hi=min([len(sentence["text"])]+[m.start() for m in boundaries if m.start()>trigger_offset])
        state=assertion_state({**sentence,"text":sentence["text"][lo:hi]},metadata)
        if event.get("speculation") and state=="observed":
            state="interpretation"
        local=[m for m in molecular if sentence["start"]<=m["start"] and m["end"]<=sentence["end"]]
        event["assertion_state"]=state
        count=sum(e["evidence_quotes"][0]["start"]==sentence["start"] for e in base["events"])
        event["conditions"]=bind_conditions(event,sentence,local,unique_event=count==1)
        event.update({k:event["conditions"][k] for k in ("arm_id","comparator","timepoint")})
        taxa={m.get("payload",{}).get("taxon_id") for m in (event["controller"],event["theme"]) if m["kind"]=="gene"}
        taxa.discard(None)
        taxon=next(iter(taxa)) if len(taxa)==1 else None
        model=event["theme"].get("payload",{}).get("study_model",base["context"].get("study_model","unknown"))
        event.update(taxon_id=taxon,species="human" if taxon==9606 else "animal" if taxon else base["context"]["species"],study_model=model)
        event["organism"]=event["species"]
        reasons=list(event.get("triage_reasons",[]))
        if state!="observed":
            reasons.append("assertion_"+state)
        if any(m["kind"]=="gene" and not m.get("normalized_id") for m in (event["controller"],event["theme"])):
            reasons.append("gene_identity_unresolved")
        if event["controller"].get("payload",{}).get("ambiguous") or event["theme"].get("payload",{}).get("ambiguous"):
            reasons.append("ambiguous_entity_identity")
        event["triage_reasons"]=sorted(set(reasons))
        event["status"]="background" if state=="background" else "abstained" if state=="planned" else (
            "candidate" if state=="observed" and "gene_identity_unresolved" not in reasons else "needs_review")
        event["abstention_reasons"]=["planned_statement"] if state=="planned" else (
            ["background_or_prior_statement"] if state=="background" else [])
        event["measurement"]={"kind":event["outcome_type"],"target":event["object"]}
        event["source_relation_candidate"]=True
        event["event_id"]="event-sha256:"+_event_key(event)
        key=event["event_id"]
        if key in groups:
            for q in event["evidence_quotes"]:
                if q not in groups[key]["evidence_quotes"]:
                    groups[key]["evidence_quotes"].append(q)
        else:
            groups[key]=event
    events=list(groups.values())
    for previous,current in zip(annotated,annotated[1:]):
        if re.match(r"\s*This (?:treatment|intervention|supplementation)\b",current["text"],re.I):
            tokens=validate_tokens(parser.parse(current),current)
            events.extend(adjacent_candidates(text,previous,current,molecular,tokens))
    assertions=[]
    for event in events:
        if event["status"]!="candidate":
            continue
        predicate={"increase":"increases","decrease":"decreases","association":"associated_with"}.get(event["direction"],
                  "binds" if event["relation_type"]=="binding" else "affects")
        assertions.append({"subject":event["subject"],"object":event["object"],"predicate":predicate,
                           **event["evidence_quotes"][0],"rule_id":event["rule_id"],
                           "payload":{"event":event,"explicit_relation":True,"subject_mention":event["controller"],
                                      "object_mention":event["theme"],"polarity":event["polarity"],
                                      "speculative":event["speculation"],"organism":event["species"],
                                      "triage_reasons":event["triage_reasons"],"rule_score":1}})
    hashes,rules=source_manifest()
    manifest={"engine_version":ENGINE_VERSION,"rules_version":rules,"rule_source_hashes":hashes,
              "dictionary_version":dictionaries.version+"|ncbi:"+gene_index.manifest["records_sha256"],
              "parser":parser_manifest(parser),"candidate_provider":dict(provider.manifest),
              "clinical_models":dict(clinical.manifest),"gene_dictionary":gene_index.manifest,
              "oracle":metadata.get("_gold_entities") is not None,"offset_unit":"unicode_codepoints",
              "profile":metadata.get("profile","local"),"nutrition_accuracy":"unmeasured",
              "review_time_saved":"unmeasured","triage_scores_are_probabilities":False,
              "artifact_identity":metadata.get("_artifact_identity")}
    manifest["external_response_sha256"]=metadata.get("_external_response_sha256")
    spans=[span for s in annotated for span in s.get("pico_spans",[])]
    diagnostics={"gene_candidates":sum(m["kind"]=="gene" for m in mentions),
                 "unresolved_genes":sum(m["kind"]=="gene" and not m.get("normalized_id") for m in mentions),
                 "candidate_events":sum(e["status"]=="candidate" for e in events),
                 "source_events":len(events),"planned_sentences":sum(s["assertion_state"]=="planned" for s in annotated)}
    families=[{"id":m.group(),"start":m.start(),"end":m.end(),"text":m.group()} for m in re.finditer(r"\bNCT\d{8}\b",text)]
    pairs=[]
    for sentence in sentences:
        local=[m for m in molecular if sentence["start"]<=m["start"] and m["end"]<=sentence["end"]]
        for gene in [m for m in local if m["kind"]=="gene"]:
            for chemical in [m for m in local if m["kind"] in {"component","nutrient"}]:
                pairs.append([{k:chemical[k] for k in ("start","end","text")},
                              {k:gene[k] for k in ("start","end","text")}])
    return {**base,"engine_version":ENGINE_VERSION,"rules_version":rules,"dictionary_version":manifest["dictionary_version"],
            "manifest":manifest,"mentions":molecular,"assertions":assertions,"events":events,
            "summary":summary(annotated,events),"sentence_roles":annotated,"annotated_spans":spans,
            "abstentions":base["abstentions"]+[e for e in events if e["status"] in {"abstained","needs_review"}],
            "diagnostics":diagnostics,"candidate_pairs":pairs,"study_families":families,"abbreviations":definitions,
            "duplicate_events_grouped":len(base["events"])-len(events),"status":"complete","processing_status":"complete"}
