"""CPU BiomedBERT token contexts and exact last-four-layer low-rank updates."""
from pathlib import Path
from collections import OrderedDict
from .contract_v7 import POLICY, canonical, sha


def install_lora(model):
    import torch
    from torch import nn
    cfg = POLICY['lora']
    for p in model.parameters():
        p.requires_grad_(False)
    class LowRankLinear(nn.Module):
        def __init__(self, base):
            super().__init__()
            self.base = base
            self.a = nn.Parameter(torch.empty(cfg['r'], base.in_features))
            self.b = nn.Parameter(torch.zeros(base.out_features, cfg['r']))
            self.dropout = nn.Dropout(cfg['dropout'])
            nn.init.kaiming_uniform_(self.a, a=5 ** .5)
        def forward(self, x):
            return self.base(x) + (self.dropout(x) @ self.a.T @ self.b.T) * (cfg['alpha'] / cfg['r'])
    layers = getattr(model, 'encoder', None)
    if layers is None or len(layers.layer) != 12:
        raise ValueError('V7 LoRA requires the pinned 12-layer BERT architecture')
    for i in cfg['layers']:
        attention = layers.layer[i].attention.self
        for target in cfg['targets']:
            setattr(attention, target, LowRankLinear(getattr(attention, target)))
    return model


class ContextEncoder:
    def __init__(self, path, adapt=False):
        import torch
        from transformers import AutoModel, AutoTokenizer
        torch.set_num_threads(1)
        self.path = Path(path)
        self.tokenizer = AutoTokenizer.from_pretrained(self.path, local_files_only=True, use_fast=True)
        if not self.tokenizer.is_fast:
            raise ValueError('Fast tokenizer with Unicode positions required')
        self.model = AutoModel.from_pretrained(self.path, local_files_only=True).to('cpu')
        self.model.eval()
        for parameter in self.model.parameters():
            parameter.requires_grad_(False)
        if adapt:
            install_lora(self.model)
        files = {p.name: sha(p.read_bytes()) for p in self.path.iterdir() if p.is_file() and p.suffix in ('.bin','.safetensors','.json','.txt')}
        self.manifest = {'files': files, 'tokenizer_sha256':sha(self.tokenizer.backend_tokenizer.to_str()),
                         'adapted':adapt, 'device':'cpu', 'weights_updated':adapt, 'source_sha256':sha(Path(__file__).read_bytes()),
                         'policy':POLICY['lora'], 'dimension':self.model.config.hidden_size}
        self.identity = sha(canonical(self.manifest))

    def encode(self, text):
        import torch
        encoded = self.tokenizer(text, add_special_tokens=False, return_offsets_mapping=True)
        ids, offsets = encoded['input_ids'], encoded['offset_mapping']
        rows, retained, seen = [], [], set()
        size, stride = POLICY['lora']['max_tokens'] - 2, POLICY['lora']['max_tokens'] - 2 - POLICY['lora']['overlap']
        for begin in range(0, len(ids), stride):
            end = min(begin + size, len(ids))
            prepared = self.tokenizer.prepare_for_model(ids[begin:end],return_tensors='pt',return_attention_mask=True)
            prepared = {k:v.unsqueeze(0) if v.ndim == 1 else v for k,v in prepared.items()}
            hidden = self.model(**prepared).last_hidden_state[0,1:1+end-begin]
            for j in range(end-begin):
                i = begin+j
                if i not in seen and offsets[i][0] != offsets[i][1]:
                    seen.add(i)
                    rows.append(hidden[j])
                    retained.append(offsets[i])
            if end == len(ids):
                break
        if not rows:
            raise ValueError('Empty source tokenization')
        return torch.stack(rows), retained


def pooled(hidden, offsets, intervals):
    import torch
    indices = [i for i,(lo,hi) in enumerate(offsets) if any(lo < end and start < hi for start,end in intervals)]
    if not indices:
        return hidden.mean(dim=0) * 0
    return hidden[torch.tensor(indices,dtype=torch.long)].mean(dim=0)


def pair_vector(hidden, offsets, chemical, gene, sentence, path_spans=()):
    import torch
    a = pooled(hidden,offsets,[(chemical['start'],chemical['end'])])
    b = pooled(hidden,offsets,[(gene['start'],gene['end'])])
    first,second = sorted((chemical,gene),key=lambda x:x['start'])
    between = pooled(hidden,offsets,[(first['end'],second['start'])]) if first['end'] < second['start'] else a*0
    context = pooled(hidden,offsets,[(sentence['start'],sentence['end'])])
    path = pooled(hidden,offsets,[(p['start'],p['end']) for p in path_spans])
    return torch.cat((a,b,between,context,torch.abs(a-b),a*b,path))


def validate_pair(text, pair):
    for entity in (pair['chemical'],pair['gene'],pair['sentence']):
        if text[entity['start']:entity['end']] != entity['text']:
            raise ValueError('V7 pair/source Unicode position mismatch')
