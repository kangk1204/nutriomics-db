import hashlib
import json
from pathlib import Path

import pytest

from nutri_literature import dictionaries
from nutri_literature.extraction import extract
from nutri_literature.settings import Settings


def settings(tmp_path):
    return Settings(tmp_path, Path(__file__).resolve().parents[1] / "config" / "topics.json")


def install_snapshot(config, raw):
    config.dictionaries.mkdir(parents=True)
    sha = hashlib.sha256(raw).hexdigest()
    filename = f"hgnc-{sha}.tsv"
    (config.dictionaries / filename).write_bytes(raw)
    (config.dictionaries / "hgnc-active.json").write_text(
        json.dumps(
            {
                "path": filename,
                "sha256": sha,
                "version": f"hgnc-sha256:{sha}",
                "source": dictionaries.HGNC_URL,
            }
        ),
        encoding="utf-8",
    )
    return filename


def test_snapshot_hash_and_human_normalization(tmp_path):
    config = settings(tmp_path)
    raw = b"hgnc_id\tsymbol\tname\tstatus\talias_symbol\tprev_symbol\nHGNC:12679\tVDR\tvitamin D receptor\tApproved\tNR1I1\t\n"
    filename = install_snapshot(config, raw)
    dictionary = dictionaries.load_dictionaries(config)
    text = "Vitamin D increased VDR gene expression in human cells."
    result = extract(text, [{"label": "Results", "start": 0, "end": len(text)}], {}, dictionary)
    gene = next(m for m in result["mentions"] if m["kind"] == "gene")
    assert gene["normalized_id"] == "HGNC:12679"
    assert dictionary.manifest["fallback"] is False
    (config.dictionaries / filename).write_bytes(raw + b"corruption")
    with pytest.raises(ValueError, match="hash mismatch"):
        dictionaries.load_dictionaries(config)


def test_ambiguous_alias_never_selects_arbitrary_gene(tmp_path):
    config = settings(tmp_path)
    raw = b"hgnc_id\tsymbol\tname\tstatus\talias_symbol\nHGNC:1\tABC1\tfictional test row one\tApproved\tXYZ\nHGNC:2\tABC2\tfictional test row two\tApproved\tXYZ\n"
    install_snapshot(config, raw)
    dictionary = dictionaries.load_dictionaries(config)
    text = "Caffeine increased XYZ gene expression in human adults."
    gene = next(m for m in extract(text, [], {}, dictionary)["mentions"] if m["kind"] == "gene")
    assert gene["normalized_id"] is None
    assert gene["status"] == "needs_review"
    assert gene["payload"]["candidate_symbols"] == ["ABC1", "ABC2"]


def test_update_rejects_truncated_complete_set_before_activation(tmp_path, monkeypatch):
    config = settings(tmp_path)
    raw = b"hgnc_id\tsymbol\tname\tstatus\nHGNC:12679\tVDR\tvitamin D receptor\tApproved\n"

    class Response:
        content = raw
        headers = {}

        def raise_for_status(self):
            pass

    monkeypatch.setattr(dictionaries.requests, "get", lambda *a, **k: Response())
    with pytest.raises(ValueError, match="truncated"):
        dictionaries.update_hgnc(config)
    assert not (config.dictionaries / "hgnc-active.json").exists()


def test_nonhuman_never_normalizes_to_official_human_id(tmp_path):
    config = settings(tmp_path)
    raw = b"hgnc_id\tsymbol\tname\tstatus\nHGNC:12679\tVDR\tvitamin D receptor\tApproved\n"
    install_snapshot(config, raw)
    text = "Vitamin D increased Vdr gene expression in mice."
    gene = next(
        m
        for m in extract(text, [], {}, dictionaries.load_dictionaries(config))["mentions"]
        if m["kind"] == "gene"
    )
    assert gene["normalized_id"] is None
    assert gene["payload"]["organism"] == "animal"


def test_unstated_species_never_normalizes_to_human_id(tmp_path):
    config = settings(tmp_path)
    raw = b"hgnc_id\tsymbol\tname\tstatus\nHGNC:12679\tVDR\tvitamin D receptor\tApproved\n"
    install_snapshot(config, raw)
    text = "Vitamin D increased VDR gene expression."
    gene = next(
        m
        for m in extract(text, [], {}, dictionaries.load_dictionaries(config))["mentions"]
        if m["kind"] == "gene"
    )
    assert gene["normalized_id"] is None
    assert gene["payload"]["organism"] == "unknown"


def test_curated_config_is_readable_and_covers_six_topics(tmp_path):
    dictionary = dictionaries.load_dictionaries(settings(tmp_path))
    text = "DASH diet; omega-3; vitamin D; caffeine; cocoa; flavanols; DNA methylation; hypertension"
    result = extract(text, [], {}, dictionary)
    ids = {m["normalized_id"] for m in result["mentions"]}
    assert {
        "local:DASH",
        "local:omega3",
        "local:vitaminD",
        "local:caffeine",
        "local:cocoa",
        "local:flavanol",
        "local:DNA-methylation",
        "local:hypertension",
    } <= ids
