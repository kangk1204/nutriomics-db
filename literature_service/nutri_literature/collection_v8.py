"""Additive, resumable PubMed corpus expansion; no model/API inference calls.

Search membership is frozen before metadata ingestion. A separate durable queue
retains every selected PMID, including failed/deleted citations. Legacy source,
license and extraction queues remain managed by the existing collector.
"""
from contextlib import contextmanager
from datetime import date, timedelta
import json
import os
from pathlib import Path
import re
import sqlite3
import shutil

from .collection import ApiClient, frozen_raw, parse_pubmed, save_article
from .db import connect, digest, dumps, finish_run, now, start_run, uid
from .locking import lock

FORMAT = "nutriomics-v8-expansion/1"
DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config" / "topics-v8.json"
UID_MAX = 2147483647


def atomic_json(path, value, immutable=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = dumps(value)
    if immutable and path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise RuntimeError("Immutable expansion artifact changed: " + str(path))
        return
    temporary = path.with_name(".v8-" + uid()[:12] + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def load_contract(config=DEFAULT_CONFIG):
    payload = json.loads(Path(config).read_text(encoding="utf-8"))
    if not re.fullmatch(r"[A-Za-z0-9_-]+", payload["corpus_id"]):
        raise ValueError("Invalid corpus identifier")
    end = date.fromisoformat(payload["end_date"])
    if not 1 <= int(payload.get("partition_limit", 8000)) <= 8000:
        raise ValueError("partition_limit must be between 1 and 8000")
    seen = set()
    for topic in payload["topics"]:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", topic["id"]) or topic["id"] in seen or not topic["query"].strip():
            raise ValueError("Duplicate topic or empty query")
        seen.add(topic["id"])
        if date.fromisoformat(topic["start_date"]) > end:
            raise ValueError("Topic starts after corpus cutoff")
    contract = {"format": FORMAT, "configuration": payload, "default_engine": "heuristic-v1"}
    return contract, digest(dumps(contract))


def topic_version(topic, end_date):
    return digest(dumps({"query": topic["query"], "start_date": topic["start_date"], "end_date": end_date}))


def registered_topic_id(topic, end_date):
    # New query versions never overwrite old topic definitions or enable a broad
    # query in the legacy daily scheduler.
    return "v8-" + topic["id"] + "-" + topic_version(topic, end_date)[:12]


def _date_middle(lower, upper):
    if lower.year != upper.year:
        return date((lower.year + upper.year) // 2, 12, 31)
    if lower.month != upper.month:
        month = (lower.month + upper.month) // 2
        return date(lower.year, month + 1, 1) - timedelta(days=1)
    return lower + (upper - lower) // 2


class PartitionedSearch:
    """Cache exact bounded queries; never page a >8000 PubMed result set."""

    def __init__(self, settings, client, directory, limit=8000):
        self.settings, self.client = settings, client
        self.directory, self.limit = Path(directory), limit
        self.directory.mkdir(parents=True, exist_ok=True)

    def _response(self, term, offset=0, size=0):
        request = {"term": term, "retmode": "json", "retstart": offset, "retmax": size}
        key = digest(dumps(request))
        receipt_path = self.directory / (key[:16] + ".json")
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            raw = Path(receipt["raw_path"]).read_bytes()
            if receipt["request"] != request or digest(raw) != receipt["sha256"]:
                raise RuntimeError("Search cache integrity failure")
        else:
            raw = self.client.pubmed("esearch.fcgi", request)
            raw_path = frozen_raw(self.settings, "search-v8", raw, ".json")
            receipt = {"request": request, "raw_path": str(raw_path), "sha256": digest(raw)}
            # Validate errors before caching; malformed count is never success.
            self._parse(raw)
            atomic_json(receipt_path, receipt, immutable=True)
        return self._parse(raw), {"receipt": str(receipt_path), "sha256": receipt["sha256"]}

    @staticmethod
    def _parse(raw):
        payload = json.loads(raw)
        result = payload.get("esearchresult", {})
        if payload.get("error") or result.get("errorlist") or "count" not in result:
            raise RuntimeError("Invalid PubMed search response")
        count = int(result["count"])
        if count < 0:
            raise RuntimeError("Invalid PubMed search count")
        return result

    def search(self, query, lower, upper):
        root_term = f'({query}) AND ("{lower.isoformat()}"[PDAT] : "{upper.isoformat()}"[PDAT])'
        return self._node(root_term, lower, upper)

    def _node(self, term, lower, upper, uid_range=None):
        result, source = self._response(term)
        count = int(result["count"])
        sources = [source]
        if count <= self.limit:
            ids = []
            for offset in range(0, count, 1000):
                page, receipt = self._response(term, offset, min(1000, count - offset))
                sources.append(receipt)
                if int(page["count"]) != count:
                    raise RuntimeError("Search count drift; new corpus snapshot required")
                page_ids = page.get("idlist", [])
                if not isinstance(page_ids, list) or any(not str(p).isdecimal() for p in page_ids):
                    raise RuntimeError("Invalid PMID search page")
                ids.extend(str(p) for p in page_ids)
            if len(ids) != count or len(set(ids)) != count:
                raise RuntimeError("PubMed page boundary/count mismatch")
            return sorted(ids, key=int), sources
        if uid_range is not None:
            left_uid, right_uid = uid_range
            if left_uid == right_uid:
                raise RuntimeError("Unsplittable PMID result set")
            middle = (left_uid + right_uid) // 2
            # Strip only the UID suffix constructed here, never alter query text.
            base = term.rsplit(" AND (", 1)[0]
            ranges = [(left_uid, middle), (middle + 1, right_uid)]
            children = [self._node(f"{base} AND ({a}:{b}[UID])", lower, upper, (a, b)) for a, b in ranges]
        elif lower < upper:
            middle = _date_middle(lower, upper)
            prefix = term.rsplit(" AND (", 1)[0]
            children = [self.search(prefix[1:-1], a, b) for a, b in
                        [(lower, middle), (middle + timedelta(days=1), upper)]]
        else:
            # Exceptionally dense publication days are partitioned by numeric UID,
            # rather than silently truncating at PubMed's 10000 result ceiling.
            child = self._node(f"{term} AND (1:{UID_MAX}[UID])", lower, upper, (1, UID_MAX))
            children = [child]
        ids = set()
        for child_ids, receipts in children:
            ids.update(child_ids)
            sources.extend(receipts)
        # Multiple PDAT representations can overlap date partitions. Unique union,
        # not a sum of child counts, must match the frozen parent denominator.
        if len(ids) != count:
            raise RuntimeError("Partition union does not match parent search count")
        return sorted(ids, key=int), sources


@contextmanager
def ledger(directory):
    connection = sqlite3.connect(Path(directory) / "ingest.sqlite3", timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("CREATE TABLE IF NOT EXISTS metadata(pmid TEXT PRIMARY KEY,status TEXT NOT NULL DEFAULT 'pending',"
                       "attempts INTEGER NOT NULL DEFAULT 0,error TEXT,raw_sha256 TEXT,completed_at TEXT)")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def _manifests(settings, contract, home, client):
    manifests = []
    end_date = contract["configuration"]["end_date"]
    for topic in contract["configuration"]["topics"]:
        path = home / "search" / (topic["id"] + ".manifest.json")
        version = topic_version(topic, end_date)
        if path.exists():
            manifest = json.loads(path.read_text(encoding="utf-8"))
            if manifest["query_version"] != version or manifest["pmids_sha256"] != digest(dumps(manifest["pmids"])):
                raise RuntimeError("Frozen PMID manifest integrity failure")
            for artifact in manifest["artifacts"]:
                receipt = json.loads(Path(artifact["receipt"]).read_text(encoding="utf-8"))
                if digest(Path(receipt["raw_path"]).read_bytes()) != artifact["sha256"]:
                    raise RuntimeError("Archived search response integrity failure")
        else:
            # Incomplete topics get a fresh attempt. Never reuse a stale parent
            # count with newer children/pages after PubMed index/count drift.
            # Every failed attempt and its raw responses remain auditable.
            attempts = home / "search" / topic["id"]
            attempts.mkdir(parents=True, exist_ok=True)
            while True:
                attempt = attempts / ("a" + uid()[:7])
                if not attempt.exists():
                    attempt.mkdir()
                    break
            search = PartitionedSearch(settings, client, attempt,
                                       contract["configuration"].get("partition_limit", 8000))
            try:
                ids, artifacts = search.search(topic["query"], date.fromisoformat(topic["start_date"]), date.fromisoformat(end_date))
            except BaseException as exc:
                atomic_json(attempt / "attempt.json", {"status": "failed", "error": type(exc).__name__ + ": " + str(exc),
                                                       "query_version": version, "updated_at": now()}, immutable=True)
                raise
            manifest = {"topic": topic, "query_version": version,
                        "registered_topic_id": registered_topic_id(topic, end_date), "end_date": end_date,
                        "pmids": ids, "selected": len(ids), "pmids_sha256": digest(dumps(ids)),
                        "artifacts": artifacts, "search_complete": True, "frozen_at": now()}
            atomic_json(path, manifest, immutable=True)
            atomic_json(attempt / "attempt.json", {"status": "completed", "query_version": version,
                                                   "selected": len(ids), "updated_at": now()}, immutable=True)
        manifests.append(manifest)
    ids = sorted({p for manifest in manifests for p in manifest["pmids"]}, key=int)
    combined_path = home / "search-manifest.json"
    if not combined_path.exists():
        atomic_json(combined_path, {"format": FORMAT, "corpus_id": contract["configuration"]["corpus_id"],
                    "query_version": digest(dumps(contract)), "expected_papers": len(ids), "pmids": ids,
                    "pmids_sha256": digest(dumps(ids)), "status": "search_complete", "observed_at": now(),
                    "topic_manifests": [str(home / "search" / (m["topic"]["id"] + ".manifest.json")) for m in manifests]}, immutable=True)
    else:
        combined = json.loads(combined_path.read_text(encoding="utf-8"))
        if combined["pmids"] != ids or combined["query_version"] != digest(dumps(contract)):
            raise RuntimeError("Combined search manifest integrity failure")
    return manifests


def _save_state(home, value):
    """Small live counters and an immutable-manifest pointer for the web worker."""
    value = {k: v for k, v in value.items() if k != "missing_pmids"}
    value["pid"] = os.getpid()
    value["manifest"] = str(home / "search-manifest.json")
    value["expected_papers"] = value.get("selected_unique")
    value["processed"] = value.get("metadata_status", {}).get("completed", 0)
    value["failed"] = value.get("metadata_status", {}).get("failed", 0)
    atomic_json(home / "state.json", value)
    atomic_json(home.parent / "state.json", {**value, "experiment_home": str(home)})


def resource_status(settings, min_available_gib=0, min_free_disk_gib=0):
    """Inspect Linux memory reserve without importing model runtimes."""
    available = None
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        available = int(next(line.split()[1] for line in meminfo.read_text().splitlines()
                             if line.startswith("MemAvailable:"))) * 1024
    free = shutil.disk_usage(settings.home).free
    return {"available_memory_bytes": available, "free_disk_bytes": free,
            "ready": (available is None or available >= min_available_gib * 1024 ** 3)
                     and free >= min_free_disk_gib * 1024 ** 3}


def _inventory(settings, home, manifests):
    ids = set()
    membership = {}
    for manifest in manifests:
        ids.update(manifest["pmids"])
        for pmid in manifest["pmids"]:
            membership.setdefault(pmid, []).append((manifest["registered_topic_id"], manifest["query_version"]))
    with connect(settings) as con:
        rows = {row["pmid"]: row["paper_id"] for row in con.execute("SELECT pmid,paper_id FROM papers") if row["pmid"] in ids}
        actual = {(r["paper_id"], r["topic_id"], r["query_version"]) for r in con.execute(
            "SELECT paper_id,topic_id,query_version FROM paper_topics WHERE topic_id LIKE 'v8-%'")}
    missing_memberships = sum((rows[p], topic, version) not in actual for p in rows for topic, version in membership[p])
    statuses = {}
    ledger_path = home / "ingest.sqlite3"
    if ledger_path.exists():
        con = sqlite3.connect("file:" + ledger_path.as_posix() + "?mode=ro", uri=True)
        try:
            statuses = dict(con.execute("SELECT status,count(*) FROM metadata GROUP BY status"))
        finally:
            con.close()
    missing = sorted(ids - rows.keys(), key=int)
    complete = (len(manifests) > 0 and not missing and not missing_memberships
                and statuses.get("completed", 0) == len(ids))
    return {"selected_unique": len(ids), "stored_selected": len(rows), "missing_count": len(missing),
            "missing_pmids": missing, "missing_memberships": missing_memberships, "metadata_status": statuses,
            "metadata_complete": complete, "default_engine": "heuristic-v1",
            "topics": [{"id": m["topic"]["id"], "registered_topic_id": m["registered_topic_id"],
                        "selected": m["selected"], "query_version": m["query_version"]} for m in manifests]}


def inspect(settings, config=DEFAULT_CONFIG, verify_raw=True):
    """Read-only audit/reconciliation. Never opens gold/model/API inference data."""
    contract, fingerprint = load_contract(config)
    home = settings.home / "v8-expansion" / fingerprint
    if not (home / "contract.json").exists():
        return {"contract_sha256": fingerprint, "initialized": False, "metadata_complete": False,
                "reference_counts_are_collected": False}
    if json.loads((home / "contract.json").read_text(encoding="utf-8")) != contract:
        raise RuntimeError("Expansion contract integrity failure")
    manifests = []
    for topic in contract["configuration"]["topics"]:
        path = home / "search" / (topic["id"] + ".manifest.json")
        if not path.exists():
            continue
        m = json.loads(path.read_text(encoding="utf-8"))
        if m["pmids_sha256"] != digest(dumps(m["pmids"])) or m["query_version"] != topic_version(topic, contract["configuration"]["end_date"]):
            raise RuntimeError("Search manifest integrity failure")
        if verify_raw:
            for artifact in m["artifacts"]:
                receipt = json.loads(Path(artifact["receipt"]).read_text(encoding="utf-8"))
                if digest(Path(receipt["raw_path"]).read_bytes()) != artifact["sha256"]:
                    raise RuntimeError("Archived search response integrity failure")
        manifests.append(m)
    result = _inventory(settings, home, manifests)
    all_searched = len(manifests) == len(contract["configuration"]["topics"])
    if all_searched:
        combined = json.loads((home / "search-manifest.json").read_text(encoding="utf-8"))
        ids = sorted({p for m in manifests for p in m["pmids"]}, key=int)
        if (combined["pmids"] != ids or combined["pmids_sha256"] != digest(dumps(ids))
                or combined["expected_papers"] != len(ids) or combined["query_version"] != fingerprint):
            raise RuntimeError("Combined search manifest integrity failure")
    return {**result, "initialized": True, "contract_sha256": fingerprint, "home": str(home),
            "search_complete": all_searched, "metadata_complete": all_searched and result["metadata_complete"],
            "reference_counts_are_collected": False}


def expand(settings, config=DEFAULT_CONFIG, client=None, max_batches=1, batch_size=200, retry_failed=True,
           min_available_gib=0, min_free_disk_gib=0):
    """One bounded execution or continuous ingestion; resume is the same operation.

    Callers must initialize the legacy database once before invoking this module.
    Only the global collection lock is acquired; running OA/extract owners and
    their stage locks are never reset or taken over.
    """
    if not settings.database.exists():
        raise RuntimeError("Initialize the literature database before expansion")
    if not 1 <= batch_size <= 200 or (max_batches is not None and max_batches < 1):
        raise ValueError("Invalid metadata batch limit")
    contract, fingerprint = load_contract(config)
    home = settings.home / "v8-expansion" / fingerprint
    client = client or ApiClient(settings)
    with lock(settings.home / "locks" / "collection.lock"):
        home.mkdir(parents=True, exist_ok=True)
        atomic_json(home / "contract.json", contract, immutable=True)
        run = start_run(settings, "v8-expansion")
        started_at = now()
        try:
            _save_state(home, {"running": True, "stage": "search", "run_id": run,
                              "contract_sha256": fingerprint, "started_at": started_at, "updated_at": now()})
            manifests = _manifests(settings, contract, home, client)
            memberships = {}
            with connect(settings, durable=True) as con:
                for manifest in manifests:
                    topic = manifest["topic"]
                    values = (manifest["registered_topic_id"], topic["name"], topic["query"], manifest["query_version"], topic["start_date"], 0)
                    existing = con.execute("SELECT * FROM topics WHERE topic_id=?", (values[0],)).fetchone()
                    if existing and tuple(existing) != values:
                        raise RuntimeError("Additive topic version collision")
                    con.execute("INSERT OR IGNORE INTO topics VALUES(?,?,?,?,?,?)", values)
                    for pmid in manifest["pmids"]:
                        memberships.setdefault(pmid, []).append((values[0], values[3]))
            with ledger(home) as queue:
                queue.executemany("INSERT OR IGNORE INTO metadata(pmid) VALUES(?)", ((p,) for p in memberships))
                queue.commit()
                # A restored legacy DB may be older than the separate checkpoint.
                # Repair absent papers/memberships through normal metadata replay.
                with connect(settings) as con:
                    papers = dict(con.execute("SELECT pmid,paper_id FROM papers"))
                    links = set(map(tuple, con.execute("SELECT paper_id,topic_id,query_version FROM paper_topics WHERE topic_id LIKE 'v8-%'")))
                repair = [p for p, pairs in memberships.items() if p not in papers or
                          any((papers[p], t, v) not in links for t, v in pairs)]
                queue.executemany("UPDATE metadata SET status='pending',error='Checkpoint ahead of restored metadata/membership' WHERE pmid=? AND status='completed'",
                                  ((p,) for p in repair))
                queue.commit()
                eligible = [r[0] for r in queue.execute("SELECT pmid FROM metadata WHERE status='pending'" +
                            (" OR status='failed'" if retry_failed else "") + " ORDER BY CAST(pmid AS INTEGER)")]
                batches = 0
                waiting_resources = None
                for offset in range(0, len(eligible), batch_size):
                    if max_batches is not None and batches >= max_batches:
                        break
                    resources = resource_status(settings, min_available_gib, min_free_disk_gib)
                    if not resources["ready"]:
                        waiting_resources = resources
                        break
                    batch = eligible[offset: offset + batch_size]
                    queue.executemany("UPDATE metadata SET attempts=attempts+1 WHERE pmid=?", ((p,) for p in batch))
                    queue.commit()
                    observed, error, raw_hash = set(), None, None
                    try:
                        xml = client.pubmed("efetch.fcgi", {"id": ",".join(batch), "retmode": "xml"}, post=True)
                        raw_hash = digest(xml)
                        frozen_raw(settings, "batches-v8", xml, ".xml")
                        articles = parse_pubmed(xml)
                        if any(a["pmid"] not in batch for a in articles):
                            raise RuntimeError("Unexpected PMID in metadata response")
                        for article in articles:
                            # Source/metadata commit precedes durable membership and
                            # the separate queue checkpoint. Crash replay is idempotent.
                            paper_id = save_article(settings, article)
                            with connect(settings, durable=True) as con:
                                con.executemany("INSERT OR IGNORE INTO paper_topics VALUES(?,?,?)",
                                                ((paper_id, t, v) for t, v in memberships[article["pmid"]]))
                            queue.execute("UPDATE metadata SET status='completed',error=NULL,raw_sha256=?,completed_at=? WHERE pmid=?",
                                          (raw_hash, now(), article["pmid"]))
                            queue.commit()
                            observed.add(article["pmid"])
                        if observed != set(batch):
                            error = "Incomplete metadata response; PMID absent (deleted citation or partial fetch)"
                    except Exception as exc:
                        error = type(exc).__name__ + ": " + str(exc)
                    missing = set(batch) - observed
                    queue.executemany("UPDATE metadata SET status='failed',error=?,raw_sha256=? WHERE pmid=?",
                                      ((error, raw_hash, p) for p in missing))
                    queue.commit()
                    batches += 1
                    progress = _inventory(settings, home, manifests)
                    _save_state(home, {**progress, "running": True, "stage": "metadata",
                               "run_id": run, "contract_sha256": fingerprint, "started_at": started_at,
                               "resources": resources, "batches_this_execution": batches, "updated_at": now()})
            result = inspect(settings, config, verify_raw=False)
            status = "completed" if result["metadata_complete"] else "waiting"
            result.update(running=False, stage="waiting-resources" if waiting_resources else status,
                          resources=waiting_resources, run_id=run, started_at=started_at, updated_at=now())
            _save_state(home, result)
            finish_run(settings, run, status, result)
            return result
        except BaseException as exc:
            _save_state(home, {"running": False, "stage": "failed", "run_id": run,
                              "contract_sha256": fingerprint, "started_at": started_at,
                              "error": type(exc).__name__ + ": " + str(exc), "updated_at": now()})
            finish_run(settings, run, "failed", {"contract_sha256": fingerprint}, str(exc))
            raise
