"""Real CUDA forward/backward contract checks, independent of scientific scores."""
import gc
from .common import *

def run():
 import torch
 from .engine import seed,setup,collate
 from .neural import tokenizer,Network,marker_masks
 from .relations import document_loss,nli_loss
 from .identity import loss as id_loss
 tok=tokenizer();dev=setup();seed();records=[]
 for task in ('document','nli','identity','auxiliary'):
  network=Network(task,len(tok)).to(dev).train();row=tok('[C] Vitamin D [/C] reduces [G] GENE [/G] expression.',max_length=256,truncation=True);row.update(marker_masks(row['input_ids'],tok))
  row.update(view_encodings=[dict(row)],targets=[1.,0.,0.,1.,0.,0.,0.,0.,0.],target=1.,direction_target=0,
   query='Vitamin D reduces gene expression',candidate_texts=['NIL','vitamin D','different compound'],target_positions=[1])
  inputs=collate([row],tok,task,dev);torch.cuda.reset_peak_memory_stats()
  with torch.autocast('cuda',dtype=torch.bfloat16):
   if task=='document':loss=document_loss(network,inputs,[row])
   elif task=='nli':loss=nli_loss(network,inputs,[row])
   elif task=='identity':loss=id_loss(network,inputs,[row])
   else:loss=torch.nn.functional.cross_entropy(network(**inputs).float(),torch.tensor([1],device=dev))
  loss.backward();gradient=float(network.direction.weight.grad.abs().sum()) if task=='document' else float(network.head.weight.grad.abs().sum())
  if gradient<=0 or not torch.isfinite(loss):raise ValueError('GPU 과제 출력층 미학습: '+task)
  records.append({'task':task,'finite_loss':True,'output_gradient_sum':gradient,'peak_gpu_mib':round(torch.cuda.max_memory_allocated()/2**20)})
  del network,loss,inputs;gc.collect();torch.cuda.empty_cache()
 atomic(HOME/'gpu-task-contracts.json',{'records':records,'synthetic_contract_only':True,'scientific_performance':False,'source_sha256':source_hashes()},True);return records
