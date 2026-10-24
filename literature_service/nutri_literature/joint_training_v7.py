"""Resumable CPU multitask MIL and LoRA; no document-to-sentence labels."""
import json
from pathlib import Path
import random
import time
from .contract_v7 import POLICY,TYPES,DIRECTIONS,THRESHOLDS,canonical,sha,immutable,counts,passes
from .encoder_v7 import ContextEncoder,pair_vector,validate_pair
from .joint_v7 import make_heads,biored_loss,local_loss,bag_pool
from .splits_v7 import grouped_folds
from .supervision_v7 import predicted_keys


def load_documents(directory):
    directory=Path(directory)
    manifest=json.loads((directory/'manifest.json').read_bytes())
    if manifest['split']!='train':raise ValueError('Joint fitting is restricted to TRAIN')
    docs=[]
    for member in manifest['files']:
        raw=(directory/member['path']).read_bytes()
        if sha(raw)!=member['sha256']:raise ValueError('Joint source changed')
        docs.append(json.loads(raw))
    return manifest,docs


class JointModel:
    def __init__(self,base,vocab,adapt=False):
        import torch
        torch.manual_seed(POLICY['seed'])
        self.encoder=ContextEncoder(base,adapt)
        self.vocab=vocab
        self.heads=make_heads(self.encoder.model.config.hidden_size,vocab['drugprot'],vocab['roles'],vocab['triggers'])
        self.adapt=adapt

    def parameters(self):
        return [p for p in [*self.encoder.model.parameters(),*self.heads.parameters()] if p.requires_grad]

    def forward(self,doc):
        import torch
        if not self.adapt:
            from .context_cache_v7 import fixed_context
            hidden,offsets,vectors=fixed_context(self,doc)
            return hidden,offsets,self.heads(vectors) if vectors is not None else None
        hidden,offsets=self.encoder.encode(doc['text'])
        vectors=[]
        for p in doc['pairs']:
            validate_pair(doc['text'],p)
            vectors.append(pair_vector(hidden,offsets,p['chemical'],p['gene'],p['sentence'],p.get('path_spans',[])))
        output=self.heads(torch.stack(vectors)) if vectors else None
        return hidden,offsets,output

    def loss(self,doc):
        import torch
        from torch.nn import functional as f
        hidden,offsets,output=self.forward(doc)
        task=doc['task']; losses=[]
        if task=='bioredirect' and output is not None:
            loss=biored_loss(output,doc['bags'])
            if loss is not None:losses.append(loss)
        elif task in ('drugprot','cg'):
            vocabulary=self.vocab['drugprot' if task=='drugprot' else 'roles']
            targets=[[float(t in p['local_labels']) for t in vocabulary] for p in doc['pairs']]
            if output is not None and vocabulary:
                loss=local_loss(output,targets,'drugprot' if task=='drugprot' else 'event_role')
                if loss is not None:losses.append(loss)
            if task=='cg' and self.vocab['triggers']:
                target=torch.zeros((len(offsets),len(self.vocab['triggers'])))
                for trigger in doc['trigger_spans']:
                    if trigger['type'] not in self.vocab['triggers']:continue
                    column=self.vocab['triggers'].index(trigger['type'])
                    for i,(lo,hi) in enumerate(offsets):
                        if any(lo<s['end'] and s['start']<hi for s in trigger['spans']):target[i,column]=1.
                losses.append(f.binary_cross_entropy_with_logits(self.heads.trigger(hidden),target))
        return torch.stack(losses).mean() if losses else None

    def predict(self,doc):
        import torch
        self.encoder.model.eval();self.heads.eval()
        with torch.no_grad():
            hidden,offsets,output=self.forward(doc)
            rows=[]
            for bag in doc.get('bags',[]):
                if output is None or not bag['indices']:
                    rows.append({**{t:0. for t in TYPES},'valid':0.,'exists':0.,'direction':{}});continue
                indices=bag['indices']
                types=torch.sigmoid(bag_pool(output['types'][indices])).tolist()
                direction=torch.softmax(bag_pool(output['direction'][indices]),dim=0).tolist()
                rows.append({**dict(zip(TYPES,types)), 'valid':float(torch.sigmoid(bag_pool(output['valid'][indices])[0])),
                             'exists':float(torch.sigmoid(bag_pool(output['exists'][indices])[0])),
                             'direction':dict(zip(DIRECTIONS,direction))})
        return rows

    def state(self):
        return {'adapters':{n:p.detach().cpu() for n,p in self.encoder.model.named_parameters() if p.requires_grad},
                'heads':self.heads.state_dict()}

    def restore(self,state):
        import torch
        with torch.no_grad():
            parameters=dict(self.encoder.model.named_parameters())
            if set(state['adapters'])!={n for n,p in parameters.items() if p.requires_grad}:
                raise ValueError('Adapter architecture differs')
            for name,value in state['adapters'].items():parameters[name].copy_(value)
        self.heads.load_state_dict(state['heads'])


def fit_model(docs,vocab,base,adapt,output,epochs=3,smoke_steps=None):
    import torch
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    model=JointModel(base,vocab,adapt)
    contract={'training_documents':sorted((d['task'],d['pmid'],sha(d['text'])) for d in docs),
              'encoder':model.encoder.manifest,'vocab':vocab,'policy':POLICY,'epochs':epochs,
              'smoke_steps':smoke_steps,'implementation':{name:sha(Path(__file__).with_name(name).read_bytes())
              for name in ('joint_training_v7.py','joint_v7.py','encoder_v7.py','joint_data_v7.py','context_cache_v7.py')}}
    immutable(output/'contract.json',contract)
    final=output/'weights.pt'
    if final.exists() and (output/'manifest.json').exists():
        manifest=json.loads((output/'manifest.json').read_bytes())
        if sha(final.read_bytes())!=manifest['weights_sha256']:raise ValueError('Joint weights changed')
        model.restore(torch.load(final,map_location='cpu',weights_only=True));return model,manifest
    optimizer=torch.optim.AdamW(model.parameters(),lr=POLICY['lora']['lr'],weight_decay=.01)
    checkpoint=output/'checkpoint.pt'
    start_epoch=start_index=updates=0
    if checkpoint.exists():
        saved=torch.load(checkpoint,map_location='cpu',weights_only=True)
        model.restore(saved['model']);optimizer.load_state_dict(saved['optimizer']);torch.set_rng_state(saved['rng'])
        start_epoch,start_index,updates=saved['epoch'],saved['index'],saved['updates']
    started=time.monotonic();losses=[];steps=0
    for epoch in range(start_epoch,epochs):
        order=list(range(len(docs)));random.Random(POLICY['seed']+epoch).shuffle(order)
        model.encoder.model.train(adapt);model.heads.train()
        optimizer.zero_grad();accumulated=0
        for position in range(start_index if epoch==start_epoch else 0,len(order)):
            loss=model.loss(docs[order[position]])
            if loss is not None:
                if not torch.isfinite(loss):raise ValueError('Non-finite joint loss')
                (loss/POLICY['lora']['accumulation']).backward()
                losses.append(float(loss.detach()));accumulated+=1;steps+=1
            if accumulated==POLICY['lora']['accumulation'] or position==len(order)-1:
                if accumulated:
                    torch.nn.utils.clip_grad_norm_(model.parameters(),1.)
                    optimizer.step();optimizer.zero_grad();updates+=1;accumulated=0
                temp=checkpoint.with_suffix('.tmp')
                torch.save({'model':model.state(),'optimizer':optimizer.state_dict(),'rng':torch.get_rng_state(),
                            'epoch':epoch,'index':position+1,'updates':updates},temp)
                temp.replace(checkpoint)
                print(canonical({'stage':'joint-cpu-training','adapted':adapt,'epoch':epoch+1,'papers':position+1,
                                 'total':len(order),'updates':updates,'loss':sum(losses[-16:])/max(1,len(losses[-16:])),
                                 'elapsed_seconds':time.monotonic()-started}),flush=True)
            if smoke_steps is not None and steps>=smoke_steps:
                if accumulated:optimizer.step();optimizer.zero_grad()
                break
        start_index=0
        if smoke_steps is not None and steps>=smoke_steps:break
    temporary=final.with_suffix('.tmp')
    torch.save(model.state(),temporary)
    temporary.replace(final)
    manifest={'contract_sha256':sha((output/'contract.json').read_bytes()),'weights_sha256':sha(final.read_bytes()),
              'encoder_identity':model.encoder.identity,'adapted':adapt,'smoke_only':smoke_steps is not None,
              'training_seconds':time.monotonic()-started,'updates':updates,'source_sha256':sha(Path(__file__).read_bytes())}
    immutable(output/'manifest.json',manifest)
    return model,manifest


def native_metric(docs,predictions,threshold):
    expected,predicted=set(),set()
    for d,rows in zip(docs,predictions):
        if d['task']!='bioredirect':continue
        expected.update((p,tuple(ids),t) for p,ids,t in d['gold_keys'])
        for b,row in zip(d['bags'],rows):
            labels=[t for t in TYPES if row[t]*row['valid']>=threshold]
            endpoints=[{'chemical':d['pairs'][i]['chemical'],'gene':d['pairs'][i]['gene']} for i in b['indices']]
            predicted.update(predicted_keys(d['pmid'],endpoints,labels))
    return counts(expected,predicted)


def nested_train(features,bc2gm,generic,output):
    manifest,documents=load_documents(features)
    output=Path(output)
    contract={'features_sha256':sha((Path(features)/'manifest.json').read_bytes()),'policy':POLICY,
              'sources':{n:sha(Path(__file__).with_name(n).read_bytes()) for n in ('joint_training_v7.py','joint_v7.py','joint_data_v7.py','encoder_v7.py','context_cache_v7.py')}}
    immutable(output/'training-contract.json',contract)
    assignment=grouped_folds(documents)
    families={'bc2gm_fixed':(bc2gm,False),'generic_fixed':(generic,False),'generic_lora':(generic,True)}
    outer_records=[]
    def configurations(train,fold_map,family,prefix):
        predictions={}
        for fold in sorted(set(fold_map.values())):
            fit=[d for d in train if fold_map[d['pmid']]!=fold]
            held=[d for d in train if fold_map[d['pmid']]==fold and d['task']=='bioredirect']
            base,adapt=families[family]
            model,_=fit_model(fit,manifest['vocab'],base,adapt,output/prefix/family/('fold-'+str(fold)))
            for d in held:predictions[d['pmid']]=model.predict(d)
            del model
        observed=[d for d in train if d['task']=='bioredirect']
        return [{'family':family,'threshold':t,**native_metric(observed,[predictions[d['pmid']] for d in observed],t)} for t in THRESHOLDS]
    def choose(rows):
        eligible=[r for r in rows if passes(r)]
        return {**max(eligible or rows,key=lambda r:(r['recall'] if eligible else r['f1'],r['f1'],r['precision'],r['family']=='generic_fixed')),
                'targets_met':bool(eligible)}
    for outer in range(5):
        train=[d for d in documents if assignment[d['pmid']]!=outer]
        held=[d for d in documents if assignment[d['pmid']]==outer and d['task']=='bioredirect']
        inner=grouped_folds(train,3,'joint-inner:'+str(outer))
        rows=[]
        for family in ('bc2gm_fixed','generic_fixed'):
            rows.extend(configurations(train,inner,family,'outer-'+str(outer)+'/inner'))
        if not any(passes(r) for r in rows):
            rows.extend(configurations(train,inner,'generic_lora','outer-'+str(outer)+'/inner'))
        selection=choose(rows)
        base,adapt=families[selection['family']]
        model,_=fit_model(train,manifest['vocab'],base,adapt,output/('outer-'+str(outer))/'selected')
        observed=[model.predict(d) for d in held]
        metric=native_metric(held,observed,selection['threshold'])
        immutable(output/('outer-'+str(outer)+'-result.json'),{'selection':selection,'metrics':metric,'configurations':rows,
                  'held_pmids':[d['pmid'] for d in held],'predictions':observed})
        outer_records.append(metric)
        del model
    rows=[]
    for family in ('bc2gm_fixed','generic_fixed'):
        rows.extend(configurations(documents,assignment,family,'final-oof'))
    if not any(passes(r) for r in rows):rows.extend(configurations(documents,assignment,'generic_lora','final-oof'))
    selection=choose(rows)
    base,adapt=families[selection['family']]
    _,weights=fit_model(documents,manifest['vocab'],base,adapt,output/'selected')
    totals={k:sum(r[k] for r in outer_records) for k in ('tp','fp','fn')}
    p=totals['tp']/max(1,totals['tp']+totals['fp']);r=totals['tp']/max(1,totals['tp']+totals['fn'])
    result={**contract,'selection':selection,'outer_metrics':{**totals,'precision':p,'recall':r,'f1':2*p*r/(p+r) if p+r else 0.},
            'fold_assignments':assignment,'training_pmids':sorted(assignment),'vocab':manifest['vocab'],
            'weights':weights,'base_path':str(base),'adapted':adapt,'sealed_test_opened':False,'nutrition_accuracy':'unmeasured'}
    immutable(output/'manifest.json',result)
    return result
