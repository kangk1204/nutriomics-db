"""Read-only literature API bound to an explicitly selected immutable snapshot."""
from __future__ import annotations

from contextlib import contextmanager
import csv
import html
import io
import json
from pathlib import Path
import re
import sqlite3
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from .literature_snapshot import filehash

router = APIRouter(prefix="/api/v1/explorer/literature", tags=["Literature"])
ProcessingStatus = Literal['completed','pending','running','waiting','failed','cancelled','not_processed']


@contextmanager
def database(request):
    path = getattr(request.app.state, "literature_database", None)
    if path is None:
        raise HTTPException(503, "문헌 snapshot이 아직 연결되지 않았습니다.")
    con = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA temp_store=MEMORY")
    try:
        yield con
    finally:
        con.close()


def metadata(con):
    return {r[0]: json.loads(r[1]) for r in con.execute("SELECT key,value FROM meta")}


def stored_benchmarks(con):
    """Present measured results without publishing internal experiment targets."""
    return [{key: value for key, value in json.loads(row[0]).items() if key != 'targets'}
            for row in con.execute("SELECT payload_json FROM benchmarks ORDER BY arm")]


def corpus_filter(where,args,*,alias,corpus,query_version):
    if corpus or query_version:
        clause='EXISTS(SELECT 1 FROM corpus_memberships cm WHERE cm.pmid='+alias+'.pmid'
        for key,value in (('corpus_id',corpus),('query_version',query_version)):
            if value:clause+=' AND cm.'+key+'=?';args.append(value)
        where.append(clause+')')


def processing_filter(where,args,*,engine,status):
    if status=='not_processed':
        where.append('NOT EXISTS(SELECT 1 FROM paper_processing ps WHERE ps.pmid=p.pmid AND ps.engine=?)');args.append(engine)
    elif status:
        where.append('EXISTS(SELECT 1 FROM paper_processing ps WHERE ps.pmid=p.pmid AND ps.engine=? AND ps.status=?)');args.extend([engine,status])


def paper(row, engine, *, detail=False):
    value = dict(row)
    value["topics"] = json.loads(value.pop("topics_json"))
    summaries = json.loads(value.pop("summaries_json"))
    value["summary"] = summaries.get(engine)
    value["available_engines"] = sorted(summaries)
    value["metadata"] = json.loads(value.pop("metadata_json"))
    value["engine"] = engine
    if not detail:
        value.pop("source_text", None)
        if value.get("abstract"):
            value["abstract_preview"] = value.pop("abstract")[:500]
    return value


@router.get("/stats")
def stats(request: Request):
    if getattr(request.app.state, "literature_database", None) is None:
        return {"status": "not_connected", "counts": {}, "capabilities": {"management": False}}
    with database(request) as con:
        value = metadata(con)
        value.update(status="available", scope="public" if value["public"] else "internal",
                     benchmarks=stored_benchmarks(con),
                     capabilities={"management": getattr(request.app.state, "literature_management", False)},
                     database_url=__import__('os').environ.get('NUTRIOMICS_DATABASE_BROWSER_URL','/explorer/'))
        return value


@router.get("/papers")
def papers(request: Request, q: str = Query("", max_length=240), topic: str | None = None,
           year_from: int = Query(2010, ge=1500, le=2100), year_to: int = Query(2100, ge=1500, le=2100),
           engine: str = Query("heuristic-v1", max_length=80), fulltext: bool | None = None,
           corpus: str | None = Query(None,max_length=100),query_version: str | None = Query(None,max_length=100),
           processing_status: ProcessingStatus | None = None,
           gene: str | None = Query(None,max_length=120), chemical: str | None = Query(None,max_length=120),
           species: str | None = Query(None, max_length=80), page: int = Query(1, ge=1),
           page_size: int = Query(50, ge=1, le=200)):
    if year_from > year_to:
        raise HTTPException(422, "시작 연도는 종료 연도보다 클 수 없습니다.")
    where = ["(p.year BETWEEN ? AND ? OR p.year IS NULL)"];args = [year_from, year_to]
    corpus_filter(where,args,alias='p',corpus=corpus,query_version=query_version)
    processing_filter(where,args,engine=engine,status=processing_status)
    words = re.findall(r"[\wα-ωΑ-Ω-]+", q, flags=re.UNICODE)[:12]
    if words:
        expression = " AND ".join('"' + w.replace('"', '""') + '"' for w in words)
        where.append("p.pmid IN (SELECT pmid FROM papers_fts WHERE papers_fts MATCH ?)")
        args.append(expression)
    if topic:
        where.append("EXISTS(SELECT 1 FROM json_each(p.topics_json) WHERE value=?)");args.append(topic)
    if fulltext is not None:
        where.append("p.oa_status" + ("=" if fulltext else "!=") + "'available'")
    if species:
        where.append("p.organism=?");args.append(species)
    for value in (gene,chemical):
        if value:
            where.append('EXISTS(SELECT 1 FROM events e WHERE e.pmid=p.pmid AND e.engine=? AND (e.subject=? OR e.object=?))')
            args.extend([engine,value,value])
    with database(request) as con:
        if (corpus or query_version or processing_status) and not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='corpus_memberships'").fetchone():
            raise HTTPException(422,'이 snapshot은 코퍼스·처리 상태 필터를 지원하지 않습니다. 새 snapshot을 연결하세요.')
        # Small cards keep long OA bodies out of list scans and sort buffers.
        table='paper_cards' if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_cards'").fetchone() else 'papers'
        query = ' FROM '+table+' p WHERE '+ ' AND '.join(where)
        total = con.execute("SELECT count(*)" + query, args).fetchone()[0]
        rows = con.execute("SELECT p.*" + query + " ORDER BY coalesce(p.pub_date,'') DESC,p.pmid DESC LIMIT ? OFFSET ?",
                           [*args, page_size, (page - 1) * page_size]).fetchall()
        return {"release_id": metadata(con)["release_id"], "items": [paper(r, engine) for r in rows],
                "total": total, "page": page, "page_size": page_size,
                "filters":{"engine":engine,"corpus":corpus,"query_version":query_version,"processing_status":processing_status},
                "note": "집계 단위: PMID 기준 문헌"}


@router.get("/papers/{pmid}")
def detail(pmid: str, request: Request, engine: str = Query("heuristic-v1", max_length=80)):
    with database(request) as con:
        row = con.execute("SELECT * FROM papers WHERE pmid=?", (pmid,)).fetchone()
        if row is None:
            raise HTTPException(404, "해당 PMID가 이 snapshot에 없습니다.")
        extra={}
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='corpus_memberships'").fetchone():
            extra['corpora']=[dict(r) for r in con.execute('SELECT corpus_id,query_version,topic_id FROM corpus_memberships WHERE pmid=? ORDER BY corpus_id,query_version',(pmid,))]
            state=con.execute('SELECT status FROM paper_processing WHERE pmid=? AND engine=?',(pmid,engine)).fetchone()
            extra['processing_status']=state[0] if state else 'not_processed'
        return {"release_id": metadata(con)["release_id"], **paper(row, engine, detail=True),**extra}


@router.get("/events")
def events(request: Request, pmid: str | None = None, engine: str = Query("heuristic-v1", max_length=80),
           status: str | None = Query(None, max_length=40), entity: str | None = Query(None, max_length=120),
           corpus: str | None = Query(None,max_length=100),query_version: str | None = Query(None,max_length=100),
           page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200)):
    where = ["e.engine=?"];args = [engine]
    corpus_filter(where,args,alias='e',corpus=corpus,query_version=query_version)
    for field, value in (("pmid", pmid), ("status", status)):
        if value:
            where.append("e." + field + "=?");args.append(value)
    if entity:
        where.append("(e.subject=? OR e.object=?)");args.extend([entity, entity])
    query = " FROM events e WHERE " + " AND ".join(where)
    with database(request) as con:
        if (corpus or query_version) and not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='corpus_memberships'").fetchone():
            raise HTTPException(422,'이 snapshot은 코퍼스 필터를 지원하지 않습니다.')
        total = con.execute("SELECT count(*)" + query, args).fetchone()[0]
        rows = con.execute("SELECT e.*" + query + " ORDER BY e.pmid,e.event_id LIMIT ? OFFSET ?", [*args, page_size, (page - 1) * page_size]).fetchall()
        items = []
        for row in rows:
            item = dict(row);item["payload"] = json.loads(item.pop("payload_json"));items.append(item)
        return {"release_id": metadata(con)["release_id"], "items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/benchmarks")
def benchmarks(request: Request):
    with database(request) as con:
        meta=metadata(con)
        return {"release_id": metadata(con)["release_id"], "items": stored_benchmarks(con),
                "engine_availability":meta.get('engine_availability',{}),'diagnostics':meta.get('diagnostics',{}),
                "nutrition_accuracy": "unmeasured", "independent_final_performance": False}


@router.get("/reports")
def reports(request: Request):
    with database(request) as con:
        meta = metadata(con)
        return {"release_id": meta["release_id"], "items": [{"report_id": "current", "generated_at": meta["generated_at"],
                 "formats": ["html", "md", "csv", "json"], "snapshot_bound": True}]}


def report_data(con):
    return {**metadata(con), "benchmarks": stored_benchmarks(con)}


@router.get("/reports/current")
def report(request: Request, format: Literal["html", "md", "csv", "json"] = "html",table:Literal['metrics','inventory']='metrics'):
    with database(request) as con:
        data = report_data(con)
    if format == "json":
        content = json.dumps(data, ensure_ascii=False, indent=2);mime = "application/json"
    else:
        rows = [["구성", "평가 문헌", "정답 관계", "예측 관계", "정탐", "오탐", "누락", "정밀도", "재현율", "F1"]]
        for b in data["benchmarks"]:
            m = b["metrics"];rows.append([b["arm"], b["papers"], b["gold_relations"], b["predicted_relations"], m["tp"], m["fp"], m["fn"],
                                        *[f'{100*m[k]:.2f}%' if m.get(k) is not None else "미정의" for k in ("precision", "recall", "f1")]])
        counts = data["counts"]
        inventory=[['집계 항목','건수'],*[[k,v] for k,v in counts.items()]]
        expansion=data.get('expansion')
        if expansion:
            inventory.extend([['expansion.'+k,expansion[k]] for k in ('expected_papers','enumerated_papers','stored_papers','missing_papers','status','collection_complete')])
        for engine,states in data.get('processing_by_engine',{}).items():
            inventory.extend([['processing.'+engine+'.'+k,v] for k,v in states.items()])
        for section in ('stages','id_changes','label_provenance'):
            for kind,fields in data.get('diagnostics',{}).get(section,{}).items():
                inventory.extend([['diagnostics.'+section+'.'+kind+'.'+k,v] for k,v in fields.items()])
        for engine,availability in data.get('engine_availability',{}).items():
            inventory.extend([['engine.'+engine+'.'+k,v] for k,v in availability.items()])
        summary = f'고유 문헌 {counts["unique_papers"]:,}편 · 공개 본문 확보 {counts["fulltext_papers"]:,}편 · 검수 기록 {counts["review_records"]:,}건'
        if expansion:
            summary+=f' · 확대 검색 {expansion["expected_papers"]:,}편 중 PMID 목록 확인 {expansion["enumerated_papers"]:,}편·현재 저장 {expansion["stored_papers"]:,}편'
        note = "평가 자료: 개발 100편 · 정답 관계 154건 · 동일 집합 반복 비교. 영양 문헌 성능 평가: 예정."
        if format == "csv":
            stream = io.StringIO();writer = csv.writer(stream);writer.writerows(inventory if table=='inventory' else rows);content = "\ufeff" + stream.getvalue();mime = "text/csv"
        elif format == "md":
            content = "# NutriOmics 문헌·모델 비교\n\n" + summary + "\n\n" + "\n".join("| " + " | ".join(map(str, r)) + " |" for r in [rows[0], ["---"] * len(rows[0]), *rows[1:]]) + "\n\n" + note + "\n\n자료 버전: " + data["release_id"]+'\n\n'+ '\n'.join('| '+' | '.join(map(str,r))+' |' for r in [inventory[0],['---','---'],*inventory[1:]])
            mime = "text/markdown"
        else:
            head = "".join("<th>" + html.escape(str(v)) + "</th>" for v in rows[0])
            body = "".join("<tr>" + "".join("<td>" + html.escape(str(v)) + "</td>" for v in r) + "</tr>" for r in rows[1:])
            summary+=' · 초록 '+str(counts['papers_with_abstract'])+'편'
            content = '<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>NutriOmics 문헌 보고서</title><style>body{font-family:system-ui,sans-serif;max-width:1100px;margin:48px auto;padding:20px;color:#18332e}table{border-collapse:collapse;width:100%}td,th{padding:10px;border-bottom:1px solid #ccd9d4;text-align:right}th:first-child,td:first-child{text-align:left}h1{font-size:26px}</style><h1>NutriOmics 문헌·모델 비교</h1><p>' + html.escape(summary) + '</p><table><thead><tr>' + head + '</tr></thead><tbody>' + body + '</tbody></table><p>' + html.escape(note) + '</p><p>자료 버전: ' + html.escape(data["release_id"]) + ' · ' + html.escape(data["generated_at"]) + '</p></html>'
            mime = "text/html"
            content=content.replace('</body>', '').replace('</html>','')+'<h2>수집·처리 집계</h2><table>'+''.join('<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in r)+'</tr>' for r in inventory)+'</table><p>문헌 snapshot: '+html.escape(data['literature_snapshot_id'])+' · 부모 과학 DB: '+html.escape(str(data['science_release_id']))+'</p></html>'
    return Response(content, media_type=mime, headers={"Content-Disposition": f'inline; filename="nutriomics-literature.{format}"',
                    "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})


def attach_literature(app, *, public=True, database_path=None):
    import os
    configured = database_path or os.environ.get("NUTRIOMICS_LITERATURE_SNAPSHOT")
    app.state.literature_database = None
    if configured:
        path = Path(configured).resolve()
        if path.is_dir():
            path = path / "literature.sqlite3"
        manifest = json.loads((path.parent / "manifest.json").read_text(encoding="utf8"))
        if bool(manifest["public"]) != bool(public):
            raise ValueError("Public and internal literature snapshots cannot be interchanged")
        if filehash(path) != manifest["database_sha256"]:
            raise ValueError("Literature snapshot hash mismatch")
        app.state.literature_database = path
    app.include_router(router)
