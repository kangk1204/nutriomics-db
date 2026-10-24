def test_fixed_cache_reuses_only_label_free_states(tmp_path, monkeypatch):
    import pytest
    torch=pytest.importorskip('torch')
    from types import SimpleNamespace
    from nutri_literature.context_cache_v7 import fixed_context
    monkeypatch.setenv('LITERATURE_V7_CONTEXT_CACHE', str(tmp_path))
    calls=[]
    class Encoder:
        identity='fixed-test'
        model=SimpleNamespace(eval=lambda:None)
        def encode(self,text):
            calls.append(text)
            return torch.tensor([[1.,2.],[3.,4.]]),[(0,1),(2,3)]
    model=SimpleNamespace(encoder=Encoder(),adapt=False)
    doc={'text':'A B','pairs':[{'chemical':{'start':0,'end':1,'text':'A'},
         'gene':{'start':2,'end':3,'text':'B'},'sentence':{'start':0,'end':3,'text':'A B'}}]}
    first=fixed_context(model,doc)
    doc['gold_keys']=['changed heldout labels must have no effect']
    second=fixed_context(model,doc)
    assert len(calls)==1 and torch.equal(first[2],second[2])
    model.adapt=True
    with pytest.raises(ValueError):fixed_context(model,doc)
