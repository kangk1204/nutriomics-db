"""Local SQLite, short transactions, immutable source/extraction history."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import uuid
from .settings import Settings


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return uuid.uuid4().hex


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def digest(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


SCHEMA = """
CREATE TABLE IF NOT EXISTS topics(topic_id TEXT PRIMARY KEY, name TEXT NOT NULL, query TEXT NOT NULL,
 query_version TEXT NOT NULL, start_date TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS topic_state(topic_id TEXT, query_version TEXT, watermark TEXT,
 updated_at TEXT, PRIMARY KEY(topic_id,query_version));
CREATE TABLE IF NOT EXISTS papers(paper_id INTEGER PRIMARY KEY, pmid TEXT UNIQUE NOT NULL, doi TEXT,
 pmcid TEXT, title TEXT NOT NULL, pub_date TEXT, abstract TEXT NOT NULL DEFAULT '',
 publication_types TEXT NOT NULL DEFAULT '[]', mesh TEXT NOT NULL DEFAULT '[]',
 authors TEXT NOT NULL DEFAULT '[]', organism TEXT NOT NULL DEFAULT 'unknown',
 oa_status TEXT NOT NULL DEFAULT 'pending', license TEXT, retracted INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, current_source_id TEXT);
CREATE INDEX IF NOT EXISTS papers_doi ON papers(doi);
CREATE INDEX IF NOT EXISTS papers_pmcid ON papers(pmcid);
CREATE TABLE IF NOT EXISTS paper_topics(paper_id INTEGER REFERENCES papers(paper_id),
 topic_id TEXT REFERENCES topics(topic_id), query_version TEXT NOT NULL,
 PRIMARY KEY(paper_id,topic_id,query_version));
CREATE TABLE IF NOT EXISTS sources(source_id TEXT PRIMARY KEY, paper_id INTEGER REFERENCES papers(paper_id),
 kind TEXT NOT NULL, sha256 TEXT NOT NULL, text_hash TEXT NOT NULL, raw_path TEXT NOT NULL,
 text_content TEXT NOT NULL, sections TEXT NOT NULL DEFAULT '[]', url TEXT NOT NULL, license TEXT,
 retrieved_at TEXT NOT NULL, UNIQUE(paper_id,kind,sha256));
CREATE TABLE IF NOT EXISTS extractions(extraction_id TEXT PRIMARY KEY, paper_id INTEGER REFERENCES papers(paper_id),
 source_id TEXT REFERENCES sources(source_id), source_hash TEXT NOT NULL,
 engine_version TEXT NOT NULL, dictionary_version TEXT NOT NULL,
 result_json TEXT NOT NULL, created_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS mentions(mention_id TEXT PRIMARY KEY, extraction_id TEXT REFERENCES extractions(extraction_id),
 paper_id INTEGER REFERENCES papers(paper_id), kind TEXT NOT NULL, text TEXT NOT NULL, normalized_id TEXT,
 start INTEGER NOT NULL, end INTEGER NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS assertions(assertion_id TEXT PRIMARY KEY, extraction_id TEXT REFERENCES extractions(extraction_id),
 paper_id INTEGER REFERENCES papers(paper_id), fingerprint TEXT NOT NULL, subject TEXT NOT NULL,
 predicate TEXT NOT NULL, object TEXT NOT NULL, quote TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL,
 rule_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'candidate', payload TEXT NOT NULL,
 UNIQUE(extraction_id,fingerprint));
CREATE TABLE IF NOT EXISTS accounts(username TEXT PRIMARY KEY, password_hash TEXT NOT NULL, enabled INTEGER DEFAULT 1,
 created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, username TEXT REFERENCES accounts(username),
 expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reviews(review_id TEXT PRIMARY KEY, assertion_id TEXT REFERENCES assertions(assertion_id),
 reviewer TEXT REFERENCES accounts(username), decision TEXT NOT NULL, comment TEXT NOT NULL DEFAULT '',
 corrected_payload TEXT, fingerprint TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
 started_at TEXT NOT NULL, finished_at TEXT, detail TEXT NOT NULL DEFAULT '{}', error TEXT);
CREATE TABLE IF NOT EXISTS queue(queue_id INTEGER PRIMARY KEY, paper_id INTEGER REFERENCES papers(paper_id),
 stage TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER DEFAULT 0,
 due_at TEXT NOT NULL, error TEXT, UNIQUE(paper_id,stage));
CREATE TABLE IF NOT EXISTS jobs(job_key TEXT PRIMARY KEY, kind TEXT NOT NULL, due_at TEXT NOT NULL,
 status TEXT NOT NULL, started_at TEXT, finished_at TEXT, attempts INTEGER NOT NULL DEFAULT 0, error TEXT);
CREATE TABLE IF NOT EXISTS reports(report_id TEXT PRIMARY KEY, generated_at TEXT NOT NULL,
 period_start TEXT NOT NULL, period_end TEXT NOT NULL, snapshot_hash TEXT NOT NULL,
 snapshot_json TEXT NOT NULL, paths TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit(event_id TEXT PRIMARY KEY, kind TEXT NOT NULL, paper_id INTEGER,
 detail TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS health_checks(observed_at TEXT PRIMARY KEY, api_up INTEGER NOT NULL,
 ui_up INTEGER NOT NULL, detail TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS extractions_paper_active ON extractions(paper_id,active,created_at);
CREATE INDEX IF NOT EXISTS mentions_extraction_kind ON mentions(extraction_id,kind,start);
CREATE INDEX IF NOT EXISTS assertions_extraction ON assertions(extraction_id);
CREATE INDEX IF NOT EXISTS reviews_assertion ON reviews(assertion_id,created_at);
CREATE INDEX IF NOT EXISTS runs_started ON runs(started_at);
CREATE INDEX IF NOT EXISTS queue_pending ON queue(status,stage,due_at);
CREATE INDEX IF NOT EXISTS sources_paper_kind ON sources(paper_id,kind,retrieved_at);
CREATE INDEX IF NOT EXISTS audit_kind ON audit(kind);
CREATE INDEX IF NOT EXISTS extractions_engine_active ON extractions(engine_version,active,paper_id,created_at);
CREATE TABLE IF NOT EXISTS engine_queue(task_id INTEGER PRIMARY KEY,paper_id INTEGER NOT NULL REFERENCES papers(paper_id),
 engine_version TEXT NOT NULL,source_hash TEXT NOT NULL,rules_version TEXT NOT NULL,dictionary_version TEXT NOT NULL,
 artifact_version TEXT NOT NULL DEFAULT 'legacy',
 status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,due_at TEXT NOT NULL,error TEXT,
 UNIQUE(paper_id,engine_version,source_hash,rules_version,dictionary_version,artifact_version));
CREATE INDEX IF NOT EXISTS engine_queue_pending ON engine_queue(engine_version,status,due_at);
CREATE TABLE IF NOT EXISTS engine_profiles(engine_version TEXT PRIMARY KEY,rules_version TEXT NOT NULL,
 dictionary_version TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL,
 artifact_version TEXT NOT NULL DEFAULT 'legacy');
CREATE TABLE IF NOT EXISTS engine_health(observed_at TEXT NOT NULL,engine_version TEXT NOT NULL,
 ok INTEGER NOT NULL,provenance TEXT NOT NULL,detail TEXT NOT NULL,snapshot_sha256 TEXT NOT NULL,
 PRIMARY KEY(observed_at,engine_version));
CREATE TABLE IF NOT EXISTS engine_drills(engine_version TEXT NOT NULL,kind TEXT NOT NULL,
 receipt_json TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(engine_version,kind));
CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts USING fts5(title,abstract,content='papers',content_rowid='paper_id');
CREATE TRIGGER IF NOT EXISTS papers_ai AFTER INSERT ON papers BEGIN
 INSERT INTO papers_fts(rowid,title,abstract) VALUES(new.paper_id,new.title,new.abstract); END;
CREATE TRIGGER IF NOT EXISTS papers_au AFTER UPDATE ON papers BEGIN
 INSERT INTO papers_fts(papers_fts,rowid,title,abstract) VALUES('delete',old.paper_id,old.title,old.abstract);
 INSERT INTO papers_fts(rowid,title,abstract) VALUES(new.paper_id,new.title,new.abstract); END;
"""


READ_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_read_index(
 source_id TEXT PRIMARY KEY REFERENCES sources(source_id) ON DELETE CASCADE,text_digest TEXT);
CREATE TRIGGER IF NOT EXISTS source_read_insert AFTER INSERT ON sources BEGIN
 INSERT OR REPLACE INTO source_read_index(source_id,text_digest) VALUES(new.source_id,NULL);
END;
CREATE TRIGGER IF NOT EXISTS source_read_update AFTER UPDATE OF text_content ON sources BEGIN
 INSERT INTO source_read_index(source_id,text_digest) VALUES(new.source_id,NULL)
 ON CONFLICT(source_id) DO UPDATE SET text_digest=NULL;
END;
CREATE TABLE IF NOT EXISTS extraction_read_index(
 extraction_id TEXT PRIMARY KEY REFERENCES extractions(extraction_id) ON DELETE CASCADE,
 paper_id INTEGER,source_id TEXT,source_hash TEXT,engine_version TEXT,dictionary_version TEXT,created_at TEXT,active INTEGER,
 metadata_json TEXT NOT NULL,summary_json TEXT NOT NULL,
 abstained_count INTEGER NOT NULL DEFAULT 0,background_count INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS withheld_events(
 extraction_id TEXT NOT NULL REFERENCES extractions(extraction_id) ON DELETE CASCADE,
 ordinal INTEGER NOT NULL,event_id TEXT,status TEXT,event_json TEXT NOT NULL,
 PRIMARY KEY(extraction_id,ordinal));
CREATE INDEX IF NOT EXISTS withheld_status ON withheld_events(status,extraction_id,ordinal);
CREATE TABLE IF NOT EXISTS withheld_event_reasons(
 extraction_id TEXT NOT NULL,ordinal INTEGER NOT NULL,reason TEXT NOT NULL,
 PRIMARY KEY(extraction_id,ordinal,reason),
 FOREIGN KEY(extraction_id,ordinal) REFERENCES withheld_events(extraction_id,ordinal) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS withheld_reason ON withheld_event_reasons(reason,extraction_id,ordinal);
CREATE TRIGGER IF NOT EXISTS withheld_reason_insert AFTER INSERT ON withheld_events BEGIN
 INSERT OR IGNORE INTO withheld_event_reasons SELECT new.extraction_id,new.ordinal,value
 FROM json_each(new.event_json,'$.triage_reasons') WHERE type='text';
 INSERT OR IGNORE INTO withheld_event_reasons SELECT new.extraction_id,new.ordinal,value
 FROM json_each(new.event_json,'$.abstention_reasons') WHERE type='text';
END;
"""


def _read_projection_trigger(name, operation):
    """Maintain lossless stored projections for every writer, including SQL edits."""
    return f"""CREATE TRIGGER IF NOT EXISTS {name} AFTER {operation} ON extractions BEGIN
 DELETE FROM withheld_events WHERE extraction_id=new.extraction_id;
 INSERT OR REPLACE INTO extraction_read_index(extraction_id,paper_id,source_id,source_hash,engine_version,dictionary_version,created_at,active,metadata_json,summary_json,abstained_count,background_count)
 SELECT new.extraction_id,new.paper_id,new.source_id,new.source_hash,new.engine_version,new.dictionary_version,new.created_at,new.active,
 CASE WHEN json_valid(new.result_json) THEN json_object(
  'engine_version',json_extract(new.result_json,'$.engine_version'),
  'rules_version',json_extract(new.result_json,'$.rules_version'),
  'manifest',json_extract(new.result_json,'$.manifest'),
  'status',json_extract(new.result_json,'$.status')) ELSE '{{}}' END,
 CASE WHEN json_valid(new.result_json) THEN json_object(
  'engine_version',json_extract(new.result_json,'$.engine_version'),
  'rules_version',json_extract(new.result_json,'$.rules_version'),
  'manifest',json_extract(new.result_json,'$.manifest'),
  'status',json_extract(new.result_json,'$.status'),
  'summary',json_extract(new.result_json,'$.summary'),
  'context',json_extract(new.result_json,'$.context')) ELSE '{{}}' END,
 CASE WHEN json_valid(new.result_json) AND json_type(new.result_json,'$.abstentions')='array'
  THEN json_array_length(new.result_json,'$.abstentions') ELSE 0 END,0;
 INSERT INTO withheld_events(extraction_id,ordinal,event_id,status,event_json)
 SELECT new.extraction_id,CAST(j.key AS INTEGER),
  CASE WHEN json_type(j.value,'$.event_id') IS NULL THEN new.extraction_id||':abstention:'||j.key ELSE json_extract(j.value,'$.event_id') END,
  CASE WHEN json_type(j.value,'$.status') IS NULL THEN 'abstained' ELSE json_extract(j.value,'$.status') END,j.value
 FROM json_each(CASE WHEN json_valid(new.result_json) AND json_type(new.result_json,'$.abstentions')='array'
  THEN json_extract(new.result_json,'$.abstentions') ELSE '[]' END) j WHERE j.type='object';
 INSERT INTO withheld_events(extraction_id,ordinal,event_id,status,event_json)
 SELECT new.extraction_id,
  (SELECT abstained_count FROM extraction_read_index WHERE extraction_id=new.extraction_id)+ROW_NUMBER() OVER(ORDER BY CAST(j.key AS INTEGER))-1,
  CASE WHEN json_type(j.value,'$.event_id') IS NULL THEN new.extraction_id||':abstention:'||
   ((SELECT abstained_count FROM extraction_read_index WHERE extraction_id=new.extraction_id)+ROW_NUMBER() OVER(ORDER BY CAST(j.key AS INTEGER))-1)
   ELSE json_extract(j.value,'$.event_id') END,'background',j.value
 FROM json_each(CASE WHEN json_valid(new.result_json) AND json_type(new.result_json,'$.events')='array'
  THEN json_extract(new.result_json,'$.events') ELSE '[]' END) j
 WHERE j.type='object' AND json_extract(j.value,'$.status')='background';
 UPDATE extraction_read_index SET background_count=(SELECT COUNT(*) FROM withheld_events
  WHERE extraction_id=new.extraction_id AND status='background') WHERE extraction_id=new.extraction_id;
END;"""


def initialize_read_index(con):
    con.executescript(READ_SCHEMA)
    columns = {row[1] for row in con.execute("PRAGMA table_info(extraction_read_index)")}
    migrated = "active" not in columns
    for column, kind in (("paper_id", "INTEGER"), ("source_id", "TEXT"), ("source_hash", "TEXT"),
                         ("engine_version", "TEXT"), ("dictionary_version", "TEXT"), ("created_at", "TEXT"), ("active", "INTEGER")):
        if column not in columns:
            con.execute(f"ALTER TABLE extraction_read_index ADD COLUMN {column} {kind}")
    if migrated:
        con.execute("DROP TRIGGER IF EXISTS extraction_read_insert")
        con.execute("DROP TRIGGER IF EXISTS extraction_read_update")
    con.execute("CREATE INDEX IF NOT EXISTS extraction_read_engine ON extraction_read_index(engine_version,active,paper_id,created_at)")
    con.executescript(_read_projection_trigger("extraction_read_insert", "INSERT"))
    con.executescript(_read_projection_trigger("extraction_read_update", "UPDATE OF result_json"))
    con.executescript("""CREATE TRIGGER IF NOT EXISTS extraction_read_state_update
 AFTER UPDATE OF paper_id,source_id,source_hash,engine_version,dictionary_version,created_at,active ON extractions BEGIN
 UPDATE extraction_read_index SET paper_id=new.paper_id,source_id=new.source_id,source_hash=new.source_hash,
 engine_version=new.engine_version,dictionary_version=new.dictionary_version,created_at=new.created_at,active=new.active
 WHERE extraction_id=new.extraction_id; END;""")
    # A one-time backfill preserves source data, review decisions and timestamps.
    if migrated:
        con.execute("UPDATE extractions SET result_json=result_json")
    else:
        missing=[r[0] for r in con.execute('SELECT e.extraction_id FROM extractions e WHERE NOT EXISTS '
                                         '(SELECT 1 FROM extraction_read_index r WHERE r.extraction_id=e.extraction_id)')]
        con.executemany('UPDATE extractions SET result_json=result_json WHERE extraction_id=?',[(v,) for v in missing])
    # Digest actual stored text, never the claimed hash. All text writes dirty
    # this proof through the trigger, so a stale/corrupt source stays detectable.
    missing=[r[0] for r in con.execute("SELECT s.source_id FROM sources s LEFT JOIN source_read_index sr "
                                     "ON sr.source_id=s.source_id WHERE sr.text_digest IS NULL")]
    for source_id in missing:
        row=con.execute('SELECT source_id,text_content FROM sources WHERE source_id=?',(source_id,)).fetchone()
        con.execute("INSERT INTO source_read_index(source_id,text_digest) VALUES(?,?) "
                    "ON CONFLICT(source_id) DO UPDATE SET text_digest=excluded.text_digest",
                    (row["source_id"], digest(row["text_content"])))


@contextmanager
def connect(settings: Settings, durable=False):
    settings.ensure()
    con = sqlite3.connect(settings.database, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA synchronous=" + ("FULL" if durable else "NORMAL"))
    try:
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def initialize(settings):
    with connect(settings) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)
        # Upgrade early v2 development databases without dropping their task history.
        if 'artifact_version' not in {r[1] for r in con.execute('PRAGMA table_info(engine_profiles)')}:
            con.execute("ALTER TABLE engine_profiles ADD COLUMN artifact_version TEXT NOT NULL DEFAULT 'legacy'")
        if 'artifact_version' not in {r[1] for r in con.execute('PRAGMA table_info(engine_queue)')}:
            con.execute('ALTER TABLE engine_queue RENAME TO engine_queue_legacy')
            con.execute('DROP INDEX IF EXISTS engine_queue_pending')
            con.executescript(SCHEMA)
            con.execute("INSERT INTO engine_queue(task_id,paper_id,engine_version,source_hash,rules_version,"
                        "dictionary_version,status,attempts,due_at,error) SELECT task_id,paper_id,engine_version,"
                        "source_hash,rules_version,dictionary_version,status,attempts,due_at,error FROM engine_queue_legacy")
        initialize_read_index(con)
    sync_topics(settings)


def sync_topics(settings):
    config = json.loads(settings.config.read_text(encoding="utf-8"))
    with connect(settings) as con:
        con.execute("UPDATE topics SET enabled=0")
        for topic in config["topics"]:
            version = digest(dumps({k: topic[k] for k in ["query", "start_date"]}))
            con.execute(
                "INSERT INTO topics VALUES(?,?,?,?,?,?) ON CONFLICT(topic_id) DO UPDATE SET "
                "name=excluded.name,query=excluded.query,query_version=excluded.query_version,"
                "start_date=excluded.start_date,enabled=excluded.enabled",
                (
                    topic["id"],
                    topic["name"],
                    topic["query"],
                    version,
                    topic["start_date"],
                    int(topic.get("enabled", True)),
                ),
            )


def start_run(settings, kind):
    run_id = uid()
    with connect(settings) as con:
        con.execute(
            "INSERT INTO runs(run_id,kind,status,started_at) VALUES(?,?,?,?)",
            (run_id, kind, "running", now()),
        )
    return run_id


def finish_run(settings, run_id, status, detail=None, error=None):
    with connect(settings) as con:
        con.execute(
            "UPDATE runs SET status=?,finished_at=?,detail=?,error=? WHERE run_id=?",
            (status, now(), dumps(detail or {}), error, run_id),
        )


def enqueue(con, paper_id, stage):
    con.execute(
        "INSERT INTO queue(paper_id,stage,status,due_at) VALUES(?,?,'pending',?) "
        "ON CONFLICT(paper_id,stage) DO UPDATE SET status='pending',attempts=0,due_at=excluded.due_at,error=NULL",
        (paper_id, stage, now()),
    )
    if stage == "extract":
        source = con.execute("SELECT s.text_hash FROM papers p JOIN sources s ON s.source_id=p.current_source_id "
                             "WHERE p.paper_id=?", (paper_id,)).fetchone()
        if source:
            for profile in con.execute("SELECT * FROM engine_profiles WHERE enabled=1").fetchall():
                enqueue_engine(con, paper_id, source["text_hash"], profile)


def enqueue_engine(con, paper_id, source_hash, profile, retry=False):
    """Keep source/rule versions as independent durable tasks; completed work stays completed."""
    con.execute("INSERT OR IGNORE INTO engine_queue(paper_id,engine_version,source_hash,rules_version,"
                "dictionary_version,artifact_version,status,due_at) VALUES(?,?,?,?,?,?,'pending',?)",
                (paper_id,profile["engine_version"],source_hash,profile["rules_version"],
                 profile["dictionary_version"],dict(profile).get("artifact_version","legacy"),now()))
    if retry:
        con.execute("UPDATE engine_queue SET status='pending',attempts=0,due_at=?,error=NULL "
                    "WHERE paper_id=? AND engine_version=? AND source_hash=? AND rules_version=? "
                    "AND dictionary_version=? AND artifact_version=? AND status IN ('failed','superseded')",
                    (now(),paper_id,profile["engine_version"],source_hash,profile["rules_version"],
                     profile["dictionary_version"],dict(profile).get("artifact_version","legacy")))


def save_source(con, paper_id, kind, raw_path, raw_bytes, text, sections, url, license=None):
    sha = digest(raw_bytes)
    found = con.execute(
        "SELECT source_id FROM sources WHERE paper_id=? AND kind=? AND sha256=?", (paper_id, kind, sha)
    ).fetchone()
    if found:
        return found["source_id"]
    source_id = uid()
    con.execute(
        "INSERT INTO sources VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            source_id,
            paper_id,
            kind,
            sha,
            digest(text),
            str(raw_path),
            text,
            dumps(sections),
            url,
            license,
            now(),
        ),
    )
    con.execute("UPDATE source_read_index SET text_digest=? WHERE source_id=?", (digest(text), source_id))
    return source_id
