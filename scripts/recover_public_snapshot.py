"""Recover a successful earlier bounded capture; retain rejected bytes for audit."""

import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

root = Path("research/2026-09-30/expansion")
manifest_path = root / "snapshots/manifest.json"
manifest = json.loads(manifest_path.read_text())
old = next(row for row in manifest["files"] if row["source"] == "hall")
payload = (root / "snapshots" / old["file"]).read_bytes()
if b"recaptcha" in payload[:20000].lower():
    manifest["files"].remove(old)
    manifest["failures"].append(
        {
            "file": old["file"],
            "reason": "HTML CAPTCHA returned with HTTP 200; quarantined, not ingested",
            "sha256": old["sha256"],
        }
    )
    source = root / "discovery/hall_html.txt"
    recovered = source.read_bytes()
    if b"508" not in recovered or b"31105044" not in recovered:
        raise ValueError("recovery identity/estimate anchors missing")
    target = root / "snapshots/hall-recovered.html"
    shutil.copyfile(source, target)
    row = dict(
        old,
        file=target.name,
        local_path=str(target),
        retrieved_at=datetime.fromtimestamp(source.stat().st_mtime, UTC).isoformat(),
        retrieved_at_basis="filesystem modification time of earlier successful bounded discovery capture",
        sha256=hashlib.sha256(recovered).hexdigest(),
        bytes=len(recovered),
        recovery_source="discovery/hall_html.txt",
    )
    manifest["files"].append(row)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("Recovered Hall publication; rejected bytes retained in hall.html")
