"""Label-free fixed representations shared across folds, never adapter states."""
import json
import os
from pathlib import Path
from .contract_v7 import canonical, sha, immutable


def fixed_context(model, document):
    import torch
    from .encoder_v7 import pair_vector, validate_pair
    if model.adapt:
        raise ValueError('Trainable adapter representations cannot use the fixed cache')
    directory = Path(os.environ.get('LITERATURE_V7_CONTEXT_CACHE', 'runtime/v7/context-cache')) / model.encoder.identity
    # Labels, gold IDs and split assignments do not participate in this cache.
    source = {'text': document['text'], 'pairs': [
        {k: p.get(k, []) for k in ('chemical', 'gene', 'sentence', 'path_spans')}
        for p in document['pairs']]}
    key = sha(canonical(source))
    target = directory / (key + '.pt')
    receipt = target.with_suffix('.json')
    if target.exists() and receipt.exists():
        expected = json.loads(receipt.read_bytes())
        if expected['weights_sha256'] != sha(target.read_bytes()):
            raise ValueError('Fixed representation cache checksum differs')
        value = torch.load(target, map_location='cpu', weights_only=True)
        return value['hidden'], value['offsets'], value['vectors']
    model.encoder.model.eval()
    with torch.no_grad():
        hidden, offsets = model.encoder.encode(document['text'])
        vectors = []
        for p in document['pairs']:
            validate_pair(document['text'], p)
            vectors.append(pair_vector(hidden, offsets, p['chemical'], p['gene'], p['sentence'], p.get('path_spans', [])))
        vectors = torch.stack(vectors) if vectors else None
    directory.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix('.tmp-' + str(os.getpid()))
    torch.save({'hidden': hidden, 'offsets': offsets, 'vectors': vectors}, temp)
    temp.replace(target)
    immutable(receipt, {'weights_sha256': sha(target.read_bytes()), 'source_sha256': key,
                        'encoder_identity': model.encoder.identity, 'labels_used': False})
    return hidden, offsets, vectors
