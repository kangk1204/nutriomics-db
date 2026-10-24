"""Reconcile the full official search membership, including failures and retained legacy PMIDs."""
import json,hashlib,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from nutri_literature.settings import Settings
from nutri_literature.db import connect,dumps,now


def audit(settings):
    with connect(settings) as c:
        run=None
        for candidate in c.execute("SELECT * FROM runs WHERE kind IN ('collect','reconcile') AND status='completed' ORDER BY started_at DESC"):
            entries=json.loads(candidate['detail']).get('topics',{})
            if len(entries)==6 and all(Path(v['manifest']).exists() and (lambda m:m.get('lower')=='2010-01-01' and m.get('date_field')=='PDAT')(json.loads(Path(v['manifest']).read_text())) for v in entries.values()):
                run=candidate;break
        if not run:raise ValueError('No completed collection run')
        detail=json.loads(run['detail']);expected=set();topics={};hashes={}
        for topic,entry in detail['topics'].items():
            path=Path(entry['manifest']);value=json.loads(path.read_text());ids=set(value['pmids'])
            if not value['complete'] or len(ids)!=value['selected'] or value['returned']!=value['selected']:raise ValueError('Incomplete search: '+topic)
            expected.update(ids);topics[topic]={'returned':value['returned'],'unique_PMIDs':len(ids),'start_date':value['lower'],'end_date':value['upper'],'query_version':value['query_version']}
            hashes[path.name]=hashlib.sha256(path.read_bytes()).hexdigest()
        actual={r[0] for r in c.execute('SELECT pmid FROM papers')};missing=sorted(expected-actual)
        memberships={r[0] for r in c.execute('SELECT DISTINCT p.pmid FROM papers p JOIN paper_topics t USING(paper_id) JOIN topics q ON q.topic_id=t.topic_id AND q.query_version=t.query_version')}
        result={'audited_at':now(),'run_id':run['run_id'],'official_unique_PMIDs':len(expected),'stored_unique_PMIDs':len(actual),
                'missing_PMIDs':missing,'missing_current_query_membership':sorted(expected-memberships),'retained_outside_current_search':sorted(actual-expected),
                'search_manifest_sha256':hashes,'PMID_union_sha256':hashlib.sha256(dumps(sorted(expected)).encode()).hexdigest(),'topics':topics,
                'collection_complete':not missing and not (expected-memberships),'processing_complete':not c.execute("SELECT 1 FROM queue WHERE status IN ('pending','failed','running') LIMIT 1").fetchone(),
                'note':'Official PDAT search membership and displayed journal publication year may differ.'}
    target=settings.home/'collection-reconciliation.json';temporary=target.with_suffix('.part');temporary.write_text(dumps(result));temporary.replace(target);return result


if __name__=='__main__':print(dumps(audit(Settings.from_env())))
