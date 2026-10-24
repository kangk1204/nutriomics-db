"""Checkpointed GPU training, accumulation, memory caps and durable runtime budget."""
import contextlib,math,random,time
from pathlib import Path
from .common import *

def seed():
 import numpy as np,torch
 random.seed(SEED);np.random.seed(SEED);torch.manual_seed(SEED);torch.cuda.manual_seed_all(SEED)
 torch.set_num_threads(1);torch.set_num_interop_threads(1) if torch.get_num_interop_threads()!=1 else None

def setup():
 import torch
 from .neural import device
 d=device();total=torch.cuda.get_device_properties(0).total_memory
 torch.cuda.set_per_process_memory_fraction(min(.95,POLICY['gpu_gib']*1024**3/total));return d

def save_weights(network,path):
 from safetensors.torch import save_file
 path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
 temp=path.with_suffix('.tmp');save_file({k:v.detach().cpu().contiguous() for k,v in network.state_dict().items()},str(temp));os.replace(temp,path)

def load_weights(network,path):
 from safetensors.torch import load_file
 network.load_state_dict(load_file(str(path),device='cpu'));return network

def collate(rows,tok,task,device):
 import torch
 size=max(len(r['input_ids']) for r in rows);pad=tok.pad_token_id;out={}
 for key in ('input_ids','attention_mask','token_type_ids','c_mask','g_mask'):
  if key not in rows[0]:continue
  out[key]=torch.tensor([r[key]+[pad if key=='input_ids' else 0]*(size-len(r[key])) for r in rows],device=device)
 if task=='ner':
  out['labels']=torch.tensor([r['labels']+[[-100,-100]]*(size-len(r['labels'])) for r in rows],device=device)
 return out

def train_network(task,rows,destination,loss_fn,epochs=None):
 if not rows:raise ValueError('학습 입력 0건을 성공으로 처리하지 않음')
 import inspect
 source_files=[Path(__file__),Path(__file__).with_name('neural.py'),Path(inspect.getfile(loss_fn))]
 recipe=sha(canonical({'task':task,'policy':POLICY,'epochs':epochs or POLICY['epochs'],
  'code':{p.name:filehash(p) for p in source_files},'auxiliary_encoder':filehash(HOME/'auxiliary/model/model.safetensors') if task in ('document','nli') and (HOME/'auxiliary/model/model.safetensors').exists() else None}))
 import torch
 from .neural import Network,tokenizer
 from torch.optim import AdamW
 dest=Path(destination);receipt=dest/'complete.json'
 if receipt.exists():
  v=read(receipt)
  if filehash(dest/'model.safetensors')!=v['weights_sha256']:raise ValueError('가중치 해시 변경')
  if v['rows_sha256']!=sha(canonical(rows)) or v['task']!=task:raise ValueError('완료 학습 입력/과제 변경; 새 실험 필요')
  if v.get('recipe_sha256')!=recipe:raise ValueError('학습 알고리즘/과제 소스 변경; 새 실험 버전 필요')
  return v
 dest.mkdir(parents=True,exist_ok=True);seed();d=setup();tok=tokenizer();network=Network(task,len(tok)).to(d)
 if task in ('document','nli'):
  positives=[0.]*9;totals=[0.]*9
  for row in rows:
   if task=='document':
    for i,target in enumerate(row['targets']):positives[i]+=target;totals[i]+=1
   else:
    i=0 if row['hypothesis']=='exists' else TYPES.index(row['hypothesis'])+1;positives[i]+=row['target'];totals[i]+=1
  # Fold-local reweighting improves minority recall; the held-out acceptance gate controls precision.
  network.class_weights.copy_(torch.tensor([min(10.,max(1.,((n-p)/p)**.5)) if p else 1. for p,n in zip(positives,totals)],device=d))
 warmstart=None
 if task in ('document','nli'):
  from safetensors.torch import load_file
  p=HOME/'auxiliary/model/model.safetensors'
  if not p.exists():raise FileNotFoundError('감사한 보조 과제 가중치 부재')
  network.encoder.load_state_dict({k[8:]:v for k,v in load_file(str(p),device='cpu').items() if k.startswith('encoder.')});warmstart=filehash(p)
 trainable=sum(p.numel() for p in network.parameters() if p.requires_grad)
 optimizer=AdamW(network.parameters(),lr=POLICY['learning_rate'],weight_decay=POLICY['weight_decay'])
 n_epochs=epochs or POLICY['epochs'];micro=1 if task in ('identity','document','nli') else POLICY['microbatch'];acc=32//micro;budget=RuntimeBudget()
 start=time.monotonic();remaining=budget.start(str(dest.relative_to(HOME)));steps=0;start_epoch=0
 # Optimizer/RNG state is local trusted self-produced state. Never deserialize downloaded pickle models.
 checkpoint=dest/'resume.pt'
 if checkpoint.exists():
  value=torch.load(checkpoint,map_location='cpu',weights_only=True)
  if value['task']!=task or value['rows_sha256']!=sha(canonical(rows)) or value.get('recipe_sha256')!=recipe:raise ValueError('체크포인트 입력/알고리즘 변경')
  network.load_state_dict(value['network']);optimizer.load_state_dict(value['optimizer']);steps=value['steps'];start_epoch=value['epoch']
  if 'rng_cpu' in value:torch.set_rng_state(value['rng_cpu']);torch.cuda.set_rng_state_all(value['rng_cuda'])
  for state in optimizer.state.values():
   for k,v in state.items():
    if isinstance(v,torch.Tensor):state[k]=v.to(d)
 losses=read(dest/'progress.json').get('losses',[]) if (dest/'progress.json').exists() else [];network.train();torch.cuda.reset_peak_memory_stats();all_steps=max(1,math.ceil(len(rows)/micro/acc)*n_epochs)
 try:
  for epoch in range(start_epoch,n_epochs):
   order=list(range(len(rows)));random.Random(SEED+epoch).shuffle(order);optimizer.zero_grad(set_to_none=True);epoch_losses=[]
   for start_index in range(0,len(order),micro):
    batch=[rows[i] for i in order[start_index:start_index+micro]];budget.check()
    batch_inputs=collate(batch,tok,task,d)
    dtype=torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    with torch.autocast('cuda',dtype=dtype):loss=loss_fn(network,batch_inputs,batch)
    if not torch.isfinite(loss):raise FloatingPointError('학습 손실 NaN/Inf')
    (loss/acc).backward();epoch_losses.append(float(loss.detach()))
    if ((start_index//micro+1)%acc==0) or start_index+micro>=len(order):
     torch.nn.utils.clip_grad_norm_(network.parameters(),1.)
     warm=max(1,int(all_steps*POLICY['warmup']));scale=min(1.,(steps+1)/warm)*max(0.,(all_steps-steps)/max(1,all_steps-warm)) if steps>=warm else (steps+1)/warm
     for group in optimizer.param_groups:group['lr']=POLICY['learning_rate']*scale
     optimizer.step();optimizer.zero_grad(set_to_none=True);steps+=1
    if start_index%max(micro,100*micro)==0:
     log('gpu-training',task=task,run=str(dest.relative_to(HOME)),epoch=epoch+1,row=start_index,total=len(rows),loss=epoch_losses[-1],peak_mib=round(torch.cuda.max_memory_allocated()/2**20))
   losses.append(sum(epoch_losses)/max(1,len(epoch_losses)))
   torch.save({'network':network.state_dict(),'optimizer':optimizer.state_dict(),'steps':steps,'epoch':epoch+1,'task':task,'rows_sha256':sha(canonical(rows)),'recipe_sha256':recipe,'rng_cpu':torch.get_rng_state(),'rng_cuda':torch.cuda.get_rng_state_all()},str(checkpoint)+'.tmp')
   os.replace(str(checkpoint)+'.tmp',checkpoint)
   atomic(dest/'progress.json',{'epoch':epoch+1,'epochs':n_epochs,'steps':steps,'losses':losses,'elapsed':time.monotonic()-start})
   checkpoint_boundary()
  save_weights(network,dest/'model.safetensors')
  v={'task':task,'rows':len(rows),'rows_sha256':sha(canonical(rows)),'recipe_sha256':recipe,'model':MODEL,'revision':REVISION,
   'weights_sha256':filehash(dest/'model.safetensors'),'epochs':n_epochs,'steps':steps,'losses':losses,
   'elapsed_seconds':time.monotonic()-start,'peak_gpu_mib':round(torch.cuda.max_memory_allocated()/2**20),'trainable_parameters':trainable,
   'policy':POLICY,'actual_microbatch':micro,'actual_accumulation':acc,'source_sha256':source_hashes(),'torch':torch.__version__,'auxiliary_encoder_sha256':warmstart,
   'class_weights':network.class_weights.detach().cpu().tolist() if task in ('document','nli') else None,'class_weight_scope':'current training fold only'}
  atomic(receipt,v,True);return v
 finally:
  budget.stop();del network,optimizer;torch.cuda.empty_cache()

def probe():
 import torch,importlib.metadata as metadata
 from .neural import Network,tokenizer
 seed();d=setup();tok=tokenizer();network=Network('ner',len(tok)).to(d);network.train();torch.cuda.reset_peak_memory_stats()
 batch=tok(['Vitamin D decreases gene expression.']*4,return_tensors='pt',padding=True).to(d)
 start=time.monotonic()
 with torch.autocast('cuda',dtype=torch.bfloat16):loss=network(**batch).square().mean()
 loss.backward();torch.cuda.synchronize()
 gradients=sum(float(p.grad.abs().sum()) for p in network.parameters() if p.grad is not None)
 if not math.isfinite(gradients) or gradients<=0:raise ValueError('역전파 검증 실패')
 v={'device':torch.cuda.get_device_name(0),'capability':torch.cuda.get_device_capability(0),'torch':torch.__version__,
  'cuda':torch.version.cuda,'gradient_sum':gradients,'seconds':time.monotonic()-start,'peak_gpu_mib':round(torch.cuda.max_memory_allocated()/2**20),
  'packages':{k:metadata.version(k) for k in ('torch','transformers','numpy','scikit-learn')},'source_sha256':source_hashes()}
 atomic(HOME/'gpu-probe.json',v,True);log('gpu-probe-complete',**{k:v[k] for k in ('device','torch','seconds','peak_gpu_mib')})
 del network;torch.cuda.empty_cache();return v
