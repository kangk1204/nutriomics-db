"""Immutable, rights-filtered literature projection; original scientific DBs stay read-only."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE papers(pmid TEXT PRIMARY KEY,title TEXT NOT NULL,pub_date TEXT,year INTEGER,
 doi TEXT,pmcid TEXT,oa_status TEXT,retracted INTEGER NOT NULL,topics_json TEXT NOT NULL,
 organism TEXT,abstract TEXT,source_text TEXT,source_sha256 TEXT,rights_status TEXT NOT NULL,
 summaries_json TEXT NOT NULL,metadata_json TEXT NOT NULL);
CREATE INDEX papers_year ON papers(year,pmid);
CREATE TABLE paper_cards AS SELECT * FROM papers WHERE 0;
CREATE UNIQUE INDEX cards_pmid ON paper_cards(pmid);
CREATE INDEX cards_date ON paper_cards(pub_date DESC,pmid DESC);
CREATE INDEX cards_year ON paper_cards(year,pmid);
CREATE TABLE events(event_id TEXT PRIMARY KEY,pmid TEXT NOT NULL REFERENCES papers(pmid),
 engine TEXT NOT NULL,source_sha256 TEXT NOT NULL,predicate TEXT NOT NULL,subject TEXT,
 object TEXT,status TEXT NOT NULL,quote TEXT,start INTEGER,end INTEGER,payload_json TEXT NOT NULL);
CREATE INDEX events_paper_engine ON events(pmid,engine);
CREATE INDEX events_status ON events(status,engine);
CREATE VIRTUAL TABLE papers_fts USING fts5(pmid UNINDEXED,title,abstract);
CREATE TABLE benchmarks(arm TEXT PRIMARY KEY,payload_json TEXT NOT NULL);
CREATE TABLE parent_document_links(pmid TEXT PRIMARY KEY,parent_source_sha256 TEXT,parent_extraction_version TEXT,
 snapshot_source_sha256 TEXT,source_hash_matches INTEGER NOT NULL);
CREATE TABLE corpus_memberships(pmid TEXT NOT NULL,corpus_id TEXT NOT NULL,query_version TEXT NOT NULL,
 topic_id TEXT NOT NULL DEFAULT '',PRIMARY KEY(pmid,corpus_id,query_version,topic_id));
CREATE INDEX corpus_lookup ON corpus_memberships(corpus_id,query_version,pmid);
CREATE TABLE paper_processing(pmid TEXT NOT NULL,engine TEXT NOT NULL,status TEXT NOT NULL,
 PRIMARY KEY(pmid,engine));
CREATE INDEX processing_lookup ON paper_processing(engine,status,pmid);
"""

V8_ENGINE = 'nutriomics-evidence-v8'


def _manifest_input(value):
    """Freeze explicit inputs; never discover mutable/private runtime files in API code."""
    if value is None:
        return None, None
    raw = value if isinstance(value, dict) else json.loads(Path(value).read_text(encoding='utf8'))
    if not isinstance(raw, dict):
        raise ValueError('Snapshot audit input must be an object')
    # Copy now so both the digest and its projection refer to the same input.
    raw = json.loads(canonical(raw))
    return raw, sha(canonical(raw))


def _count_fields(value):
    """Only named aggregate counts cross this boundary, never strings or vector arrays."""
    allowed = {'total','eligible','candidates','selected','rejected','held','missing','failed','completed',
               'running','waiting','pending','tp','fp','fn','kept','changed','corrected','damaged',
               'new_links','withdrawn','nil','retrieval_failed','valid_mentions','gold_relations',
               'public_gold','weak_labels','unlabeled','documents','mentions','retained','lost','added'}
    if not isinstance(value, dict):
        return {}
    return {k:v for k,v in value.items() if k in allowed and type(v) is int and v >= 0}


def _diagnostics(value):
    if value is None:
        return {'status':'unavailable','stages':{},'id_changes':{},'label_provenance':{}}
    result = {'status':value.get('status') if value.get('status') in
              {'not_started','waiting','running','completed','failed','unavailable'} else 'unavailable'}
    for section in ('stages','id_changes','label_provenance'):
        records = value.get(section,{})
        result[section] = {k:_count_fields(v) for k,v in records.items()
                           if re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}',k)} if isinstance(records,dict) else {}
    return result


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha(value):
    return hashlib.sha256(value.encode("utf8") if isinstance(value, str) else value).hexdigest()


def filehash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def redistribution_allowed(license_value):
    """Explicit open licenses only. Missing/unknown abstract rights are withheld."""
    value = re.sub(r"[\s_]+", " ", str(license_value or "").strip().lower())
    return bool(re.fullmatch(r"cc(?:-| )?(?:0|by(?:-| )?sa|by)(?:[- /]?\d\.\d)?", value)
                or re.fullmatch(r"https?://creativecommons\.org/(?:licenses/by(?:-sa)?/[34]\.0|publicdomain/zero/1\.0)/?", value))


def _json(value, default):
    return json.loads(value) if value else default


def public_payload(value):
    if isinstance(value, dict):
        return {k: public_payload(v) for k, v in value.items()
                if k not in {"raw_path", "model_path", "artifact_path", "access_token", "password_hash", "username", "reviewer"}}
    if isinstance(value, list):
        return [public_payload(v) for v in value]
    if isinstance(value, str) and re.match(r"^(?:/home/|/Users/|[A-Za-z]:[\\/])", value):
        return None
    return value


def _span(source, item):
    a, b, text = item.get("start"), item.get("end"), item.get("quote", item.get("text"))
    return isinstance(a, int) and isinstance(b, int) and 0 <= a < b <= len(source) and source[a:b] == text


def _benchmarks(path, evaluation_path=None, v8_evaluation_path=None):
    if path is None:
        return []
    value = json.loads(Path(path).read_text(encoding="utf8"))
    result = []
    for arm, row in value["arms"].items():
        m = row["metrics"]
        if m["tp"] + m["fn"] != 154:
            raise ValueError("Benchmark gold denominator changed")
        result.append({"arm": arm, "papers": 100, "gold_relations": 154, "metrics": m,
                       "predicted_relations": m["tp"] + m["fp"], "gold_sha256": value["gold_sha256"],
                       "evaluator": "native standard IDs and relation type; direction separate",
                       "independent_final_performance": False, "source_sha256": row["sha256"],
                       "targets": value["targets"], "status": "historical_development_comparison"})
    if evaluation_path is not None:
        evaluated=json.loads(Path(evaluation_path).read_text(encoding='utf8'))
        if evaluated.get('papers')!=100 or evaluated.get('gold_relations')!=154 or evaluated.get('gold_sha256')!=value['gold_sha256']:
            raise ValueError('Candidate evaluation cohort differs from baseline')
        for arm,row in evaluated['scores'].items():
            if arm not in {'v6_gpu_r2','new_identity_only','new_relation_existing_ID_only'}:continue
            m=row['metrics']
            if m['tp']+m['fn']!=154:raise ValueError('Candidate gold denominator changed')
            result.append({'arm':arm,'papers':100,'gold_relations':154,'predicted_relations':m['tp']+m['fp'],
                           'metrics':m,'gold_sha256':value['gold_sha256'],'source_sha256':filehash(evaluation_path),
                           'independent_final_performance':False,'status':'frozen_development_evaluation'})
    if v8_evaluation_path is not None:
        evaluated=json.loads(Path(v8_evaluation_path).read_text(encoding='utf8'))
        if evaluated.get('papers')!=100 or evaluated.get('gold_relations')!=154 or evaluated.get('gold_sha256')!=value['gold_sha256']:
            raise ValueError('v8 evaluation cohort differs from baseline')
        for arm,row in evaluated['scores'].items():
            if arm not in {V8_ENGINE,'v8_identity_only','v8_relation_existing_ID_only'}:continue
            m=row['metrics']
            if any(type(m.get(k)) is not int or m[k]<0 for k in ('tp','fp','fn')) or m['tp']+m['fn']!=154:
                raise ValueError('v8 gold denominator changed')
            expected={'precision':m['tp']/(m['tp']+m['fp']) if m['tp']+m['fp'] else None,
                      'recall':m['tp']/154,'f1':2*m['tp']/(2*m['tp']+m['fp']+m['fn'])}
            if any(m.get(k) is None and v is not None or m.get(k) is not None and (v is None or abs(m[k]-v)>1e-8) for k,v in expected.items()):
                raise ValueError('v8 metrics disagree with native counts')
            result.append({'arm':arm,'papers':100,'gold_relations':154,'predicted_relations':m['tp']+m['fp'],
                           'metrics':m,'gold_sha256':value['gold_sha256'],'source_sha256':filehash(v8_evaluation_path),
                           'independent_final_performance':False,'status':'frozen_development_evaluation'})
    return result


def build_snapshot(source_database, destination, *, release_id, public=True, baseline_reference=None, science_release_id=None, evaluation_path=None,parent_database=None,_connection=None,
                   expansion_manifest=None,diagnostics_manifest=None,v8_evaluation_path=None):
    """Read one SQLite transaction and atomically publish a new, never-overwritten directory."""
    source_database, destination = Path(source_database).resolve(), Path(destination).resolve()
    expansion,expansion_sha=_manifest_input(expansion_manifest)
    diagnostics,diagnostics_sha=_manifest_input(diagnostics_manifest)
    if v8_evaluation_path is not None and baseline_reference is None:
        raise ValueError('v8 evaluation requires the frozen baseline cohort')
    if destination.exists():
        raise FileExistsError("A literature release must be a new directory")
    staging = destination.with_name("." + destination.name + ".building")
    if staging.exists():
        raise FileExistsError("An unfinished build must be inspected before retrying")
    staging.mkdir(parents=True)
    own_connection=_connection is None
    source = _connection or sqlite3.connect(source_database.as_uri() + "?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    source.execute("PRAGMA temp_store=MEMORY")
    source.execute('PRAGMA query_only=ON')
    if own_connection:source.execute("BEGIN")
    elif not source.in_transaction or Path(source.execute('PRAGMA database_list').fetchone()[2]).resolve()!=source_database:
        raise ValueError('Shared snapshot requires a pinned transaction of the declared source DB')
    db = sqlite3.connect(staging / "literature.sqlite3")
    db.executescript(SCHEMA)
    counts = Counter(); engines = {}; corpus = hashlib.sha256()
    try:
        watermarks = [dict(r) for r in source.execute("SELECT t.topic_id,t.start_date,t.query_version,s.watermark,s.updated_at "
                     "FROM topics t LEFT JOIN topic_state s ON s.topic_id=t.topic_id AND s.query_version=t.query_version WHERE t.enabled=1 ORDER BY t.topic_id")]
        memberships = {}
        for r in source.execute("SELECT DISTINCT paper_id,topic_id FROM paper_topics"):
            memberships.setdefault(r[0], set()).add(r[1])
        membership_columns={r[1] for r in source.execute('PRAGMA table_info(paper_topics)')}
        version_expr='pt.query_version' if 'query_version' in membership_columns else 't.query_version'
        db.executemany('INSERT OR IGNORE INTO corpus_memberships VALUES(?,?,?,?)',
                       ((r[0],'topic:'+r[1],r[2],r[1]) for r in source.execute(
                           'SELECT p.pmid,pt.topic_id,'+version_expr+' FROM paper_topics pt JOIN papers p ON p.paper_id=pt.paper_id JOIN topics t ON t.topic_id=pt.topic_id')))
        expansion_pmids=set()
        if expansion is not None:
            if not isinstance(expansion.get('corpus_id'),str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,100}',expansion['corpus_id']):
                raise ValueError('Expansion corpus_id is required')
            if not isinstance(expansion.get('query_version'),str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,100}',expansion['query_version']):
                raise ValueError('Expansion query_version is required')
            if type(expansion.get('expected_papers')) is not int or expansion['expected_papers']<0:
                raise ValueError('Expansion expected_papers must be a nonnegative count')
            ids=expansion.get('pmids',[])
            if not isinstance(ids,list) or any(not isinstance(p,str) or not re.fullmatch(r'\d+',p) for p in ids):
                raise ValueError('Expansion PMIDs must be a list of identifier strings')
            expansion_pmids=set(ids)
            if len(ids)!=len(expansion_pmids) or len(ids)>expansion['expected_papers']:
                raise ValueError('Expansion PMID inventory has duplicates or exceeds search count')
            stored={r[0] for r in source.execute('SELECT pmid FROM papers')}
            db.executemany('INSERT INTO corpus_memberships VALUES(?,?,?,?)',
                           ((p,expansion['corpus_id'],expansion['query_version'],'') for p in sorted(expansion_pmids&stored)))
        processing={}
        states={'pending','running','waiting','failed','cancelled'}
        queue_columns={r[1] for r in source.execute('PRAGMA table_info(queue)')}
        if 'paper_id' in queue_columns:
            for r in source.execute("SELECT p.pmid,q.status FROM queue q JOIN papers p ON p.paper_id=q.paper_id WHERE q.stage='extract'"):
                if r[1] in states:processing[(r[0],'heuristic-v1')]=r[1]
        if source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='engine_queue'").fetchone():
            # Old source versions and superseded artifact tasks cannot mark current results complete.
            for r in source.execute('SELECT p.pmid,q.engine_version,q.status FROM engine_queue q JOIN papers p ON p.paper_id=q.paper_id JOIN sources s ON s.source_id=p.current_source_id WHERE q.source_hash=s.text_hash ORDER BY q.task_id'):
                if r[2] in states:processing[(r[0],r[1])]=r[2]
        for p in source.execute("SELECT * FROM papers ORDER BY pmid"):
            src = source.execute("SELECT * FROM sources WHERE source_id=?", (p["current_source_id"],)).fetchone()
            text = src["text_content"] if src else ""
            text_hash = sha(text) if src else ""
            if src and text_hash != src["text_hash"]:
                raise ValueError("Stored literature source hash mismatch: " + p["pmid"])
            allowed = bool(src and redistribution_allowed(src["license"] or p["license"]))
            visible = not public or allowed
            rights = "declared_open_license" if allowed else "redistribution_unconfirmed"
            summaries = {}; event_count = 0; withheld = 0
            rows = source.execute("SELECT * FROM extractions WHERE paper_id=? AND active=1 AND source_id=? ORDER BY engine_version,created_at",
                                  (p["paper_id"], p["current_source_id"])).fetchall()
            for extraction in rows:
                engine = extraction["engine_version"]
                processing[(p['pmid'],engine)]='completed'
                value = _json(extraction["result_json"], {})
                if visible:
                    summaries[engine] = public_payload(value.get("summary", {})) if public else value.get("summary", {})
                inventory = engines.setdefault(engine, {"processed_papers": 0, "events": 0, "withheld_events": 0})
                inventory["processed_papers"] += 1
                for a in source.execute("SELECT * FROM assertions WHERE extraction_id=? ORDER BY assertion_id", (extraction["extraction_id"],)):
                    event = dict(a)
                    if not _span(text, event):
                        raise ValueError("Literature quote does not match exact source: " + p["pmid"])
                    payload = _json(a["payload"], {})
                    latest=source.execute('SELECT review_id,decision,corrected_payload,fingerprint FROM reviews WHERE assertion_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1',(a['assertion_id'],)).fetchone()
                    verified=bool(latest and latest['decision'] in ('approved','corrected') and latest['fingerprint']==a['fingerprint'] and a['status']=='approved' and not p['retracted'])
                    subject,predicate,object_=a['subject'],a['predicate'],a['object']
                    correction=_json(latest['corrected_payload'],{}) if latest and latest['decision']=='corrected' else None
                    if verified and latest['decision']=='corrected':
                        if not isinstance(correction,dict) or not all(isinstance(correction.get(k),str) and correction[k] for k in ('subject','predicate','object')):verified=False
                        else:subject,predicate,object_=[correction[k] for k in ('subject','predicate','object')]
                    review_cursor=latest['review_id'] if latest else None
                    corpus.update(canonical([a['assertion_id'],a['fingerprint'],a['status'],review_cursor,dict(latest) if latest else None]).encode())
                    event_count += 1
                    if public and not allowed:
                        withheld += 1;inventory["withheld_events"] += 1;continue
                    key = "LIT:" + sha(canonical([p["pmid"], engine, text_hash, extraction["dictionary_version"], a["fingerprint"],review_cursor,a['status'],subject,predicate,object_]))
                    db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                        key, p["pmid"], engine, text_hash, predicate, subject, object_,
                        'retracted' if p['retracted'] else 'needs_review' if a['status']=='approved' and not verified else a["status"], a["quote"], a["start"], a["end"],
                        canonical({"original_assertion_id": a["assertion_id"], "extraction_id": extraction["extraction_id"],
                                   "rule_id": a["rule_id"], "dictionary_version": extraction["dictionary_version"],
                                   "candidate": public_payload(payload) if public else payload, "expert_approved":verified,
                                   "review_cursor":review_cursor,"review_correction":public_payload(correction),
                                   "original_relation":{"subject":a['subject'],'predicate':a['predicate'],'object':a['object']}}),
                    ))
                    inventory["events"] += 1
            topics = sorted(memberships.get(p["paper_id"], []))
            year_match = re.search(r"\b(?:19|20)\d{2}\b", p["pub_date"] or "")
            abstract = p["abstract"] if visible else None
            meta = {"source_url": "https://pubmed.ncbi.nlm.nih.gov/" + p["pmid"] + "/",
                    "license": (src["license"] if src else None) or p["license"],
                    "publication_types": _json(p["publication_types"], []),
                    "event_count_all_engines": event_count, "withheld_events": withheld,
                    "source_kind": src["kind"] if src else None, "internal_text_available": bool(text),
                    "text_visibility": "available" if visible else "withheld",
                    "source_collected_at": src["retrieved_at"] if src else None}
            row = (p["pmid"], p["title"], p["pub_date"], int(year_match.group()) if year_match else None,
                   p["doi"], p["pmcid"], p["oa_status"], p["retracted"], canonical(topics),
                   p["organism"], abstract, text if visible else None, text_hash, rights, canonical(summaries), canonical(meta))
            db.execute("INSERT INTO papers VALUES(" + ",".join(["?"] * len(row)) + ")", row)
            card=list(row);card[10]=(abstract[:500] if abstract else None);card[11]=None
            card[14]=canonical({engine:{} for engine in summaries})
            db.execute('INSERT INTO paper_cards VALUES('+','.join(['?']*len(card))+')',card)
            db.execute("INSERT INTO papers_fts VALUES(?,?,?)", (p["pmid"], p["title"], abstract or ""))
            corpus.update(canonical([p["pmid"], text_hash, p["updated_at"], [r["extraction_id"] for r in rows]]).encode())
            counts["unique_papers"] += 1;counts["papers_with_abstract"] += bool(p["abstract"].strip())
            counts["fulltext_papers"] += p["oa_status"] == "available";counts["retracted_papers"] += bool(p["retracted"])
            counts["candidate_events_all_engines"] += event_count;counts["withheld_events"] += withheld
        db.executemany('INSERT INTO paper_processing VALUES(?,?,?)',((pmid,engine,status) for (pmid,engine),status in processing.items()))
        corpus.update(canonical({'topic_versions':watermarks,'processing':sorted([pmid,engine,status] for (pmid,engine),status in processing.items())}).encode())
        counts["review_records"] = source.execute("SELECT count(*) FROM reviews").fetchone()[0]
        notes_exist=source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='platform_review_notes'").fetchone()
        counts['unconfirmed_review_records']=source.execute('SELECT count(*) FROM platform_review_notes').fetchone()[0] if notes_exist else 0
        counts['all_review_records']=counts['review_records']+counts['unconfirmed_review_records']
        counts["stored_reports"] = source.execute("SELECT count(*) FROM reports").fetchone()[0]
        counts["visible_events"] = db.execute("SELECT count(*) FROM events").fetchone()[0]
        counts['human_approved_visible_events']=db.execute("SELECT count(*) FROM events WHERE json_extract(payload_json,'$.expert_approved')=1").fetchone()[0]
        counts["papers_with_visible_events"] = db.execute("SELECT count(DISTINCT pmid) FROM events").fetchone()[0]
        if parent_database is not None:
            parent=sqlite3.connect(Path(parent_database).resolve().as_uri()+'?mode=ro',uri=True)
            try:
                existing=parent.execute('SELECT pmid,source_sha256,extraction_version FROM candidate_document').fetchall()
                counts['parent_candidate_documents']=len(existing)
                for pmid,source_sha,engine in existing:
                    found=db.execute('SELECT source_sha256 FROM papers WHERE pmid=?',(pmid,)).fetchone()
                    db.execute('INSERT INTO parent_document_links VALUES(?,?,?,?,?)',(pmid,source_sha,engine,found[0] if found else None,int(bool(found) and source_sha==found[0])))
                counts['parent_documents_matched_by_PMID']=db.execute('SELECT count(*) FROM parent_document_links WHERE snapshot_source_sha256 IS NOT NULL').fetchone()[0]
                counts['parent_documents_missing']=len(existing)-counts['parent_documents_matched_by_PMID']
            finally:parent.close()
        queue=[dict(r) for r in source.execute('SELECT stage,status,count(*) AS count FROM queue GROUP BY stage,status')]
        corpus_inventory=[]
        for corpus_id,query_version,n in db.execute('SELECT corpus_id,query_version,count(DISTINCT pmid) FROM corpus_memberships GROUP BY corpus_id,query_version ORDER BY corpus_id,query_version'):
            corpus_inventory.append({'corpus_id':corpus_id,'query_version':query_version,'stored_papers':n})
        expansion_summary=None
        if expansion is not None:
            n=db.execute('SELECT count(DISTINCT pmid) FROM corpus_memberships WHERE corpus_id=? AND query_version=?',
                         (expansion['corpus_id'],expansion['query_version'])).fetchone()[0]
            expected=expansion['expected_papers']
            expansion_summary={'corpus_id':expansion['corpus_id'],'query_version':expansion['query_version'],
                               'expected_papers':expected,'enumerated_papers':len(expansion_pmids),'stored_papers':n,
                               'missing_papers':expected-n,'enumeration_complete':len(expansion_pmids)==expected,
                               'metadata_complete':expansion.get('metadata_complete') is True,
                               'missing_memberships':expansion.get('missing_memberships') if type(expansion.get('missing_memberships')) is int and expansion['missing_memberships']>=0 else None,
                               'collection_complete':len(expansion_pmids)==expected and n==expected and expansion.get('metadata_complete') is True and expansion.get('missing_memberships')==0,
                               'status':expansion.get('status') if expansion.get('status') in {'not_started','waiting','running','completed','failed'} else 'unavailable',
                               'observed_at':expansion.get('observed_at') if isinstance(expansion.get('observed_at'),str) and re.fullmatch(r'[0-9T:.+Z-]{10,40}',expansion['observed_at']) else None,
                               'manifest_sha256':expansion_sha}
            # A claimed completed collector run does not imply exhaustive PMID reconciliation.
            if expansion_summary['status']=='completed' and not expansion_summary['collection_complete']:
                expansion_summary['status']='waiting'
        processing_counts={}
        for engine in sorted(set(engines)|{V8_ENGINE,'heuristic-v1'}):
            grouped={r[0]:r[1] for r in db.execute('SELECT status,count(*) FROM paper_processing WHERE engine=? GROUP BY status',(engine,))}
            grouped['not_processed']=counts['unique_papers']-sum(grouped.values())
            processing_counts[engine]=grouped
        diagnostic_summary=_diagnostics(diagnostics)
        evaluated_arms=_benchmarks(baseline_reference,evaluation_path,v8_evaluation_path)
        v8_evaluated=any(r['arm']==V8_ENGINE for r in evaluated_arms)
        scopes = {"public": public, "source_transaction_sha256": corpus.hexdigest(),
                  "counts": dict(counts), "engines": engines, "collection": {"topics": watermarks},
                  "release_id": release_id, "literature_snapshot_id": release_id,
                  "science_release_id": science_release_id,
                  "generated_at": datetime.now(timezone.utc).isoformat(),
                  "schema_version": "nutriomics-literature-snapshot-v2", "nutrition_accuracy": "unmeasured",
                  "default_engine": "heuristic-v1", "candidate_engine": "relation-repair-v6-gpu-r2",
                  "candidate_engine_evaluation": "completed_development_only" if evaluation_path else "not_completed",
                  "processing_queue":queue,
                  "corpora":corpus_inventory,"expansion":expansion_summary,
                  "processing_by_engine":processing_counts,"diagnostics":diagnostic_summary,
                  "audit_inputs":{"expansion_sha256":expansion_sha,"diagnostics_sha256":diagnostics_sha},
                  "engine_availability":{V8_ENGINE:{"status":'available' if V8_ENGINE in engines else 'unavailable',
                                         "evaluation_status":'completed_development_only' if v8_evaluated else 'not_completed'},
                                         'relation-repair-v6-gpu-r2':{"status":'available' if 'relation-repair-v6-gpu-r2' in engines else 'unavailable',
                                         "evaluation_status":'completed_development_only' if evaluation_path else 'not_completed'}},
                  "count_notes": "PMID documents, event versions, database relations and independent studies are different units."}
        reconciliation=source_database.parent.parent/'collection-reconciliation.json'
        if reconciliation.exists():scopes['collection']['reconciliation']=public_payload(json.loads(reconciliation.read_text(encoding='utf8')))
        for key, value in scopes.items():
            db.execute("INSERT INTO meta VALUES(?,?)", (key, canonical(value)))
        for benchmark in evaluated_arms:
            db.execute("INSERT INTO benchmarks VALUES(?,?)", (benchmark["arm"], canonical(benchmark)))
        db.commit()
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Literature snapshot integrity check failed")
        db.close()
        if own_connection:source.close()
        manifest = {**scopes, "database_sha256": filehash(staging / "literature.sqlite3"),
                    "builder_sha256": filehash(__file__), "source_is_read_only": True,
                    "credentials_exported": False, "sealed_labels_opened": False}
        (staging / "manifest.json").write_text(canonical(manifest), encoding="utf8")
        staging.replace(destination)
        return manifest
    except BaseException:
        db.close()
        if own_connection:source.close()
        raise
