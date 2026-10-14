"""The measurement must use 100 real samples and exercise four-worker mode."""
import json

from scripts import benchmark_explorer_performance as benchmark


def test_warm_and_concurrent_samples_are_not_fabricated(tmp_path,monkeypatch):
    cases=[{"id":"one","path":"/one","params":{},"payload":None,"target_s":1},{"id":"two","path":"/two","params":{},"payload":None,"target_s":3}]
    spec=tmp_path/'cases.json';spec.write_text(json.dumps({"cases":cases,"case_sha256":benchmark.digest(cases)}))
    calls=[]
    def request(base,case):
        calls.append(case['id'])
        return {"seconds":0.2,"status":200,"bytes":5,"release_id":"fixture"},{"release_id":"fixture","items":[]}
    monkeypatch.setattr(benchmark,'request',request)
    report=benchmark.benchmark('fixture',spec,tmp_path/'report',100,False,True)
    assert len(calls)==402
    assert all(r['metrics']['n']==100 for r in report['results'])
    assert all(r['metrics']['n']==100 for r in report['concurrent'].values())
    assert report['all_warm_pass'] and report['all_concurrent_pass']


def test_failed_warmup_cannot_pass(tmp_path,monkeypatch):
    cases=[{"id":"failed","path":"/one","params":{},"target_s":1}]
    spec=tmp_path/'cases.json';spec.write_text(json.dumps({"cases":cases,"case_sha256":benchmark.digest(cases)}))
    monkeypatch.setattr(benchmark,'request',lambda *a:({"seconds":12,"status":503,"error":"timeout"},None))
    r=benchmark.benchmark('fixture',spec,tmp_path/'report',100)
    assert r['all_warm_pass'] is False
    assert r['results'][0]['metrics']['n']==1
    assert r['results'][0]['metrics']['errors']==1


def test_fast_but_unavailable_omics_is_not_a_pass(tmp_path, monkeypatch):
    cases=[{"id":"missing","path":"/one","params":{},"target_s":1}]
    spec=tmp_path/'cases.json';spec.write_text(json.dumps({"cases":cases,"case_sha256":benchmark.digest(cases)}))
    monkeypatch.setattr(benchmark, 'request', lambda *a: ({"seconds":.01,"status":200}, {"contrasts":[{"effects_status":"source_unavailable","effect_total":None}]}))
    r=benchmark.benchmark('fixture',spec,tmp_path/'report',100)
    assert not r['all_warm_pass']
    assert r['results'][0]['metrics']['errors']==1


def test_changed_response_during_run_is_not_a_pass(tmp_path, monkeypatch):
    cases=[{"id":"changed","path":"/one","params":{},"target_s":1}]
    spec=tmp_path/'cases.json';spec.write_text(json.dumps({"cases":cases,"case_sha256":benchmark.digest(cases)}))
    calls=[]
    def request(*a):
        calls.append(1)
        return {"seconds":.01,"status":200}, {"release_id":"before" if len(calls)==1 else "after"}
    monkeypatch.setattr(benchmark, 'request', request)
    r=benchmark.benchmark('fixture',spec,tmp_path/'report',100)
    assert not r['all_warm_pass']
    assert r['results'][0]['metrics']['errors']==100
    assert (tmp_path/'report/samples.csv').is_file()
