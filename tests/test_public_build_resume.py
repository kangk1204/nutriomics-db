import json

import pytest
from test_explorer_api import explorer as explorer  # noqa: PLC0414
from test_explorer_scientific_api import _science

from nutrigenomics.db import create_schema
from nutrigenomics.explorer_build import sha256
from nutrigenomics.publication import build_candidate
from scripts.finalize_public_service_build import finalize
from scripts.prepare_public_service_release import prepare


def test_resume_only_private_build_and_preserve_original(explorer, tmp_path):
    _science(explorer)
    parent = explorer["db"].parent
    (parent / "manifest.json").write_text(json.dumps({"release_id": "parent", "database_sha256": sha256(explorer["db"])}))
    core = tmp_path / "core.sqlite"
    create_schema("sqlite:///" + core.as_posix())
    policy = tmp_path / "policy.json"
    policy.write_text('{"analyses":[]}')
    candidate = tmp_path / "candidate"
    build_candidate(parent, core, policy, candidate, "candidate")
    original_hash = sha256(candidate / "nutriomics.sqlite3")
    output = tmp_path / "service"
    prepare(candidate, None, output, "service")
    with pytest.raises(ValueError, match="private"):
        finalize(output, candidate, output)
    staging = output.with_name(output.name + ".building")
    output.rename(staging)  # Independent fixture simulates an unactivated build.
    result = finalize(staging, candidate, output)
    assert result["scientific_review"] == "pending"
    assert sha256(candidate / "nutriomics.sqlite3") == original_hash
    assert (output / "build-audit/quality-before-key-normalization.json").is_file()
    assert sha256(output / "nutriomics.sqlite3") == result["database_sha256"]
