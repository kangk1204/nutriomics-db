"""Nested automatic mentions: every predicted paper is excluded from its NER fit."""
import gc,sqlite3
from .common import *


def retrieval_cache():
    path=HOME/'identity/retrieval-cache.sqlite3';path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path);db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA synchronous=NORMAL')
    db.executescript('CREATE TABLE IF NOT EXISTS cache(k TEXT PRIMARY KEY,metadata TEXT,sha256 TEXT);CREATE TABLE IF NOT EXISTS contract(k TEXT PRIMARY KEY,v TEXT);')
    contract=canonical({'knowledge':filehash(HOME/'knowledge/manifest.json'),'dense':filehash(HOME/'knowledge/dense-manifest.json'),
                        'preparation':filehash(Path(__file__).with_name('preparation.py')),'dictionary':filehash(Path(__file__).with_name('dictionary_mentions.py'))})
    row=db.execute('SELECT v FROM contract WHERE k=?',('source',)).fetchone()
    if row and row[0]!=contract:db.close();raise ValueError('검색 캐시 사전·모델·후보 정의 변경')
    db.execute('INSERT OR IGNORE INTO contract VALUES(?,?)',('source',contract));db.commit();return db


def cached(db,key):
    row=db.execute('SELECT metadata,sha256 FROM cache WHERE k=?',(key,)).fetchone()
    if row:
        if sha(row[0])!=row[1]:raise ValueError('검색 캐시 무결성 오류')
        return json.loads(row[0])
    return None


def cache_put(db,key,metadata):
    blob=canonical(metadata);old=db.execute('SELECT sha256 FROM cache WHERE k=?',(key,)).fetchone()
    if old and old[0]!=sha(blob):raise ValueError('같은 원문·위치의 검색 메타데이터 변경')
    db.execute('INSERT OR IGNORE INTO cache VALUES(?,?,?)',(key,blob,sha(blob)))


def _disjoint(training, held):
    if {d['pmid'] for d in training}&{d['pmid'] for d in held}:
        raise ValueError('ID 자동 후보 생성기의 학습·예측 PMID 중복')
    if {textkey(d['text']) for d in training}&{textkey(d['text']) for d in held}:
        raise ValueError('ID 자동 후보 생성기의 학습·예측 원문 중복')
    if {d['pmcid'] for d in training if d.get('pmcid')}&{d['pmcid'] for d in held if d.get('pmcid')}:
        raise ValueError('ID 자동 후보 생성기의 학습·예측 PMCID 중복')


def ner_model(training):
    from .entities import train
    path=HOME/'identity/auto-ner'/sha(canonical(sorted(d['pmid'] for d in training)))[:20]
    train(training,path)
    return path


def profile(held, training):
    _disjoint(training,held)
    key=sha(canonical([sorted(d['pmid'] for d in training),[(d['pmid'],sha(d['text'])) for d in held]]))[:24]
    directory=HOME/'identity/auto-profiles'/key;receipt=directory/'manifest.json'
    if receipt.exists():
        value=read(receipt)
        if value['knowledge_sha256']!=filehash(HOME/'knowledge/manifest.json') or value['dense_sha256']!=filehash(HOME/'knowledge/dense-manifest.json'):
            raise ValueError('자동 ID 후보의 사전·검색 모델 변경')
        for f in value['files']:
            if filehash(HOME/f['path'])!=f['sha256']:raise ValueError('자동 ID 후보 캐시 변경')
        return value
    from .entities import predict
    path=ner_model(training);raw_path=directory/'ner.json'
    if not raw_path.exists():
        atomic(raw_path,predict([{'pmid':d['pmid'],'text':d['text']} for d in held],path),True)
    raw=read(raw_path)
    from .knowledge import Knowledge
    from .dense import Retriever
    from .dictionary_mentions import extract
    from .preparation import candidate_mentions
    kb=Knowledge();retriever=None;files=[];cache_db=retrieval_cache()
    try:
        for d in held:
            target=directory/('paper-'+d['pmid']+'.json')
            if not target.exists():
                mentions=raw[d['pmid']];seen={(m['kind'],m['start'],m['end']) for m in mentions}
                mentions=mentions+[m for m in extract(d['text'],kb) if (m['kind'],m['start'],m['end']) not in seen]
                values=[];missing=[]
                for m in mentions:
                    cache=sha(canonical([sha(d['text']),m['kind'],m['start'],m['end']]))
                    metadata=cached(cache_db,cache)
                    if metadata is not None:values.append({**metadata,**m})
                    else:missing.append((m,cache))
                if missing:
                    if retriever is None:retriever=Retriever()
                    built=candidate_mentions({'pmid':d['pmid'],'text':d['text']},[v[0] for v in missing],kb,retriever)
                    for row,(_,cache) in zip(built,missing):
                        metadata={k:v for k,v in row.items() if k not in ('ner_score','source')}
                        cache_put(cache_db,cache,metadata);values.append(row)
                    cache_db.commit()
                values.sort(key=lambda m:(m['start'],m['end'],m['kind']))
                if not all(exact(d['text'],m) for m in values):raise ValueError('자동 ID 후보 원문 위치 불일치')
                atomic(target,{'pmid':d['pmid'],'text':d['text'],'source_sha256':sha(d['text']),'mentions':values,'source_only':True},True)
            files.append({'pmid':d['pmid'],'path':str(target.relative_to(HOME)),'sha256':filehash(target)})
        result={'files':files,'NER_training_pmids':sorted(d['pmid'] for d in training),
                'predicted_pmids':sorted(d['pmid'] for d in held),'training_and_prediction_disjoint':True,
                'NER_weights_sha256':filehash(path/'model.safetensors'),
                'knowledge_sha256':filehash(HOME/'knowledge/manifest.json'),
                'dense_sha256':filehash(HOME/'knowledge/dense-manifest.json'),
                'candidate_generation_gold_injection':False}
        atomic(receipt,result,True);log('automatic-id-profile',training=len(training),held=len(held),key=key)
        return result
    finally:
        if retriever is not None:retriever.close()
        cache_db.close();kb.close();gc.collect()


def oof(documents,n=2,salt='r1-id-ranker-oof'):
    folds=foldmap(documents,n,salt);files=[];sources=[]
    for f in range(n):
        training=[d for d in documents if folds[d['pmid']]!=f];held=[d for d in documents if folds[d['pmid']]==f]
        v=profile(held,training);files.extend(v['files']);sources.append(v)
    return {'files':files,'source_profiles':sources,'cross_fitted_automatic_errors':True}


def existing_outer(held):
    # Reused immutable OOF NER profiles have exactly the same five paper groups.
    meta=read(HOME/'identity/ner-oof.json')
    wanted={d['pmid'] for d in held}
    all_ids={d['pmid'] for d in read(HOME/'data/identity-train.json')['documents']}
    if not any(wanted==set(f['held_pmids']) and all_ids-wanted==set(f['training_pmids']) for f in meta['folds']):
        raise ValueError('기존 자동 후보의 외부 fold가 현재 분할과 다름')
    files=[]
    for d in held:
        p=HOME/'prepared/normalization'/('paper-'+d['pmid']+'.json')
        files.append({'pmid':d['pmid'],'path':str(p.relative_to(HOME)),'sha256':filehash(p)})
    return {'files':files,'reused_frozen_outer_NER_only':True,'training_and_prediction_disjoint':True}
