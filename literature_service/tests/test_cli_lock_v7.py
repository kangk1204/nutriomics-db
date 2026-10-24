import os
import pytest


@pytest.mark.skipif(os.name!='posix',reason='Linux flock regression')
def test_pair_preparation_does_not_hold_its_parsers_nonreentrant_lock(tmp_path,monkeypatch):
    import fcntl
    import sys
    from contextlib import contextmanager,nullcontext
    from nutri_literature import cli_v7,joint_data_v7,operations_v6,operations_v7,resources_v6
    target=tmp_path/'nlp.lock'
    monkeypatch.setenv('LITERATURE_NLP_LOCK',str(target))
    monkeypatch.setattr(operations_v7,'resource_check',lambda _:None)
    monkeypatch.setattr(operations_v6,'processing_slot',lambda _:nullcontext())
    @contextmanager
    def budget():yield {}
    monkeypatch.setattr(resources_v6,'memory_budget',budget)
    def prepare(*args):
        with target.open('a+b') as stream:
            # A parser forward must be able to acquire this independently.
            fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
            fcntl.flock(stream,fcntl.LOCK_UN)
        return {'papers':1}
    monkeypatch.setattr(joint_data_v7,'prepare',prepare)
    monkeypatch.setattr(sys,'argv',['v7','--home',str(tmp_path),'prepare-joint','--features','a','--auxiliary','b','--out','c'])
    cli_v7.main()
