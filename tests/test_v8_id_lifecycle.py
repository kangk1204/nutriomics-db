"""The full OOF corpus must not remain resident during nested regeneration."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'literature_service'))
from nutri_literature.evidence_v8 import ner, runner


def test_nested_id_partitions_start_before_full_example_profile_is_loaded(monkeypatch):
    docs=[{'pmid':'1','text':'first'},{'pmid':'2','text':'second'}]
    monkeypatch.setattr(runner,'documents',lambda _:docs)
    monkeypatch.setattr(runner,'groups',lambda *_:{'1':0,'2':1})
    monkeypatch.setattr(runner,'checkpoint',lambda:None)
    def forbidden(*_):
        raise AssertionError('Full OOF profile retained before nested partitions')
    monkeypatch.setattr(runner,'example_rows',forbidden)
    class PartitionReached(Exception):pass
    def stop_at_partition(partition):
        assert [d['pmid'] for d in partition]==['2']
        raise PartitionReached
    monkeypatch.setattr(ner,'crossfit',stop_at_partition)
    with pytest.raises(PartitionReached):runner.train('id')
