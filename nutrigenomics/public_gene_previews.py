"""One native gene read for public intervention previews, preserving semantics."""
from pathlib import Path
from .explorer_performance import (CACHE,QueryUnavailable,canonical,fingerprint,
    logical_connection_path,read_connection)
from .public_omics_index import _projection


def batch_gene_previews(core_path,contrasts,gene,projection,limit=6):
    from .explorer_scientific import gene_feature_identifiers,_preview_key,_format_effect
    if not contrasts or not Path(core_path).is_file():return None
    identity=fingerprint(core_path);projection_path=logical_connection_path(projection)
    projection_identity=fingerprint(projection_path) if projection_path else None
    prepared=[]
    for contrast in contrasts:
        key=tuple(contrast[k] for k in ('series_accession','contrast','analysis_version'))
        prepared.append((contrast,key,set(gene_feature_identifiers(gene,contrast)),
            _preview_key(identity,projection_identity,contrast,gene,limit)))
    with read_connection(core_path) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='effect_selector'").fetchone():return None
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='public_effect_feature' AND tbl_name='effect'").fetchone():return None
        if [r[2] for r in db.execute('PRAGMA index_info(public_effect_feature)')]!=['feature_type','feature_id']:return None
        try:columns,_=_projection(db)
        except ValueError:return None
        totals={tuple(r[:3]):r[3] for r in db.execute('SELECT series_accession,contrast,analysis_version,sum(total) FROM effect_selector GROUP BY series_accession,contrast,analysis_version')}
        identifiers=sorted(set().union(*(ids for _,_,ids,_ in prepared)))
        rows=[]
        for start in range(0,len(identifiers),400):
            ids=identifiers[start:start+400]
            rows.extend(dict(r) for r in db.execute('SELECT rowid AS _preview_native_rowid,'+columns+" FROM effect INDEXED BY public_effect_feature WHERE feature_type IN ('gene_expression','expression','gene','transcript') AND feature_id IN ("+','.join('?' for _ in ids)+')',ids))
    grouped={}
    for row in rows:
        if any(not isinstance(row.get(k),str) or not row[k] for k in
                ('series_accession','contrast','analysis_version','feature_type','feature_id')):
            raise QueryUnavailable('analysis_key_mismatch')
        key=tuple(row[k] for k in ('series_accession','contrast','analysis_version'))
        grouped.setdefault(key,[]).append(row)
    results={'_batch_sources':(identity,projection_identity)}
    for contrast,key,ids,cache_key in prepared:
        if not ids:results[cache_key]=([],0,'available');continue
        if key not in totals:results[cache_key]=([],None,'not_in_public_release');continue
        selected=sorted((r for r in grouped.get(key,[]) if r['feature_id'] in ids),
            key=lambda r:(r['feature_type'],r['feature_id'],r['_preview_native_rowid']))
        if len(selected)>totals[key]:raise QueryUnavailable('analysis_key_mismatch')
        values=[]
        for row in selected[:limit]:
            value={k:v for k,v in row.items() if k!='_preview_native_rowid'}
            values.append(_format_effect(value,gene))
        results[cache_key]=(values,len(selected),'available')
    if (fingerprint(core_path)!=identity or
            (fingerprint(projection_path) if projection_path else None)!=projection_identity):
        raise QueryUnavailable('source_changed')
    for key,result in results.items():
        if key!='_batch_sources' and result[2]=='available':CACHE.put(key,canonical(result).encode())
    return results
