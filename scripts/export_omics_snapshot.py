"""Export one analysis from an existing SQLite database with a read-only consistent transaction."""
import argparse,csv,hashlib,json,sqlite3
from contextlib import closing
from pathlib import Path
def export(database,out,accession,analysis_version):
    database=Path(database).resolve();out=Path(out)
    if not database.is_file():raise FileNotFoundError(database)
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as con:
        con.row_factory=sqlite3.Row;con.execute('PRAGMA query_only=ON');con.execute('BEGIN')
        runs=[dict(r) for r in con.execute('SELECT * FROM analysis_run WHERE series_accession=? AND analysis_version=?',(accession,analysis_version))]
        if len(runs)!=1 or runs[0]['status']!='completed':raise ValueError('Exactly one completed frozen analysis required')
        cur=con.execute('SELECT * FROM study_effect WHERE series_accession=? AND analysis_version=? ORDER BY id',(accession,analysis_version))
        cols=[x[0] for x in cur.description]
        out.mkdir(parents=True,exist_ok=False);n=0;significant=0;missing_q=0;contrasts={}
        with (out/'effects.csv').open('x',encoding='utf-8',newline='') as f:
            w=csv.writer(f);w.writerow(cols)
            for row in cur:
                w.writerow(tuple(row));n+=1;contrasts[row['contrast']]=contrasts.get(row['contrast'],0)+1
                q=row['q_value'];missing_q+=q is None;significant+=q is not None and q<.05
        if not n:raise ValueError('No effect rows; export incomplete')
        result={'status':'EXPORTED_NOT_REANALYZED','database':str(database),'snapshot_policy':'SQLite read-only transaction; DB hash deliberately omitted for a live/WAL DB','accession':accession,'analysis_version':analysis_version,'analysis_run':runs[0],'effect_rows':n,'contrast_counts':contrasts,'q_lt_0_05':significant,'missing_q':missing_q,'effects_sha256':hashlib.sha256((out/'effects.csv').read_bytes()).hexdigest(),'limitations':['Export does not independently reproduce raw omics preprocessing or model fitting','Live transaction snapshot contains committed rows only']}
        (out/'export_manifest.json').write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding='utf-8');return result
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--database',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--accession',required=True);p.add_argument('--analysis-version',required=True);a=p.parse_args();print(json.dumps(export(a.database,a.output,a.accession,a.analysis_version),ensure_ascii=False,indent=2))
