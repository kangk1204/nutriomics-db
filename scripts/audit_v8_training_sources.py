"""Build DAPT-only public-gold exclusions from explicitly listed metadata/train files.

No directory scan, sealed annotation access, model request, or training occurs here.
An incomplete inventory is a useful waiting artifact, never permission to start DAPT.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = ('NLM-Gene','NLM-Chem','BioRED','BioREDirect','DrugProt','CG','BC5CDR',
            'EBM-NLP','PubMed-RCT','EvidenceOutcomes','EvidenceInference','BC2GM')
KINDS = {'protection','protected_audit','source_only_documents','training_documents',
         'native_rows','training_manifest','id_manifest'}
ANNOTATIONS = {'entities','relations','labels','gold','targets','comparisons','annotations'}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def normalized_text_hash(text):
    return hashlib.sha256(' '.join(unicodedata.normalize('NFKC',text).casefold().split()).encode('utf8')).hexdigest()


def default_config():
    """Fixed, previously inspected paths. Missing files remain explicit missing sources."""
    items = [
        {'name':'r2-upstream-protection','dataset':'upstream-union','split':'protected',
         'kind':'protection','path':'relation-repair-v6-gpu/data/protection.json'},
        {'name':'v6-protected-audit','dataset':'public-protection-union','split':'protected',
         'kind':'protected_audit','path':'v6/training-data/audited-complete/audit.json'},
        {'name':'NLM-Gene-train','dataset':'NLM-Gene','split':'train','kind':'training_documents',
         'path':'v6/training-data/official-nlm-train/nlmgene-train.json'},
        {'name':'NLM-Chem-train','dataset':'NLM-Chem','split':'train','kind':'training_documents',
         'path':'v6/training-data/official-nlm-train/nlmchem-train.json'},
        {'name':'BioREDirect-train','dataset':'BioREDirect','split':'train','kind':'training_documents',
         'path':'v6/training-data/official-relation-train.json'},
        {'name':'BioRED-development','dataset':'BioRED','split':'development','kind':'source_only_documents',
         'path':'public_benchmarks/biored/c1cc615dc140a064f4bbfd57/inputs.json'},
        {'name':'BioREDirect-development','dataset':'BioREDirect','split':'development','kind':'source_only_documents',
         'path':'public_benchmarks/bioredirect/404a04bf9fb316caa061e9ab/inputs.json'},
        {'name':'BC8-final-source-only','dataset':'BioREDirect','split':'bc8-test','kind':'source_only_documents',
         'path':'v6/training-data/protected-bc8-inputs.json'},
        {'name':'EBM-reserved-development','dataset':'EBM-NLP','split':'development','kind':'source_only_documents',
         'path':'v6/training-data/protected-ebm-development.json'},
        {'name':'EvidenceOutcomes-development','dataset':'EvidenceOutcomes','split':'development','kind':'source_only_documents',
         'path':'public_benchmarks/evidenceoutcomes/4718467f95ec7381f8e06399/inputs.json'},
        {'name':'EBM-allowed-train','dataset':'EBM-NLP','split':'train','kind':'training_manifest',
         'training_key':'ebm','path':'v6/validation/server-model-manifests/clinical-base.json'},
        {'name':'RCT-allowed-train','dataset':'PubMed-RCT','split':'train','kind':'training_manifest',
         'training_key':'rct','path':'v6/validation/server-model-manifests/clinical-base.json'},
        {'name':'EvidenceInference-allowed-train','dataset':'EvidenceInference','split':'train','kind':'training_documents',
         'path':'v6/refinement-r4/clinical-training.json'},
        {'name':'DrugProt-allowed-train','dataset':'DrugProt','split':'train','kind':'native_rows','task':'drugprot',
         'path':'relation-repair-v6-gpu/auxiliary/train.json'},
        {'name':'CG-allowed-train','dataset':'CG','split':'train','kind':'native_rows','task':'cg',
         'path':'relation-repair-v6-gpu/auxiliary/train.json'},
    ]
    for dataset,folder in [('NLM-Gene','nlmgene'),('NLM-Chem','nlmchem'),
                           ('PubMed-RCT','pubmedrct'),('EvidenceInference','evidenceinference')]:
        items.append({'name':dataset+'-test-source-only','dataset':dataset,'split':'test',
                      'kind':'source_only_documents','path':'holdouts/'+folder+'/inputs.json'})
    return {'schema_version':'v8-public-gold-audit-config-v1',
            'source_root':str(ROOT/'literature_service/runtime'),'sources':items}


def _identifiers(values, *, pmcid=False):
    if not isinstance(values,list):
        raise ValueError('Identifier inventory must be a list')
    result=set()
    for raw in values:
        value=str(raw).strip()
        if pmcid:
            value=value.upper()
            if not re.fullmatch(r'PMC\d+',value):
                raise ValueError('Invalid PMCID in exclusion inventory')
        elif not value or re.search(r'\s|[/\\]',value):
            raise ValueError('Invalid document identifier in exclusion inventory')
        result.add(value)
    return result


def _hashes(values):
    if not isinstance(values,list) or any(not isinstance(v,str) or not re.fullmatch(r'[0-9a-f]{64}',v) for v in values):
        raise ValueError('Normalized text hashes must be SHA-256 strings')
    return set(values)


def _read_source(spec,path):
    # These are inventory/allowed train inputs. A path to sealed annotations is never read.
    if any('sealed' in p.lower() for p in path.parts) or re.search(r'(^|[-_.])gold([-_.]|$)',path.name,re.I):
        raise ValueError('Sealed/gold annotation files cannot be audit inputs')
    before=path.read_bytes()
    digest=hashlib.sha256(before).hexdigest()
    if spec.get('sha256') is not None and spec['sha256']!=digest:
        raise ValueError('Explicit source SHA-256 differs: '+spec['name'])
    value=json.loads(before)
    if not isinstance(value,dict):
        raise ValueError('Audit source must have an object root')
    kind=spec['kind']
    if kind=='source_only_documents' and ANNOTATIONS & value.keys():
        raise ValueError('Source-only input unexpectedly contains annotation fields')
    ids,pmcids,hashes=set(),set(),set()
    row_count=missing_pmcid=missing_text=unidentifiable=0
    train_manifest_counts=None
    if kind in {'protection','protected_audit','id_manifest'}:
        prefix='protected_' if kind=='protected_audit' else ''
        if kind!='id_manifest' and (value.get('labels_read') is True or value.get('test_labels_read') is True):
            raise ValueError('Protected source claims annotation access')
        ids=_identifiers(value.get(prefix+'pmids',[]))
        pmcids=_identifiers(value.get(prefix+'pmcids',[]),pmcid=True)
        hashes=_hashes(value.get(prefix+'normalized_text_hashes',value.get(prefix+'text_hashes',[])))
        row_count=len(ids)
        missing_pmcid=None  # Aggregate lists cannot establish document-level PMCID coverage.
        missing_text=None
    else:
        if kind=='training_manifest':
            if value.get('training_split')!='train' or value.get('test_used_for_selection') is not False:
                raise ValueError('Training manifest is not explicitly train-only')
            source_rows=value.get('training_pmids',{}).get(spec.get('training_key'))
            if not isinstance(source_rows,list):
                raise ValueError('Training PMID inventory is absent')
            excluded=value.get('excluded_pmids',{}).get(spec.get('training_key'),[])
            if not isinstance(excluded,list):raise ValueError('Excluded train PMID inventory must be a list')
            # Previously excluded supervised examples remain public-gold articles:
            # DAPT must exclude those identifiers too, without opening their labels.
            train_manifest_counts={'allowed_train_identifiers':len(source_rows),'excluded_train_identifiers':len(excluded)}
            rows=({'pmid':p} for p in [*source_rows,*excluded])
        else:
            if kind in {'training_documents','native_rows'}:
                if spec.get('split')!='train':
                    raise ValueError('Annotated datasets may be accessed only through official train inputs')
                if value.get('split','train')!='train' or value.get('test_labels_read') is True or value.get('heldout_labels_read') is True:
                    raise ValueError('Annotated input is not an allowed train split')
            elif value.get('labels_read') is True:
                raise ValueError('Source-only input declares labels read')
            key='rows' if kind=='native_rows' else 'documents'
            rows=value.get(key)
            if not isinstance(rows,list):
                raise ValueError('Document metadata rows are absent')
        for row in rows:
            if not isinstance(row,dict):
                raise ValueError('Document metadata row must be an object')
            if kind=='native_rows' and row.get('task')!=spec.get('task'):
                continue
            if kind=='source_only_documents' and ANNOTATIONS & row.keys():
                raise ValueError('Source-only input unexpectedly contains annotation fields')
            # The values of entity/relation/ICO/token labels are never inspected or used.
            row_count+=1
            pmid=row.get('pmid');pmc=row.get('pmcid');text=row.get('text')
            if pmid is not None and str(pmid).strip():ids|=_identifiers([pmid])
            if pmc:pmcids|=_identifiers([pmc],pmcid=True)
            else:missing_pmcid+=1
            if isinstance(text,str) and text.strip():hashes.add(normalized_text_hash(text))
            else:missing_text+=1
            if not pmid and not pmc and not (isinstance(text,str) and text.strip()):unidentifiable+=1
    # A complete all-release inventory is a separately pinned declaration, not inferred
    # from train data or a small development sample. Default known paths never claim it.
    full=(spec['split']=='all' and value.get('split')=='all' and value.get('coverage_complete') is True
          and spec.get('sha256')==digest and type(value.get('expected_documents')) is int
          and value['expected_documents']==row_count and row_count>0 and not unidentifiable)
    return {'pmids':ids,'pmcids':pmcids,'hashes':hashes,'receipt':{
        'name':spec['name'],'dataset':spec['dataset'],'split':spec['split'],'sha256':digest,
        'kind':kind,'rows':row_count,'unique_pmids':len(ids),'unique_pmcids':len(pmcids),
        'normalized_text_hashes':len(hashes),'rows_missing_pmcid':missing_pmcid,
        'rows_missing_text':missing_text,'unidentifiable_rows':unidentifiable,
        'supervised_train_manifest_counts':train_manifest_counts,
        'all_release_coverage_verified':full,'labels_used_for_exclusion':False}}


def build_exclusions(config):
    specs=config.get('sources')
    if not isinstance(specs,list):raise ValueError('Explicit sources list required')
    root=Path(config.get('source_root',ROOT/'literature_service/runtime')).resolve()
    ids,pmcids,hashes=set(),set(),set()
    receipts=[];missing=[];names=set();covered=set()
    for spec in specs:
        if not isinstance(spec,dict) or spec.get('kind') not in KINDS:
            raise ValueError('Unsupported metadata source adapter')
        if not all(isinstance(spec.get(k),str) and spec[k] for k in ('name','dataset','split','path')):
            raise ValueError('Every source needs name, dataset, split and explicit path')
        if spec['name'] in names:raise ValueError('Duplicate source name')
        names.add(spec['name'])
        path=Path(spec['path']);path=(root/path).resolve() if not path.is_absolute() else path.resolve()
        if not path.is_file():
            missing.append({'name':spec['name'],'dataset':spec['dataset'],'split':spec['split'],'reason':'explicit_source_file_missing'})
            continue
        part=_read_source(spec,path)
        ids.update(part['pmids']);pmcids.update(part['pmcids']);hashes.update(part['hashes'])
        receipts.append(part['receipt'])
        if part['receipt']['all_release_coverage_verified']:covered.add(spec['dataset'])
    for dataset in REQUIRED:
        if dataset not in covered:
            missing.append({'name':dataset+'-complete-public-inventory','dataset':dataset,'split':'all',
                            'reason':'complete_public_release_metadata_not_verified'})
    complete=not missing and not any(r['unidentifiable_rows'] for r in receipts)
    numeric_ids={p for p in ids if re.fullmatch(r'[0-9]+',p)}
    legacy_ids=ids-numeric_ids
    pmcids.update(p.upper() for p in legacy_ids if re.fullmatch(r'PMC[0-9]+',p,re.I))
    return {'schema_version':'nutriomics-v8-dapt-exclusion-v1',
        # This describes the exported inventory, including projections of allowed
        # training files: only source identifiers, text hashes and receipts leave
        # this builder, never their entity/relation/effect annotations.
        'source_only':True,
        'purpose':'DAPT-only exclusion of all known public-gold articles, including official train',
        'not_a_supervised_training_allowlist':True,'status':'completed' if complete else 'waiting',
        'all_public_gold_excluded':complete,'required_public_gold_sources':list(REQUIRED),
        'pmids':sorted(numeric_ids),'pmcids':sorted(pmcids),'normalized_text_hashes':sorted(hashes),
        'source_document_ids':sorted(ids),'legacy_source_document_ids':sorted(legacy_ids),
        'counts':{'pmids':len(numeric_ids),'source_document_ids':len(ids),'legacy_source_document_ids':len(legacy_ids),
                  'pmcids':len(pmcids),'normalized_text_hashes':len(hashes)},
        'identifier_notes':{'numeric_pubmed_ids':len(numeric_ids),'non_numeric_source_document_ids':len(ids-numeric_ids),
                            'legacy_non_numeric_ids_retained_separately':True},
        'public_gold_sources':receipts,'sources_manifest':{'builder_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'config_sha256':hashlib.sha256(canonical(config).encode()).hexdigest(),
        'files':[{'name':r['name'],'split':r['split'],'sha256':r['sha256']} for r in receipts]},
        'missing_sources':missing,'pmcid_coverage':'incomplete_or_unestablished' if any(r['rows_missing_pmcid'] is None or r['rows_missing_pmcid']>0 for r in receipts) else 'available_in_listed_rows',
        'near_duplicate_text_audit':'not_performed','pretrained_model_overlap':'unknown',
        'sealed_annotation_files_opened':False,'model_requests':0}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,help='Explicit source manifest; no directory discovery')
    parser.add_argument('--output',type=Path,required=True,help='New output file; existing artifacts are never overwritten')
    args=parser.parse_args(argv)
    config=json.loads(args.config.read_text(encoding='utf8')) if args.config else default_config()
    if args.config and 'source_root' not in config:config['source_root']=str(args.config.resolve().parent)
    result=build_exclusions(config)
    output=args.output.resolve()
    for spec in config['sources']:
        source=Path(spec['path']);source=(Path(config.get('source_root',ROOT/'literature_service/runtime'))/source).resolve() if not source.is_absolute() else source.resolve()
        if output==source:raise ValueError('Output cannot replace an audit input')
    if args.config and output==args.config.resolve():raise ValueError('Output cannot replace its config')
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('xb') as stream:
        stream.write(canonical(result).encode('utf8'));stream.flush();os.fsync(stream.fileno())
    print(canonical({'status':result['status'],'all_public_gold_excluded':result['all_public_gold_excluded'],
                     'counts':result['counts'],'missing_sources':len(result['missing_sources'])}))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
