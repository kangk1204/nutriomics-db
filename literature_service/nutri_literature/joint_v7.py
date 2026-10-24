"""Shared local representations; document labels only supervise MIL bags."""
from .contract_v7 import TYPES,DIRECTIONS


def make_heads(dimension, drugprot_types, event_roles, trigger_types=()):
    import torch
    from torch import nn
    class Heads(nn.Module):
        def __init__(self):
            super().__init__()
            self.shared=nn.Sequential(nn.Linear(dimension*7,128),nn.ReLU(),nn.Dropout(.1))
            self.types=nn.Linear(128,len(TYPES))
            self.exists=nn.Linear(128,1)
            self.valid=nn.Linear(128,1)
            self.direction=nn.Linear(128,len(DIRECTIONS))
            self.drugprot=nn.Linear(128,max(1,len(drugprot_types)))
            self.event_role=nn.Linear(128,max(1,len(event_roles)))
            self.trigger=nn.Linear(dimension,max(1,len(trigger_types)))
        def forward(self,vectors):
            x=self.shared(vectors)
            return {name:getattr(self,name)(x) for name in ('types','exists','valid','direction','drugprot','event_role')}
    return Heads()


def bag_pool(logits):
    import math
    import torch
    # Normalize witness count: repeated text cannot itself raise a bag score.
    return torch.logsumexp(logits,dim=0)-math.log(logits.shape[0])


def biored_loss(outputs,bags):
    import torch
    from torch.nn import functional as f
    losses=[]
    for bag in bags:
        indices=bag['indices']
        if not indices:
            continue
        targets=bag['targets']
        valid=torch.tensor([float(targets['entity_valid'])]*len(indices))
        losses.append(f.binary_cross_entropy_with_logits(outputs['valid'][indices,0],valid))
        if not targets['semantic_supervised']:
            continue
        target=torch.tensor([float(t in targets['semantic_labels']) for t in TYPES])
        losses.append(f.binary_cross_entropy_with_logits(bag_pool(outputs['types'][indices]),target))
        losses.append(f.binary_cross_entropy_with_logits(bag_pool(outputs['exists'][indices])[0],torch.tensor(float(bool(targets['semantic_labels'])))))
        if targets['direction'] in DIRECTIONS:
            losses.append(f.cross_entropy(bag_pool(outputs['direction'][indices]).unsqueeze(0),torch.tensor([DIRECTIONS.index(targets['direction'])])))
    return torch.stack(losses).mean() if losses else None


def local_loss(outputs,targets,kind):
    import torch
    from torch.nn import functional as f
    if not targets:
        return None
    return f.binary_cross_entropy_with_logits(outputs[kind],torch.tensor(targets,dtype=torch.float32))
