"""Transparent, safe error-code diagnostics; no request or stream changes."""
import json
import re
import uuid
from pathlib import Path
import instrument_chatgpt_repair_v4 as observer

OriginalResponse=observer.ObservedResponse

class TriageResponse(OriginalResponse):
    def capture(self,pieces,at_eof=False):
        super().capture(pieces,at_eof)
        if not pieces:return
        try:event=json.loads('\n'.join(pieces))
        except (ValueError,TypeError):return
        if event.get('type') not in ('error','response.failed','response.incomplete'):return
        response=event.get('response') or {}
        error=event.get('error') or response.get('error') or {}
        if not isinstance(error,dict):return
        safe=lambda v: v if isinstance(v,str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}',v) else observer.safe_code(v)
        message=str(error.get('message') or '').lower()
        flags={name:bool(re.search(pattern,message)) for name,pattern in {
            'context_length':r'context.{0,30}(length|window|limit)|too.{0,10}many.{0,10}tokens|input.{0,10}too.{0,10}long',
            'quota':r'quota|usage.{0,20}limit|exhaust|rate.{0,10}limit',
            'authentication':r'authenticat|unauthoriz|credential|token.{0,10}expired',
            'temporary_server':r'temporar|internal.{0,10}error|server.{0,10}error|try.{0,10}again|overload',
            'request_validation':r'invalid.{0,20}(request|argument|input|parameter)|unsupported',
        }.items()}
        observer.immutable(observer.HOME/'transport-triage'/(uuid.uuid4().hex+'.json'),{
            **self.metadata,'event':event.get('type'),'error_code':safe(error.get('code')),
            'error_type':safe(error.get('type')),'message_categories':flags,
            'raw_message_or_credentials_logged':False,'stream_modified':False})

if __name__=='__main__':
    observer.ObservedResponse=TriageResponse
    receipt={'source_sha256':observer.sha(Path(__file__).read_bytes()),
        'base_observer_sha256':observer.sha(Path(observer.__file__).read_bytes()),
        'request_or_stream_modified':False,'raw_error_message_logged':False}
    observer.immutable(observer.HOME/'transport-triage-observer.json',receipt)
    observer.immutable(observer.INFERENCE_HOME/'train/transport/diagnostics/triage-observer-manifest.json',receipt)
    observer.main()
