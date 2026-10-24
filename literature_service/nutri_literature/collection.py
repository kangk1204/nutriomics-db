"""Official API retrieval. PubMed parser adapted from existing evidence collector.

Search completeness is mandatory; metadata checkpoints do not depend on OA or extraction.
"""

from datetime import date, datetime, timedelta
import calendar
import json
import os
import re
import time
from zoneinfo import ZoneInfo
import requests
from defusedxml import ElementTree as ET
from .db import connect, digest, dumps, enqueue, finish_run, now, save_source, start_run, uid

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/"


class ApiClient:
    def __init__(self, settings):
        self.settings = settings
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "NutriOmics-literature/0.1 source-grounded-research"
        self.last = 0.0

    def request(self, endpoint, params=None, post=False, limit=50 * 1024 * 1024):
        # A single collector is protected by a CLI file lock. 1 rps leaves shared-IP headroom.
        for attempt in range(5):
            time.sleep(max(0, 1.0 - (time.monotonic() - self.last)))
            self.last = time.monotonic()
            try:
                with self.session.request(
                    "POST" if post else "GET",
                    endpoint,
                    data=params if post else None,
                    params=None if post else params,
                    timeout=(15, 60),
                    stream=True,
                ) as response:
                    if response.status_code in {429, 500, 502, 503, 504}:
                        if attempt == 4:
                            raise RuntimeError(f"API transient status {response.status_code}")
                        delay = response.headers.get("Retry-After", "")
                        time.sleep(min(60, int(delay)) if delay.isdecimal() else 2**attempt)
                        continue
                    if response.status_code != 200:
                        raise RuntimeError(f"API status {response.status_code} at {endpoint.split('?')[0]}")
                    chunks = []
                    size = 0
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > limit:
                            raise RuntimeError("API response exceeds size limit")
                        chunks.append(chunk)
                    return b"".join(chunks)
            except (requests.Timeout, requests.ConnectionError) as error:
                if attempt == 4:
                    raise RuntimeError(f"API network failure: {type(error).__name__}") from None
                time.sleep(2**attempt)
        raise RuntimeError("API retries exhausted")

    def pubmed(self, utility, params, post=False):
        params = {"db": "pubmed", "tool": "nutriomics_literature", **params}
        if os.environ.get("NCBI_EMAIL"):
            params["email"] = os.environ["NCBI_EMAIL"]
        if os.environ.get("NCBI_API_KEY"):
            params["api_key"] = os.environ["NCBI_API_KEY"]
        return self.request(EUTILS + utility, params, post)


def frozen_raw(settings, kind, content, suffix):
    path = settings.raw / kind / (digest(content) + suffix)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if digest(path.read_bytes()) != digest(content):
            raise RuntimeError("Source integrity failure")
    else:
        temporary = path.with_suffix(path.suffix + "." + uid() + ".tmp")
        temporary.write_bytes(content)
        temporary.replace(path)
    return path


def element_text(element):
    return "".join(element.itertext()) if element is not None else ""


def parse_pubmed(content):
    root = ET.fromstring(content)
    if root.tag != "PubmedArticleSet":
        raise ValueError("Expected PubmedArticleSet")
    records = []
    for article in root:
        if article.tag == "DeleteCitation":
            continue
        book = article.tag == "PubmedBookArticle"
        if article.tag not in {"PubmedArticle", "PubmedBookArticle"}:
            raise ValueError("Unexpected PubMed article element")
        citation = article.find("BookDocument" if book else "MedlineCitation")
        if citation is None:
            raise ValueError("Missing citation")
        body = citation if book else citation.find("Article")
        if body is None:
            raise ValueError("Missing article body")
        pmid = citation.findtext("PMID")
        if not pmid or not pmid.isdecimal():
            raise ValueError("Invalid PMID")
        ids = {n.get("IdType"): element_text(n) for n in article.findall(".//ArticleId")}
        title = element_text(body.find("ArticleTitle")) or element_text(body.find("Book/BookTitle"))
        text = title
        sections = [{"label": "TITLE", "start": 0, "end": len(title)}]
        abstract = []
        for node in body.findall("Abstract/AbstractText"):
            value = element_text(node)
            start = len(text) + 1
            text += "\n" + value
            label = node.get("NlmCategory") or node.get("Label") or "ABSTRACT"
            sections.append({"label": label, "start": start, "end": len(text)})
            abstract.append((node.get("Label", "") + ": " if node.get("Label") else "") + value)
        types = [element_text(n) for n in body.findall(".//PublicationType")]
        mesh = [element_text(n) for n in citation.findall("MeshHeadingList/MeshHeading/DescriptorName")]
        pub = body.find("Book/PubDate" if book else "Journal/JournalIssue/PubDate")
        pub_date = ""
        if pub is not None:
            year = pub.findtext("Year") or re.search(r"\d{4}", element_text(pub))
            year = year.group() if hasattr(year, "group") else year
            month = pub.findtext("Month", "")
            month = (
                str(list(calendar.month_abbr).index(month[:3])).zfill(2)
                if month[:3] in list(calendar.month_abbr)[1:]
                else month.zfill(2)
                if month.isdecimal()
                else ""
            )
            pub_date = (
                str(year or "")
                + ("-" + month if month else "")
                + ("-" + pub.findtext("Day").zfill(2) if pub.findtext("Day") and month else "")
            )
        corrections = [
            {"type": n.get("RefType"), "pmid": n.findtext("PMID")}
            for n in citation.findall("CommentsCorrectionsList/CommentsCorrections")
        ]
        retracted = "Retracted Publication" in types or any(n["type"] == "RetractionIn" for n in corrections)
        records.append(
            {
                "pmid": pmid,
                "doi": ids.get("doi"),
                "pmcid": ids.get("pmc"),
                "title": title,
                "pub_date": pub_date,
                "abstract": "\n".join(abstract),
                "text": text,
                "sections": sections,
                "publication_types": types,
                "mesh": mesh,
                "corrections": corrections,
                "retracted": retracted,
                "authors": [
                    element_text(n.find("LastName")) + " " + element_text(n.find("ForeName"))
                    for n in body.findall("AuthorList/Author")
                ],
                "element_xml": ET.tostring(article, encoding="utf-8"),
            }
        )
    if len({r["pmid"] for r in records}) != len(records):
        raise ValueError("Duplicate PMID response")
    return records


def save_article(settings, article, topic=None, version=None):
    if topic is not None and version is None:
        raise ValueError("A topic membership requires its query version")
    raw = article["element_xml"]
    path = frozen_raw(settings, "pubmed", raw, ".xml")
    stamp = now()
    with connect(settings) as con:
        old = con.execute("SELECT * FROM papers WHERE pmid=?", (article["pmid"],)).fetchone()
        if old:
            article = {
                **article,
                "doi": article["doi"] or old["doi"],
                "pmcid": article["pmcid"] or old["pmcid"],
            }
        columns = [
            "doi",
            "pmcid",
            "title",
            "pub_date",
            "abstract",
            "publication_types",
            "mesh",
            "authors",
            "retracted",
        ]
        new_values = [
            article[k]
            if k not in ["publication_types", "mesh", "authors", "retracted"]
            else int(article[k])
            if k == "retracted"
            else dumps(article[k])
            for k in columns
        ]
        unchanged = old and all(old[k] == value for k, value in zip(columns, new_values))
        updated = old["updated_at"] if unchanged else stamp
        con.execute(
            "INSERT INTO papers(pmid,doi,pmcid,title,pub_date,abstract,publication_types,mesh,authors,retracted,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(pmid) DO UPDATE SET doi=excluded.doi,pmcid=excluded.pmcid,"
            "title=excluded.title,pub_date=excluded.pub_date,abstract=excluded.abstract,publication_types=excluded.publication_types,"
            "mesh=excluded.mesh,authors=excluded.authors,retracted=excluded.retracted,updated_at=excluded.updated_at",
            (
                article["pmid"],
                article["doi"],
                article["pmcid"],
                article["title"],
                article["pub_date"],
                article["abstract"],
                dumps(article["publication_types"]),
                dumps(article["mesh"]),
                dumps(article["authors"]),
                int(article["retracted"]),
                stamp,
                updated,
            ),
        )
        paper_id = con.execute("SELECT paper_id FROM papers WHERE pmid=?", (article["pmid"],)).fetchone()[0]
        if topic is not None:
            con.execute("INSERT OR IGNORE INTO paper_topics VALUES(?,?,?)", (paper_id, topic, version))
        source_id = save_source(
            con,
            paper_id,
            "pubmed",
            path,
            raw,
            article["text"],
            article["sections"],
            f"https://pubmed.ncbi.nlm.nih.gov/{article['pmid']}/",
        )
        latest = con.execute(
            "SELECT source_id FROM sources WHERE paper_id=? AND kind='fulltext' ORDER BY retrieved_at DESC LIMIT 1",
            (paper_id,),
        ).fetchone()
        active_source = latest[0] if latest else source_id
        if not latest and old and old["current_source_id"]:
            previous = con.execute(
                "SELECT text_hash FROM sources WHERE source_id=?", (old["current_source_id"],)
            ).fetchone()
            if previous and previous[0] == digest(article["text"]):
                # New metadata bytes are archived, while unchanged evidence retains its review identity.
                active_source = old["current_source_id"]
        con.execute("UPDATE papers SET current_source_id=? WHERE paper_id=?", (active_source, paper_id))
        if old is None or old["current_source_id"] != active_source:
            enqueue(con, paper_id, "extract")
        if old is None:
            enqueue(con, paper_id, "oa")
        if article["retracted"]:
            con.execute("UPDATE assertions SET status='retracted' WHERE paper_id=?", (paper_id,))
        if old and old["retracted"] != int(article["retracted"]):
            con.execute(
                "INSERT INTO audit VALUES(?,?,?,?,?)",
                (uid(), "retraction_change", paper_id, dumps(article["corrections"]), stamp),
            )
        return paper_id


def search_all(client, query, lower, upper, date_field, archive):
    term = f'({query}) AND ("{lower.isoformat()}"[{date_field}] : "{upper.isoformat()}"[{date_field}])'
    raw = client.pubmed("esearch.fcgi", {"term": term, "retmode": "json", "retmax": 0, "usehistory": "y"})
    archive(raw)
    first = json.loads(raw).get("esearchresult", {})
    if "count" not in first:
        raise RuntimeError("Missing PubMed search count")
    count = int(first["count"])
    if count > 9999:
        if lower == upper:
            raise RuntimeError("Single-day search exceeds PubMed limit; incomplete run")
        middle = lower + (upper - lower) // 2
        return search_all(client, query, lower, middle, date_field, archive) + search_all(
            client, query, middle + timedelta(days=1), upper, date_field, archive
        )
    ids = []
    for offset in range(0, count, 1000):
        # Query the explicit bounded term: current PubMed rejects history-only '#1'
        # ESearch terms. Detect index/count drift rather than advancing a partial checkpoint.
        raw = client.pubmed(
            "esearch.fcgi", {"term": term, "retmode": "json", "retstart": offset, "retmax": 1000}
        )
        archive(raw)
        result = json.loads(raw).get("esearchresult", {})
        if int(result.get("count", -1)) != count:
            raise RuntimeError("PubMed search count changed during pagination; retry required")
        ids.extend(result.get("idlist", []))
    if len(ids) != count or len(set(ids)) != count:
        raise RuntimeError("PubMed search page count mismatch")
    return ids


def collect(settings, reconcile=False, topic_id=None, client=None):
    client = client or ApiClient(settings)
    run_id = start_run(settings, "reconcile" if reconcile else "collect")
    end = datetime.now(ZoneInfo("Asia/Seoul")).date()
    details = {"topics": {}, "unique_pmids": 0, "engine": "heuristic-v1"}
    all_ids = set()
    failed = []
    with connect(settings) as con:
        topics = [dict(r) for r in con.execute("SELECT * FROM topics WHERE enabled=1")]
    for topic in topics:
        if topic_id and topic["topic_id"] != topic_id:
            continue
        name = topic["topic_id"]
        version = topic["query_version"]
        try:
            with connect(settings) as con:
                state = con.execute(
                    "SELECT watermark FROM topic_state WHERE topic_id=? AND query_version=?", (name, version)
                ).fetchone()
            lower = date.fromisoformat(topic["start_date"])
            field = "PDAT"
            if state and state[0] and not reconcile:
                lower = date.fromisoformat(state[0][:10]) - timedelta(days=7)
                field = "MDAT"
            base = f'({topic["query"]}) AND ("{topic["start_date"]}"[PDAT] : "{end.isoformat()}"[PDAT])'
            artifacts = []

            def archive(raw):
                p = frozen_raw(settings, "search", raw, ".json")
                artifacts.append(str(p))

            ids = sorted(set(search_all(client, base, lower, end, field, archive)), key=int)
            returned = set()
            for offset in range(0, len(ids), 200):
                batch = ids[offset : offset + 200]
                xml = client.pubmed("efetch.fcgi", {"id": ",".join(batch), "retmode": "xml"}, post=True)
                p = frozen_raw(settings, "batches", xml, ".xml")
                artifacts.append(str(p))
                articles = parse_pubmed(xml)
                observed = {r["pmid"] for r in articles}
                if observed != set(batch):
                    raise RuntimeError(
                        f"PubMed fetch incomplete: missing {len(set(batch) - observed)}; unexpected {len(observed - set(batch))}"
                    )
                for article in articles:
                    save_article(settings, article, name, version)
                returned.update(observed)
            ledger = {
                "selected": len(ids),
                "returned": len(returned),
                "complete": True,
                "query_version": version,
                "date_field": field,
                "lower": lower.isoformat(),
                "upper": end.isoformat(),
                "pmids": ids,
                "artifacts": artifacts,
            }
            ledger_path = settings.raw / "manifests" / f"{run_id}-{name}.json"
            ledger_path.parent.mkdir(parents=True, exist_ok=True)
            ledger_path.write_text(dumps(ledger), encoding="utf-8")
            with connect(settings, durable=True) as con:
                removed = []
                if field == "PDAT":
                    # Only the complete full-period search proves absence. Incremental
                    # MDAT windows must retain memberships outside that window.
                    selected = set(ids)
                    for membership in con.execute(
                        "SELECT pt.paper_id,p.pmid FROM paper_topics pt JOIN papers p USING(paper_id) "
                        "WHERE pt.topic_id=? AND pt.query_version=?",
                        (name, version),
                    ).fetchall():
                        if membership["pmid"] in selected:
                            continue
                        con.execute(
                            "DELETE FROM paper_topics WHERE paper_id=? AND topic_id=? AND query_version=?",
                            (membership["paper_id"], name, version),
                        )
                        con.execute(
                            "INSERT INTO audit VALUES(?,?,?,?,?)",
                            (
                                uid(),
                                "topic_membership_removed",
                                membership["paper_id"],
                                dumps(
                                    {
                                        "topic_id": name,
                                        "query_version": version,
                                        "pmid": membership["pmid"],
                                        "run_id": run_id,
                                        "manifest": str(ledger_path),
                                        "reason": "absent_from_complete_full_search",
                                    }
                                ),
                                now(),
                            ),
                        )
                        removed.append(membership["pmid"])
                con.execute(
                    "INSERT INTO topic_state VALUES(?,?,?,?) ON CONFLICT(topic_id,query_version) DO UPDATE SET "
                    "watermark=excluded.watermark,updated_at=excluded.updated_at",
                    (name, version, end.isoformat(), now()),
                )
            details["topics"][name] = {
                "selected": len(ids),
                "returned": len(returned),
                "complete": True,
                "manifest": str(ledger_path),
                "removed_memberships": len(removed),
            }
            all_ids.update(ids)
        except Exception as error:
            failed.append(name)
            details["topics"][name] = {"complete": False, "error": str(error)}
    details["unique_pmids"] = len(all_ids)
    if topic_id and not any(t["topic_id"] == topic_id for t in topics):
        failed.append(topic_id)
    if reconcile and not failed:
        # A paper can stop matching after a metadata correction or retraction. It
        # remains in the library and must still receive monthly status refreshes.
        with connect(settings) as con:
            absent = sorted({row[0] for row in con.execute("SELECT pmid FROM papers")} - all_ids, key=int)
        refreshed = set()
        artifacts = []
        refresh = {"selected": len(absent), "returned": 0, "complete": False, "pmids": absent}
        try:
            for offset in range(0, len(absent), 200):
                batch = absent[offset : offset + 200]
                xml = client.pubmed("efetch.fcgi", {"id": ",".join(batch), "retmode": "xml"}, post=True)
                artifacts.append(str(frozen_raw(settings, "batches", xml, ".xml")))
                articles = parse_pubmed(xml)
                observed = {article["pmid"] for article in articles}
                if observed != set(batch):
                    raise RuntimeError(
                        f"Known library fetch incomplete: missing {len(set(batch) - observed)}; "
                        f"unexpected {len(observed - set(batch))}"
                    )
                for article in articles:
                    save_article(settings, article)
                refreshed.update(observed)
            if refreshed != set(absent):
                raise RuntimeError("Known library PMID set incomplete")
            refresh["complete"] = True
        except Exception as error:
            failed.append("known_library_refresh")
            refresh["error"] = str(error)
        refresh["returned"] = len(refreshed)
        refresh["artifacts"] = artifacts
        refresh_path = settings.raw / "manifests" / f"{run_id}-known-library-refresh.json"
        refresh_path.parent.mkdir(parents=True, exist_ok=True)
        refresh_path.write_text(dumps(refresh), encoding="utf-8")
        details["known_library_refresh"] = {**refresh, "manifest": str(refresh_path)}
    elif reconcile:
        details["known_library_refresh"] = {"complete": False, "skipped_reason": "incomplete_topic_search"}
    finish_run(
        settings,
        run_id,
        "failed" if failed else "completed",
        details,
        "Incomplete topics: " + ",".join(failed) if failed else None,
    )
    if failed:
        raise RuntimeError("Incomplete topics: " + ",".join(failed))
    return {"run_id": run_id, **details}


def parse_jats(content):
    root = ET.fromstring(content)
    blocks = []
    title = root.find(".//article-title")
    if title is not None:
        blocks.append(("TITLE", element_text(title)))
    abstract = root.find(".//abstract")
    if abstract is not None:
        for p in abstract.findall(".//p"):
            blocks.append(("ABSTRACT", element_text(p)))
    body = root.find(".//body")
    if body is not None:

        def walk(node, label="BODY"):
            for child in node:
                tag = child.tag.split("}")[-1]
                if tag == "sec":
                    title = element_text(child.find("title"))
                    walk(child, child.get("sec-type") or title or label)
                elif tag == "p":
                    blocks.append((label, element_text(child)))

        walk(body)
    text = ""
    sections = []
    for label, value in blocks:
        if not value.strip():
            continue
        start = len(text) + (1 if text else 0)
        text += ("\n" if text else "") + value
        sections.append({"label": label, "start": start, "end": len(text)})
    if not text:
        raise ValueError("No JATS text")
    license_nodes = root.findall(".//permissions/license")
    licenses = " ".join(element_text(n) + " " + str(n.attrib) for n in license_nodes)
    return text, sections, licenses


def fetch_oa(settings, paper, client):
    raw = client.request(
        EPMC + "search",
        {"query": f"EXT_ID:{paper['pmid']} AND SRC:MED", "format": "json", "resultType": "core"},
    )
    metadata_path = frozen_raw(settings, "epmc_metadata", raw, ".json")
    rows = json.loads(raw).get("resultList", {}).get("result", [])
    rows = [r for r in rows if r.get("id") == paper["pmid"] and r.get("source") == "MED"]
    if len(rows) != 1:
        return "unavailable"
    info = rows[0]
    pmcid = info.get("pmcid")
    license = info.get("license")
    with connect(settings) as con:
        con.execute(
            "UPDATE papers SET pmcid=COALESCE(?,pmcid),license=? WHERE paper_id=?",
            (pmcid, license, paper["paper_id"]),
        )
        con.execute(
            "INSERT INTO audit VALUES(?,?,?,?,?)",
            (
                uid(),
                "oa_metadata",
                paper["paper_id"],
                dumps(
                    {
                        "raw_path": str(metadata_path),
                        "sha256": digest(raw),
                        "license": license,
                        "pmcid": pmcid,
                    }
                ),
                now(),
            ),
        )
    if not pmcid or info.get("isOpenAccess") != "Y":
        return "unavailable"
    # OA status alone is not sufficient permission. Only explicitly identified reuse licenses.
    eligible = bool(license and re.search(r"(?i)(cc[ -]?(by|0)|creative commons|public domain)", license))
    if not eligible:
        return "license_not_eligible"
    xml = client.request(EPMC + pmcid + "/fullTextXML")
    path = frozen_raw(settings, "fulltext", xml, ".xml")
    text, sections, _xml_license = parse_jats(xml)
    with connect(settings) as con:
        source_id = save_source(
            con,
            paper["paper_id"],
            "fulltext",
            path,
            xml,
            text,
            sections,
            EPMC + pmcid + "/fullTextXML",
            license,
        )
        old = con.execute(
            "SELECT current_source_id,updated_at FROM papers WHERE paper_id=?", (paper["paper_id"],)
        ).fetchone()
        con.execute(
            "UPDATE papers SET pmcid=?,license=?,oa_status=?,current_source_id=?,updated_at=? WHERE paper_id=?",
            (
                pmcid,
                license,
                "available",
                source_id,
                now() if old[0] != source_id else old[1],
                paper["paper_id"],
            ),
        )
        if old[0] != source_id:
            enqueue(con, paper["paper_id"], "extract")
    return "available"
