"""Fresh foundation encoder; task weights are never imported from past experiments."""
import math
import torch
from torch import nn
from transformers import AutoModel,AutoTokenizer
from .common import HOME,MODEL,REVISION,TYPES

SPECIAL=['[C]','[/C]','[G]','[/G]','[MENTION]','[/MENTION]']
def tokenizer():
 t=AutoTokenizer.from_pretrained(MODEL,revision=REVISION,cache_dir=str(HOME/'cache/huggingface'),use_fast=True)
 t.add_special_tokens({'additional_special_tokens':SPECIAL});return t
class Network(nn.Module):
 def __init__(self,task,tokenizer_size):
  super().__init__();self.task=task
  self.encoder=AutoModel.from_pretrained(MODEL,revision=REVISION,cache_dir=str(HOME/'cache/huggingface'),attn_implementation='sdpa')
  self.encoder.resize_token_embeddings(tokenizer_size,mean_resizing=False)
  self.encoder.gradient_checkpointing_enable(gradient_checkpointing_kwargs={'use_reentrant':False})
  h=self.encoder.config.hidden_size;self.dropout=nn.Dropout(.1)
  if task=='ner':self.head=nn.Linear(h,6)
  elif task=='dual':self.head=nn.Linear(h,256)
  elif task=='identity' or task=='nli':self.head=nn.Linear(h,1)
  elif task=='auxiliary':self.head=nn.Linear(h,128)
  elif task=='document':
   self.hidden=nn.Sequential(nn.Linear(4*h,512),nn.GELU(),nn.Dropout(.1))
   self.head=nn.Linear(512,len(TYPES)+1);self.direction=nn.Linear(512,3)
  else:raise ValueError(task)
  if task in ('document','nli'):self.register_buffer('class_weights',torch.ones(len(TYPES)+1))
 def forward(self,input_ids,attention_mask,token_type_ids=None,c_mask=None,g_mask=None):
  args={'input_ids':input_ids,'attention_mask':attention_mask}
  if token_type_ids is not None:args['token_type_ids']=token_type_ids
  h=self.encoder(**args).last_hidden_state
  if self.task=='ner':return self.head(self.dropout(h)).reshape(*h.shape[:2],2,3)
  if self.task=='dual':return nn.functional.normalize(self.head(h[:,0]),dim=-1)
  if self.task in ('identity','nli'):return self.head(self.dropout(h[:,0])).squeeze(-1)
  if self.task=='auxiliary':return self.head(self.dropout(h[:,0]))
  def pool(mask):return (h*mask.unsqueeze(-1)).sum(1)/mask.sum(1,keepdim=True).clamp_min(1)
  c,g=pool(c_mask),pool(g_mask)
  score=((h*c.unsqueeze(1)).sum(-1)+(h*g.unsqueeze(1)).sum(-1))/math.sqrt(h.shape[-1])
  weights=score.masked_fill(attention_mask==0,-1e4).softmax(-1);context=(h*weights.unsqueeze(-1)).sum(1)
  v=self.hidden(torch.cat((h[:,0],c,g,context),-1));return {'logits':self.head(v),'direction':self.direction(v)}

def device():
 if not torch.cuda.is_available():raise RuntimeError('GPU 의존성 부재; CPU 성공으로 대체하지 않음')
 if torch.cuda.get_device_properties(0).total_memory<12*1024**3:raise RuntimeError('GPU 메모리 부족')
 return torch.device('cuda:0')
def marker_masks(ids,t):
 out={}
 for kind,key in [('C','c_mask'),('G','g_mask')]:
  left,right=t.convert_tokens_to_ids(['['+kind+']','[/'+kind+']']);mask=[];inside=False
  for token in ids:
   if token==left:inside=True;mask.append(0)
   elif token==right:inside=False;mask.append(0)
   else:mask.append(int(inside))
  if not any(mask):raise ValueError('토큰 창에서 개체 위치 표시가 소실됨')
  out[key]=mask
 return out

def bounded_ids(ids,t,budget):
 """Retain BOTH complete mentions even when their context exceeds the token limit."""
 markers=t.convert_tokens_to_ids(['[C]','[/C]','[G]','[/G]'])
 positions=[ids.index(v) for v in markers]
 a,b=min(positions),max(positions)+1
 if len(ids)<=budget:return ids
 if b-a<=budget:
  start=max(0,min(a-(budget-(b-a))//2,len(ids)-budget));return ids[start:start+budget]
 ranges=sorted([(positions[0],positions[1]+1),(positions[2],positions[3]+1)])
 minimum=sum(b-a for a,b in ranges)
 if minimum+1>budget:raise ValueError('개체 자체 토큰 길이가 지원 창을 초과')
 extra=(budget-1-minimum)//2;chunks=[]
 for a,b in ranges:
  left=min(a,extra//2);right=min(len(ids)-b,extra-left)
  chunks.append(ids[a-left:b+right])
 result=chunks[0]+[t.sep_token_id]+chunks[1]
 if any(v not in result for v in markers):raise ValueError('제한 창의 개체 표식 손실')
 return result

def encode_view(t,text,hypothesis=None):
 ids=t(text,add_special_tokens=False)['input_ids'];pair=t(hypothesis,add_special_tokens=False)['input_ids'][:64] if hypothesis else None
 budget=512-t.num_special_tokens_to_add(pair=pair is not None)-(len(pair) if pair else 0)
 ids=bounded_ids(ids,t,budget);out=t.prepare_for_model(ids,pair_ids=pair,add_special_tokens=True,return_attention_mask=True,return_token_type_ids=True)
 out.update(marker_masks(out['input_ids'],t));return dict(out)
