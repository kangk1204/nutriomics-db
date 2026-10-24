"""Cross-process resource serialization; no NLP installation is required."""

import multiprocessing
from contextlib import contextmanager

from nutri_literature.nlp_v2 import cpu_inference_lock


def _competing_process(path, connection):
    connection.send("attempting")
    with cpu_inference_lock(path):
        connection.send("acquired")
    connection.close()


def _holding_process(path, connection):
    with cpu_inference_lock(path):
        connection.send("acquired")
        connection.recv()


def test_cross_process_forward_lock_blocks_and_releases(tmp_path):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    path = tmp_path / "cpu.lock"
    process = context.Process(target=_competing_process, args=(str(path), child))
    try:
        with cpu_inference_lock(path):
            process.start()
            child.close()
            assert parent.poll(10)
            assert parent.recv() == "attempting"
            assert not parent.poll(0.2), "second process entered concurrent CPU inference"
        assert parent.poll(10)
        assert parent.recv() == "acquired"
        process.join(10)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(10)
        parent.close()


def test_process_termination_releases_os_lock(tmp_path):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    path = tmp_path / "cpu.lock"
    process = context.Process(target=_holding_process, args=(str(path), child))
    process.start()
    child.close()
    try:
        assert parent.poll(10)
        assert parent.recv() == "acquired"
        process.terminate()
        process.join(10)
        with cpu_inference_lock(path):
            assert path.exists()
    finally:
        if process.is_alive():
            process.terminate()
            process.join(10)
        parent.close()


def test_configured_global_path_overrides_isolated_home(tmp_path, monkeypatch):
    shared = tmp_path / "shared" / "cpu.lock"
    monkeypatch.setenv("LITERATURE_HOME", str(tmp_path / "isolated-home"))
    monkeypatch.setenv("LITERATURE_NLP_LOCK", str(shared))
    with cpu_inference_lock() as actual:
        assert actual == shared
    assert not (tmp_path / "isolated-home" / "locks" / "nlp-cpu.lock").exists()


def test_actual_forward_path_uses_global_lock_but_cache_read_does_not(monkeypatch):
    from nutri_literature import nlp_v2

    calls = []

    @contextmanager
    def lock():
        calls.append("enter")
        try:
            yield
        finally:
            calls.append("exit")

    class Token:
        i, idx = 0, 0
        text, lemma_, dep_, pos_ = "caffeine", "caffeine", "ROOT", "NOUN"

        @property
        def head(self):
            return self

    monkeypatch.setattr(nlp_v2, "cpu_inference_lock", lock)
    parser = nlp_v2.CpuParser()
    parser._nlp = lambda text: [Token()]
    first = parser.parse({"text": "caffeine", "start": 0, "end": 8})
    second = parser.parse({"text": "caffeine", "start": 10, "end": 18})
    assert calls == ["enter", "exit"]
    assert first[0]["start"] == 0 and second[0]["start"] == 10
