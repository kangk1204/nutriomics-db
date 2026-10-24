"""Bind an optional ID ranker to its frozen dictionary and adoption decision."""
import json
from pathlib import Path
from .contract_v7 import sha
from .adoption_v7 import identity_adoption


def load_identity(path,values):
    import joblib
    path=Path(path);raw=(path/'manifest.json').read_bytes();manifest=json.loads(raw)
    if manifest['identity_sha256']!=sha(Path(__file__).with_name('identity_v7.py').read_bytes()):
        raise ValueError('ID feature implementation changed')
    if manifest['dictionary_sha256']!=values['_gene_index'].manifest['records_sha256']:
        raise ValueError('ID ranker dictionary differs from extraction dictionary')
    if manifest['model_sha256']!=sha((path/'identity.joblib').read_bytes()):
        raise ValueError('ID ranker weights changed')
    model=joblib.load(path/'identity.joblib')
    return model,{**manifest,'manifest_sha256':sha(raw),'adoption':identity_adoption(manifest)}


def apply_identity(mentions,text,sentences,values,loaded):
    from .identity_v7 import apply
    model,manifest=loaded
    adoption=identity_adoption(manifest)
    policy={**manifest,'selection':{**manifest['selection'],'target_precision_met':adoption['passed']}}
    return apply(mentions,text,sentences,values['_id_search'],model,policy)
