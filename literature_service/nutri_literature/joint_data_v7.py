"""One source-pair layout for training and inference, task labels kept separate."""
from collections import defaultdict
import json
from pathlib import Path
import re
import zipfile
from .contract_v7 import immutable,sha,canonical
from .text import sentence_spans


def local_pairs(document,entities):
    sentences=sentence_spans(document['text'],document.get('sections',[]))
    pairs=[]
    for s in sentences:
        local=[e for e in entities if s['start']<=e['start']<e['end']<=s['end']]
        chemicals=[e for e in local if e['kind'] in ('component','nutrient')]
        genes=[e for e in local if e['kind']=='gene']
        for a in chemicals:
            for b in genes:
                if a['start']<b['end'] and b['start']<a['end']:
                    continue
                pairs.append({'chemical':a,'gene':b,'sentence':s,'path_spans':[]})
    return pairs


def runtime_document(text,sections,mentions):
    from .nlp_v2 import parser_for
    pairs=dependency_paths(local_pairs({'text':text,'sections':sections},mentions),parser_for({}))
    groups=defaultdict(list)
    for i,pair in enumerate(pairs):
        groups[tuple(pair[r].get('payload',{}).get('mention_cluster_id',
                     r+':'+str(pair[r]['start'])+':'+str(pair[r]['end'])) for r in ('chemical','gene'))].append(i)
    return {'text':text,'task':'bioredirect','pairs':pairs,'bags':[{'indices':indices} for indices in groups.values()]}


def dependency_paths(pairs,parser):
    from .features_v5 import _head
    from .nlp_v2 import validate_tokens
    from collections import deque
    cached={}
    for pair in pairs:
        s=pair['sentence']
        if s['start'] not in cached:
            cached[s['start']]=validate_tokens(parser.parse(s),s)
        tokens=cached[s['start']]
        a,b=_head(tokens,pair['chemical']),_head(tokens,pair['gene'])
        graph=defaultdict(set)
        for t in tokens:
            if t['i']!=t['head']:
                graph[t['i']].add(t['head']);graph[t['head']].add(t['i'])
        queue=deque([(a['i'],[a['i']])]) if a else deque()
        seen=set(); path=[]
        while queue and b:
            node,trail=queue.popleft()
            if node==b['i']:
                path=trail;break
            if node in seen:continue
            seen.add(node)
            queue.extend((x,trail+[x]) for x in graph[node] if x not in seen)
        pair['path_spans']=[{'start':t['start'],'end':t['end'],'text':t['text']} for t in tokens if t['i'] in path]
    return pairs


def prepare(features,auxiliary,output):
    features,auxiliary,output=map(Path,(features,auxiliary,output))
    from .nlp_v2 import parser_for
    parser=parser_for({})
    manifest=json.loads((features/'manifest.json').read_bytes())
    auxiliary_manifest=json.loads((auxiliary/'manifest.json').read_bytes())
    if manifest['split']!='train' or auxiliary_manifest['split']!='train':
        raise ValueError('Joint features require TRAIN only')
    protected=set();protected_text=set()
    archive=auxiliary.parent/'raw/cg-official.zip'
    if not archive.exists():raise ValueError('CG split inventory required before joint fitting')
    with zipfile.ZipFile(archive) as source:
        for name in source.namelist():
            # Identify heldout papers from filenames; no heldout labels read.
            if '/original-data/' in name and any('/'+s+'/' in name for s in ('devel','test')) and name.endswith('.txt'):
                match=re.fullmatch(r'PMID-(\d+)\.txt',Path(name).name)
                if match:protected.add(match[1])
                from .splits_v7 import text_key
                protected_text.add(text_key(source.read(name).decode('utf-8')))
    files=[];excluded=[];vocab={'drugprot':set(),'roles':set(),'triggers':set()}
    for member in manifest['files']:
        d=json.loads((features/member['path']).read_bytes())
        if d['pmid'] in protected or text_key(d['text']) in protected_text:
            excluded.append({'task':'bioredirect','pmid':d['pmid']});continue
        pairs=local_pairs(d,d['entities'])
        lookup={(p['chemical']['start'],p['chemical']['end'],p['gene']['start'],p['gene']['end']):i for i,p in enumerate(pairs)}
        bags=[]
        for b in d['bags']:
            indices=[]
            for p in b['endpoints']:
                key=(p['chemical']['start'],p['chemical']['end'],p['gene']['start'],p['gene']['end'])
                if key in lookup:indices.append(lookup[key])
            bags.append({'indices':sorted(set(indices)),'targets':b['targets']})
        payload={'pmid':d['pmid'],'text':d['text'],'task':'bioredirect','pairs':dependency_paths(pairs,parser),
                 'bags':bags,'gold_keys':d['gold_keys'],'entities':d['entities']}
        target=immutable(output/('bioredirect-'+d['pmid']+'.json'),payload)
        files.append({'path':target.name,'sha256':sha(target.read_bytes()),'task':'bioredirect'})
    for task in ('drugprot','cg'):
        record=auxiliary_manifest['tasks'][task]
        raw=(auxiliary/record['path']).read_bytes()
        if sha(raw)!=record['sha256']:raise ValueError('Auxiliary source differs')
        for n,d in enumerate(json.loads(raw)['documents']):
            if d['pmid'] in protected or text_key(d['text']) in protected_text:
                excluded.append({'task':task,'pmid':d['pmid']});continue
            pairs=[]
            if task=='drugprot':
                pairs=local_pairs(d,d['entities'])
                relation=defaultdict(set)
                for r in d['relations']:
                    relation[(r['arg1'],r['arg2'])].add(r['type']);vocab['drugprot'].add(r['type'])
                for p in pairs:
                    p['local_labels']=sorted(relation.get((p['chemical']['annotation_id'],p['gene']['annotation_id']),()))
                payload={'pmid':d['pmid'],'text':d['text'],'task':task,'pairs':dependency_paths(pairs,parser)}
            else:
                spans=d['spans']; events=d['events']
                triggers={e['trigger']:spans[e['trigger']] for e in events.values()}
                links=defaultdict(set)
                for e in events.values():
                    vocab['triggers'].add(e['type'])
                    for arg in e['arguments']:
                        target=events[arg['target']]['trigger'] if arg['target'] in events else arg['target']
                        links[(e['trigger'],target)].add(arg['role'])
                        vocab['roles'].add(arg['role'])
                def entity(s):
                    if len(s['spans'])!=1:return None
                    return {**s['spans'][0],'annotation_id':s['annotation_id'],'kind':'gene'}
                sentences=sentence_spans(d['text'],[])
                for aid,a in triggers.items():
                    a=entity(a)
                    if a is None:continue
                    sentence=next((s for s in sentences if s['start']<=a['start']<a['end']<=s['end']),None)
                    if not sentence:continue
                    for bid,b in spans.items():
                        b=entity(b)
                        if b is None or aid==bid or not sentence['start']<=b['start']<b['end']<=sentence['end']:continue
                        pairs.append({'chemical':a,'gene':b,'sentence':sentence,'path_spans':[],
                                      'local_labels':sorted(links.get((aid,bid),()))})
                payload={'pmid':d['pmid'],'text':d['text'],'task':task,'pairs':dependency_paths(pairs,parser),
                         'trigger_spans':[{'type':e['type'],'spans':spans[e['trigger']]['spans']} for e in events.values()],
                         'gold_entities_provided_for_role_training':True}
            target=immutable(output/(task+'-'+d['pmid']+'.json'),payload)
            files.append({'path':target.name,'sha256':sha(target.read_bytes()),'task':task})
            if (n+1)%50==0:print(canonical({'stage':'joint-source-pairs','task':task,'papers':n+1}),flush=True)
    result={'split':'train','files':files,'vocab':{k:sorted(v) for k,v in vocab.items()},
            'source_manifests':{'bioredirect':sha((features/'manifest.json').read_bytes()),'auxiliary':sha((auxiliary/'manifest.json').read_bytes())},
            'pair_source_sha256':sha(Path(__file__).read_bytes()),'source_only_pair_positions':True}
    result['cg_protected_pmids']=sorted(protected)
    result['cg_protected_text_hashes']=sorted(protected_text)
    result['excluded_cg_heldout_papers']=excluded
    result['cg_test_labels_read']=False
    immutable(output/'manifest.json',result)
    return result
