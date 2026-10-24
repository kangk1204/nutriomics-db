import json
from scripts import instrument_chatgpt_repair_v4 as observer

class FakeResponse:
    status_code=200
    headers={'Authorization':'never-log-this','Retry-After':'15'}
    def __init__(self,lines):self.lines=lines;self.closed=False
    def iter_lines(self,*args,**kwargs):yield from self.lines
    def close(self):self.closed=True

def test_transparent_stream_and_safe_terminal_metadata(tmp_path,monkeypatch):
    monkeypatch.setattr(observer,'TRACE',tmp_path)
    payload={'type':'response.failed','response':{'status':'failed','error':{'code':'server_error','message':'private text never log'}}}
    lines=['data: '+json.dumps(payload),'']
    raw=FakeResponse(lines);wrapped=observer.ObservedResponse(raw,{'model':'gpt-6-luna'})
    assert list(wrapped.iter_lines())==lines
    wrapped.close()
    saved=json.loads(next(tmp_path.glob('*.json')).read_text())
    assert saved['terminal_signals'][0]['error_code']=='server_error'
    assert saved['retry_after_seconds']==15 and raw.closed
    assert 'private text' not in json.dumps(saved)
    assert 'never-log-this' not in json.dumps(saved)

def test_eof_diagnostic_does_not_repair_or_accept_incomplete_framing(tmp_path,monkeypatch):
    monkeypatch.setattr(observer,'TRACE',tmp_path)
    payload={'type':'response.completed','response':{'status':'completed'}}
    lines=['data: '+json.dumps(payload)]
    raw=FakeResponse(lines);wrapped=observer.ObservedResponse(raw,{})
    assert list(wrapped.iter_lines())==lines
    wrapped.close()
    saved=json.loads(next(tmp_path.glob('*.json')).read_text())
    assert saved['terminal_signals'][0]['at_eof_without_delimiter'] is True
    assert saved['request_body_or_stream_modified'] is False
