"""Canonical missing publication IDs never become shared duplicate identities."""
from copy import deepcopy


def canonical_training_documents(documents):
    result=[]
    for document in documents:
        d=deepcopy(document)
        value=d.get('pmcid')
        if value is None or not str(value).strip():d.pop('pmcid',None)
        else:d['pmcid']=str(value).strip().upper()
        result.append(d)
    return result
