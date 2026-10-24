"""Pin auxiliary TRAIN sources/model revision, never extract final labels."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import tarfile
import urllib.request
import zipfile
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def fetch(url):
    request=urllib.request.Request(url,headers={'User-Agent':'nutriomics-literature-v7/1.0'})
    with urllib.request.urlopen(request,timeout=90) as response:
        return response.read()


def main():
    from nutri_literature.contract_v7 import immutable,sha
    from nutri_literature.corpora_v7 import prepare
    p=argparse.ArgumentParser()
    p.add_argument('--drugprot',type=Path,required=True)
    args=p.parse_args()
    home=ROOT/'runtime/v7'
    raw=home/'raw'
    raw.mkdir(parents=True,exist_ok=True)
    repo='openbiocorpora/bionlp-st-2013-cg'
    receipt=raw/'cg-source.json'
    if receipt.exists():
        record=json.loads(receipt.read_bytes())
        commit=record['commit']
    else:
        commit=json.loads(fetch('https://api.github.com/repos/'+repo+'/commits/master'))['sha']
        record={'repository':repo,'commit':commit,'url':'https://codeload.github.com/'+repo+'/zip/'+commit}
        immutable(receipt,record)
    archive=raw/'cg-official.zip'
    if not archive.exists():
        archive.write_bytes(fetch(record['url']))
    source=raw/'cg'
    source.mkdir(exist_ok=True)
    extracted=[]
    with zipfile.ZipFile(archive) as z:
        for name in z.namelist():
            if 'original-data' not in name or 'train' not in name.lower():
                continue
            data=z.read(name)
            if name.endswith(('.tar.gz','.tgz')):
                with tarfile.open(fileobj=io.BytesIO(data),mode='r:gz') as t:
                    for member in t:
                        if not member.isfile() or not member.name.endswith(('.txt','.a1','.a2')):
                            continue
                        target=source/'train'/Path(member.name).name
                        target.parent.mkdir(parents=True,exist_ok=True)
                        value=t.extractfile(member).read()
                        if target.exists() and target.read_bytes()!=value:
                            raise ValueError('Pinned CG train source differs')
                        target.write_bytes(value)
                        extracted.append({'path':str(target.relative_to(source)),'sha256':sha(value)})
            elif name.endswith(('.txt','.a1','.a2')):
                target=source/'train'/Path(name).name
                target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes(data)
                extracted.append({'path':str(target.relative_to(source)),'sha256':sha(data)})
    immutable(raw/'cg-train-manifest.json',{'source':record,'archive_sha256':sha(archive.read_bytes()),'files':extracted,
                                           'test_labels_read':False})
    data_root=next((ROOT/'runtime/v6/training-data').glob('audited-complete*'))
    # Existing protected input partitions only: never traverse gold paths.
    index=json.loads((ROOT/'runtime/public_benchmarks/v2-measurement-index.json').read_bytes())
    protected=[]
    for step in index['steps'].values():
        if isinstance(step,dict) and step.get('inputs_path'):
            candidate=Path(step['inputs_path'])
            if not candidate.exists():
                parts=candidate.parts
                if 'runtime' in parts:
                    candidate=ROOT.joinpath(*parts[parts.index('runtime'):])
            if candidate.exists():
                protected.append(candidate)
    result=prepare(args.drugprot,source,data_root/'audit.json',list(set(protected)),home/'auxiliary-train')
    print(json.dumps({'stage':'auxiliary-train-prepared','tasks':result['tasks'],'excluded':len(result['excluded'])}),flush=True)
    from huggingface_hub import HfApi,snapshot_download
    model_id='microsoft/BiomedNLP-BiomedBERT-base-uncased-abstract-fulltext'
    pinned=raw/'biomedbert-revision.json'
    revision=json.loads(pinned.read_bytes())['revision'] if pinned.exists() else HfApi(token=False).model_info(model_id).sha
    immutable(pinned,{'repo_id':model_id,'revision':revision,'source':'official Microsoft checkpoint','pretraining_overlap':'unknown'})
    destination=home/'base-biomedbert'
    snapshot_download(model_id,revision=revision,token=False,local_dir=destination,
                      allow_patterns=['config.json','model.safetensors','pytorch_model.bin','vocab.txt','tokenizer*','special_tokens_map.json'])
    immutable(destination/'v7-weight-manifest.json',{'revision':revision,'repo_id':model_id,
        'files':{f.name:sha(f.read_bytes()) for f in destination.iterdir() if f.is_file() and not f.name.startswith('v7-')}})
    print(json.dumps({'stage':'generic-biomedbert-pinned','revision':revision}),flush=True)


if __name__=='__main__':
    main()
