"""A portable source copy must not erase the exact executed-byte identity."""
import hashlib

from scripts.package_workbench_code import portable_r_sources


def test_default_path_translation_preserves_original_bytes_and_line_endings(tmp_path):
    folder = tmp_path / 'scripts'
    folder.mkdir()
    original = b'root<-arg_value("--root","/home/research/projects/example/expansion/2026-10-01")\r\nprint(root)\r\n'
    path = folder / 'example.R'
    path.write_bytes(original)
    result = portable_r_sources(tmp_path)
    assert len(result) == 1
    row = result[0]
    assert (tmp_path / row['original_bytes_path']).read_bytes() == original
    assert row['original_sha256'] == hashlib.sha256(original).hexdigest()
    assert row['portable_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert path.read_bytes().count(b'\r\n') == 2
    assert b'NUTRIOMICS_ANALYSIS_ROOT' in path.read_bytes()
    assert portable_r_sources(tmp_path) == result
    assert (tmp_path / row['original_bytes_path']).read_bytes() == original
