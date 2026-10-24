"""Transparent transport observability; original request, parser and scientific code unchanged."""
import os
import sys
import json
import uuid
from pathlib import Path
from collections import Counter
from datetime import datetime,timezone
for name in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):
    os.environ[name]='1'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import requests
from nutri_literature.repair_v4.common import HOME,INFERENCE_HOME,immutable,canonical,sha

TRACE=HOME/'transport-diagnostics'
SAFE_CODES={'server_error','internal_server_error','rate_limit_exceeded','rate_limit_error',
    'insufficient_quota','model_error','invalid_request_error','overloaded_error','request_timeout',
    'max_output_tokens','content_filter','safety','usage_limit','cancelled'}

def safe_code(value):
    if value is None:return None
    return value if isinstance(value,str) and value in SAFE_CODES else 'other:'+sha(str(value))[:16]

class ObservedResponse:
    def __init__(self,response,metadata):
        self.original=response;self.metadata=metadata;self.counts=Counter();self.signals=[]
        self.identifier=uuid.uuid4().hex;self.started=datetime.now(timezone.utc).isoformat()

    def __getattr__(self,name):return getattr(self.original,name)

    def capture(self,pieces,at_eof=False):
        if not pieces:return
        try:event=json.loads('\n'.join(pieces))
        except (ValueError,TypeError):return
        kind=event.get('type')
        token=kind if kind in {'response.created','response.in_progress','response.completed','response.failed',
            'response.incomplete','response.output_text.done','response.output_text.delta','error'} else 'other'
        self.counts[token]+=1
        if kind not in {'response.completed','response.failed','response.incomplete','error'}:return
        response=event.get('response') if isinstance(event.get('response'),dict) else {}
        error=event.get('error') or response.get('error') or {}
        error=error if isinstance(error,dict) else {}
        details=response.get('incomplete_details') or {}
        usage=response.get('usage') or {}
        self.signals.append({'event':token,'at_eof_without_delimiter':at_eof,
            'response_status':response.get('status') if response.get('status') in ('completed','failed','incomplete','cancelled','in_progress') else None,
            'error_code':safe_code(error.get('code')),'error_type':safe_code(error.get('type')),
            'incomplete_reason':safe_code(details.get('reason')),
            'input_tokens':usage.get('input_tokens') if isinstance(usage.get('input_tokens'),int) else None,
            'output_tokens':usage.get('output_tokens') if isinstance(usage.get('output_tokens'),int) else None})

    def iter_lines(self,*args,**kwargs):
        pieces=[]
        try:
            for line in self.original.iter_lines(*args,**kwargs):
                decoded=line.decode('utf8') if isinstance(line,bytes) else line
                if decoded:
                    if decoded.startswith('data:'):pieces.append(decoded[5:].lstrip())
                else:
                    try:self.capture(pieces)
                    except Exception:pass
                    pieces=[]
                # Never add, drop, normalize or change a line passed to the official client.
                yield line
        finally:
            try:self.capture(pieces,at_eof=True)
            except Exception:pass

    def close(self):
        try:
            retry=self.original.headers.get('Retry-After')
            retry_seconds=int(retry) if isinstance(retry,str) and retry.isdigit() and int(retry)<=86400 else None
            immutable(TRACE/(self.identifier+'.json'),{**self.metadata,'started_utc':self.started,
                'finished_utc':datetime.now(timezone.utc).isoformat(),'http_status':self.original.status_code,
                'retry_after_seconds':retry_seconds,'event_counts':dict(self.counts),'terminal_signals':self.signals,
                'request_or_response_text_logged':False,'authorization_logged':False,'raw_headers_logged':False,
                'request_body_or_stream_modified':False})
        except Exception:
            # An observer failure must not change an otherwise valid model request.
            pass
        finally:self.original.close()

class ObservingSession(requests.Session):
    def post(self,*args,**kwargs):
        body=kwargs.get('json') or {}
        metadata={'api_request_body_sha256':sha(canonical(body)),
            'model':body.get('model') if body.get('model') in ('gpt-6.1-sol','gpt-6-luna') else None,
            'reasoning_effort':(body.get('reasoning') or {}).get('effort')}
        return ObservedResponse(super().post(*args,**kwargs),metadata)

def main():
    import psutil
    try:psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except (AttributeError,PermissionError):pass
    from nutri_literature import chatgpt_client
    original=chatgpt_client.ChatGPTPlanClient
    class ObservedClient(original):
        def __init__(self,*args,**kwargs):
            kwargs['session']=ObservingSession();super().__init__(*args,**kwargs)
    receipt={'schema':'repair-v4-transparent-transport-observer','source_sha256':sha(Path(__file__).read_bytes()),
        'official_client_sha256':sha(Path(chatgpt_client.__file__).read_bytes()),
        'request_body_modified':False,'stream_lines_modified':False,'scientific_algorithm_modified':False,
        'credential_text_logged':False,'response_text_logged':False}
    immutable(HOME/'transport-observer.json',receipt)
    immutable(INFERENCE_HOME/'train/transport/diagnostics/observer-manifest.json',receipt)
    chatgpt_client.ChatGPTPlanClient=ObservedClient
    try:
        from nutri_literature.repair_v4.runner import main as run
        run()
    finally:chatgpt_client.ChatGPTPlanClient=original

if __name__=='__main__':main()
