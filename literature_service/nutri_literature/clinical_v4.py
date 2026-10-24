"""Small CPU span/role models, independent assertion-state checks and quantities."""
from decimal import Decimal
import json
from pathlib import Path
import re

from .db import digest
from .nlp_v2 import cpu_inference_lock, ParserUnavailable
from .extraction import _PRIOR, _METHOD, _LIMIT, _NULL
from .summary_v2 import _vectors, _cosine

TOKEN = re.compile(r"\w+(?:[-']\w+)*|[^\w\s]")
PLANNED = re.compile(r"\b(?:will|aims? to|aimed to|to test whether|to determine whether|we hypothesi[sz]e|study protocol|planned|forthcoming|is designed to|will be)\b", re.I)
OBSERVED = re.compile(r"\b(?:found|observed|demonstrated|showed|revealed|reduced|lowered|increased|decreased|improved|inhibited|activated|induced|suppressed|attenuated|stimulated|enhanced|was associated|were associated|did not differ|unchanged|no significant|not significantly)\b", re.I)
STAT = re.compile(r"\b(?:p\s*[<>=≤≥]|confidence interval|CI\s*[:=]|mean difference|hazard ratio|odds ratio)\b", re.I)
NUMBER = re.compile(r"(?<!\w)(\d+(?:\.\d+)?)\s*(mg/kg|μg/kg|ug/kg|mg/day|g/day|μg/day|ug/day|IU/day|mg|μg|ug|ng|g|mM|μM|uM|nmol/L|ng/mL|mmHg|weeks?|months?|days?|hours?|%|IU)(?!\w)", re.I)
P_VALUE = re.compile(r"\bp\s*([<>=≤≥])\s*(0?\.\d+|\d+(?:\.\d+)?(?:e-\d+)?)", re.I)
UNITS = {"mg":"mg", "g":"g", "μg":"ug", "ug":"ug", "ng":"ng", "mg/day":"mg/d",
         "g/day":"g/d", "μg/day":"ug/d", "ug/day":"ug/d", "mg/kg":"mg/kg", "μg/kg":"ug/kg",
         "ug/kg":"ug/kg", "iu":"[IU]", "iu/day":"[IU]/d", "mmhg":"mm[Hg]", "%":"%",
         "nmol/l":"nmol/L", "ng/ml":"ng/mL", "mm":"mmol/L", "μm":"umol/L", "um":"umol/L"}


def token_features(tokens, index):
    value = tokens[index]
    word = value["text"]
    result = {"bias":1.0, "lower":word.casefold(), "suffix3":word[-3:].casefold(),
              "prefix3":word[:3].casefold(), "digit":word.isdigit(), "upper":word.isupper(),
              "title":word.istitle(), "position":str(min(index//10,10))}
    for offset in (-2,-1,1,2):
        j = index+offset
        if 0 <= j < len(tokens):
            result[str(offset)+":lower"] = tokens[j]["text"].casefold()
        else:
            result[str(offset)+":boundary"] = True
    return result


class ClinicalModels:
    def __init__(self, directory):
        self.path = Path(directory)
        self.manifest = {"available":False, "device":"cpu"}
        self.role = self.pico = None

    def load(self):
        if self.role is not None:
            return
        import joblib
        manifest = json.loads((self.path/"manifest.json").read_bytes())
        for filename, sha in manifest["files"].items():
            if digest((self.path/filename).read_bytes()) != sha:
                raise ParserUnavailable("Clinical artifact hash differs: "+filename)
        if manifest.get("training_split") != "train" or manifest.get("test_used_for_selection") is not False:
            raise ParserUnavailable("Clinical model does not have a train-only manifest")
        self.role = joblib.load(self.path/"role.joblib")
        self.pico = joblib.load(self.path/"pico.joblib")
        self.manifest = {k:v for k,v in manifest.items() if k not in {"training_pmids","excluded_pmids","heldout_source_audit"}}
        self.manifest.update(available=True,device="cpu",manifest_sha256=digest((self.path/"manifest.json").read_bytes()),
                             training_articles={k:len(v) for k,v in manifest["training_pmids"].items()})

    def predict(self, sentences):
        self.load()
        result = []
        with cpu_inference_lock():
            labels = self.role.predict([s["text"] for s in sentences]) if sentences else []
            for sentence, label in zip(sentences,labels):
                tokens = [{"text":m.group(),"start":sentence["start"]+m.start(),"end":sentence["start"]+m.end()}
                          for m in TOKEN.finditer(sentence["text"])]
                features = [token_features(tokens,i) for i in range(len(tokens))]
                spans = []
                for kind, model in self.pico.items():
                    tags = model.predict_single(features) if features else []
                    begin = None
                    for i, tag in enumerate(list(tags)+["O"]):
                        if begin is not None and tag != "I":
                            start, end = tokens[begin]["start"],tokens[i-1]["end"]
                            spans.append({"label":kind,"start":start,"end":end,
                                          "text":sentence["text"][start-sentence["start"]:end-sentence["start"]],
                                          "origin":"cpu-crf-v4","status":"candidate"})
                            begin=None
                        if tag in {"B","I"} and begin is None:
                            begin=i
                result.append({**sentence,"rhetorical_role":str(label).casefold(),"pico_spans":spans})
        return result


def assertion_state(sentence, metadata=None):
    text, section = sentence["text"],sentence.get("label","")
    metadata = metadata or {}
    if re.search(r"title|references?|bibliograph|background|introduction",section,re.I) or _PRIOR.search(text):
        return "background"
    if any(re.search(r"review|meta-analysis",str(v),re.I) for v in metadata.get("publication_types",[])):
        return "background"
    # Purpose/future applies to its clause; past observations after a contrast survive.
    clauses = re.split(r";|\b(?:but|however|whereas)\b",text,flags=re.I)
    planned = any(PLANNED.search(c) for c in clauses)
    modal_result=re.compile(r"\b(?:may|might|could)\s+(?:\w+\s+){0,3}(?:increased|decreased|reduced|improved|inhibited|activated)\b",re.I)
    observations = [c for c in clauses if not PLANNED.search(c) and not modal_result.search(c)
                    and (OBSERVED.search(c) or STAT.search(c))]
    if planned and not observations:
        return "planned"
    if re.search(r"methods?|materials?|design|objective",section,re.I):
        return "planned" if planned else "unknown"
    if observations:
        return "observed"
    if re.search(r"\b(?:suggests?|may|might|could|potential)\b",text,re.I):
        return "interpretation"
    return "unknown"


def quantity_spans(sentence):
    values=[]
    for match in NUMBER.finditer(sentence["text"]):
        unit=match[2].casefold()
        original_unit=match[2]
        prefix=sentence["text"][max(0,match.start()-65):match.start()].casefold()
        suffix=sentence["text"][match.end():match.end()+65].casefold()
        role = "timepoint" if re.fullmatch(r"weeks?|months?|days?|hours?",unit) else (
            "food_content" if re.search(r"content|contains?|per serving|per 100",prefix+suffix) else
            "concentration" if re.search(r"serum|plasma|blood|concentration",prefix) or unit in {"nmol/l","ng/ml"} else
            "dose" if re.search(r"dose|administer|supplement|receive|intake|treated|treatment",prefix) else "unknown")
        ucum=UNITS.get(unit, "wk" if unit.startswith("week") else "mo" if unit.startswith("month") else
                       "d" if unit.startswith("day") else "h" if unit.startswith("hour") else None)
        reason=None
        if unit in {"mm","μm","um"}:
            if original_unit in {"mM","μM","uM"}:
                role="concentration"
            elif original_unit in {"mm","μm","um"}:
                role="length"
                ucum="mm" if original_unit=="mm" else "um"
            else:
                role="unknown"
                ucum=None
                reason="ambiguous_unit_case"
        elif unit in {"%","mmhg"}:
            # Outcome percentages and pressure are never inferred as a dose
            # merely because a dosing verb occurs elsewhere in the sentence.
            role="outcome_measurement" if unit=="mmhg" else "unknown"
        value={"start":sentence["start"]+match.start(),"end":sentence["start"]+match.end(),"text":match.group(),
               "value":match[1],"unit_original":match[2],"ucum":ucum,"quantity_role":role,
               "assignment":"unresolved","normalized_value":None,"normalized_unit":None}
        if reason:
            value["hold_reason"]=reason
        if "/kg" in unit:
            value["denominator_meaning"] = "body_weight" if re.search(r"body weight|bw\b",suffix) else (
                "food_mass" if role=="food_content" else "unknown")
        if role in {"dose","food_content"} and unit in {"mg","g","ug","μg"}:
            factor={"g":"1","mg":"0.001","ug":"0.000001","μg":"0.000001"}[unit]
            value.update(normalized_value=str(Decimal(match[1])*Decimal(factor)),normalized_unit="g")
        values.append(value)
    for match in P_VALUE.finditer(sentence["text"]):
        values.append({"start":sentence["start"]+match.start(),"end":sentence["start"]+match.end(),"text":match.group(),
                       "quantity_role":"p_value","operator":match[1],"value":match[2],"assignment":"unresolved"})
    for match in re.finditer(r"\b(?:95\s*%\s*(?:CI|confidence interval)|confidence interval|CI)\s*[:=]?\s*\(?\s*[-−]?\d+(?:\.\d+)?\s*(?:to|[-–,])\s*[-−]?\d+(?:\.\d+)?\s*\)?",sentence["text"],re.I):
        values.append({"start":sentence["start"]+match.start(),"end":sentence["start"]+match.end(),"text":match.group(),
                       "quantity_role":"confidence_interval","assignment":"unresolved"})
    for match in re.finditer(r"\b(?:n\s*=\s*\d+|\d+\s+(?:patients?|participants?|volunteers?|subjects?))\b",sentence["text"],re.I):
        values.append({"start":sentence["start"]+match.start(),"end":sentence["start"]+match.end(),"text":match.group(),
                       "quantity_role":"sample_size","value":re.search(r"\d+",match.group()).group(),
                       "population_scope":"unconfirmed","assignment":"unresolved"})
    return values


def bind_conditions(event, sentence, mentions, unique_event=True):
    quantities=quantity_spans(sentence)
    interventions={m.get("normalized_id") or m["text"] for m in mentions if m["kind"] in {"component","nutrient","food","diet"}}
    conditions={"arm_id":None,"comparator":None,"timepoint":None,"quantities":[],"statistics":[],
                "unassigned_quantities":quantities}
    comparators=[]
    for match in re.finditer(r"\b(?:versus|vs\.?|compared (?:with|to))\s+([^.;,]+)",sentence["text"],re.I):
        lo,hi=sentence["start"]+match.start(1),sentence["start"]+match.end(1)
        available=[m for m in mentions if lo<=m["start"] and m["end"]<=hi]
        if len(available)==1:
            m=available[0]
            comparators.append({"start":m["start"],"end":m["end"],"text":m["text"]})
        elif not available:
            control=re.match(r"(?:the\s+)?(?:placebo|control group|usual care)\b",match[1],re.I)
            if control:
                comparators.append({"start":lo,"end":lo+control.end(),"text":control.group()})
    conditions["comparator"]=comparators[0] if len(comparators)==1 else None
    # A unique endpoint does not prove a unique time/dose when multiple values occur.
    controller=event.get("controller",{})
    controller_id=controller.get("normalized_id") or controller.get("text")
    if unique_event and len(interventions)==1 and controller_id in interventions:
        groups=list(re.finditer(re.escape(controller["text"])+r"\s+(?:group|arm)\b",sentence["text"],re.I))
        if len(groups)==1:
            group=groups[0]
            conditions["arm_id"]="arm:"+re.sub(r"\W+","-",group.group().casefold())
            conditions["arm_evidence"]={"start":sentence["start"]+group.start(),"end":sentence["start"]+group.end(),"text":group.group()}
        for kind in ("dose","timepoint","p_value","confidence_interval"):
            group=[q for q in quantities if q["quantity_role"]==kind]
            if len(group)!=1:
                continue
            if kind=="dose" and min(abs(group[0]["start"]-controller.get("end",-10000)),
                                    abs(controller.get("start",-10000)-group[0]["end"]))>40:
                continue
            q={**group[0],"assignment":"single_local_intervention","subject":event["subject"]}
            if kind=="timepoint":
                conditions["timepoint"]=q
            elif kind in {"p_value","confidence_interval"}:
                conditions["statistics"].append(q)
            else:
                conditions["quantities"].append(q)
        conditions["unassigned_quantities"]=[q for q in quantities if not any(
            q["start"]==v["start"] for v in conditions["quantities"]+conditions["statistics"]+
            ([conditions["timepoint"]] if conditions["timepoint"] else []))]
    return conditions


def summary(sentences, events, lam=0.7):
    method=[s for s in sentences if s["assertion_state"] in {"unknown","planned"} and not re.search(r"title",s["label"],re.I)
            and (s.get("rhetorical_role")=="methods" or _METHOD.search(s["text"]))]
    limits=[s for s in sentences if _LIMIT.search(s["text"]) and s["assertion_state"]!="background"]
    candidates=[s for s in sentences if s["assertion_state"]=="observed" and s not in limits]
    vectors=_vectors(candidates)
    event_keys={}
    for event in events:
        key=(event.get("object"),json.dumps(event.get("conditions",{}),sort_keys=True),event.get("direction"))
        for q in event.get("evidence_quotes",[]):
            event_keys.setdefault((q["start"],q["end"]),set()).add(key)
    chosen=[]
    nulls=[i for i,s in enumerate(candidates) if _NULL.search(s["text"])]
    if nulls:
        chosen=[nulls[0]]
    while len(chosen)<min(4,len(candidates)):
        pool=[i for i in range(len(candidates)) if i not in chosen and not any(candidates[i]["text"]==candidates[j]["text"] for j in chosen)]
        if not pool:
            break
        covered=set().union(*(event_keys.get((candidates[j]["start"],candidates[j]["end"]),set()) for j in chosen))
        def rank(i):
            s=candidates[i]
            novelty=bool(event_keys.get((s["start"],s["end"]),set())-covered)
            relevance=.5+.25*bool(STAT.search(s["text"]))+.25*novelty
            return lam*relevance-(1-lam)*max((_cosine(vectors[i],vectors[j]) for j in chosen),default=0)
        chosen.append(min(pool,key=lambda i:(-rank(i),candidates[i]["start"])))
    clean=lambda s:{k:v for k,v in s.items() if k!="pico_spans"}
    return {"labels":{"method":"연구설계","results":"관측 결과 후보","limitations":"한계"},
            "method":clean(method[0]) if method else "미확인",
            "results":[clean(candidates[i]) for i in sorted(chosen)],
            "limitations":clean(limits[0]) if limits else "미확인", "missing_results":not chosen,
            "status":"extractive_candidate", "results_status":"observed_candidates" if chosen else "results_unavailable",
            "notice":"후보 검수용 베타입니다. 관측 결과 미확인" if not chosen else "원문과 연구 조건을 확인할 관측 결과 후보입니다.",
            "selection":{"method":"assertion-gated-tfidf-mmr","lambda":lam,"null_result_reserved":bool(nulls),
                         "candidate_sentences":len(candidates)}}
