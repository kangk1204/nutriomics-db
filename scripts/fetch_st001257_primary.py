"""Bounded public acquisition; no login, affirmative acceptance or bulk raw archive."""

import hashlib
import json
import shutil
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

CAP = Path("research/2026-09-30/expansion/cohort-primary/ST001257")
HOST = "www.metabolomicsworkbench.org"


class Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlparse(newurl)
        if (
            parsed.scheme != "https"
            or parsed.hostname != HOST
            or any(token in parsed.path.lower() for token in ["login", "register", "agreement"])
        ):
            raise ValueError("Access/redirect restriction; stop without bypass")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


if __name__ == "__main__":
    CAP.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(CAP).free < 1_000_000_000:
        raise ValueError("1 GB reserve required")
    sources = [
        (
            "untargeted.tsv",
            "https://www.metabolomicsworkbench.org/rest/study/analysis_id/AN002086/untarg_data",
            6_000_000,
        ),
        (
            "factors.json",
            "https://www.metabolomicsworkbench.org/rest/study/study_id/ST001257/factors",
            200_000,
        ),
        (
            "analysis.json",
            "https://www.metabolomicsworkbench.org/rest/study/study_id/ST001257/analysis",
            100_000,
        ),
        (
            "mwtab.txt",
            "https://www.metabolomicsworkbench.org/data/study_textformat_view.php?ANALYSIS_ID=AN002086&MODE=d&STUDY_ID=ST001257",
            200_000,
        ),
    ]
    opener = urllib.request.build_opener(Redirect())
    manifest = []
    if (CAP / "manifest.json").exists():
        raise ValueError("Capture exists; preserve checkpoint and use offline importer")
    for name, url, cap in sources:
        with opener.open(
            urllib.request.Request(
                url, headers={"User-Agent": "NutriGenomics provenance audit/1.0"}
            ),
            timeout=30,
        ) as response:
            if int(response.headers.get("Content-Length", 0)) > cap:
                raise ValueError("Source exceeds bounded cap")
            data = response.read(cap + 1)
            content_type = response.headers.get("Content-Type", "")
        if len(data) > cap:
            raise ValueError("Source exceeds bounded cap")
        lower = data[:20000].lower()
        if "text/html" in content_type or any(
            token in lower
            for token in [b"<html", b"captcha", b"accept agreement", b"sign in to download"]
        ):
            raise ValueError("Unexpected HTML or access gate; stop without bypass")
        if name.endswith(".json"):
            json.loads(data)
        if name == "untargeted.tsv" and not data.startswith(b"Samples\tgroup\t"):
            raise ValueError("Expected public feature matrix")
        (CAP / name).write_bytes(data)
        manifest.append(
            {
                "file": name,
                "url": url,
                "retrieved_at": datetime.now(UTC).isoformat(),
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
                "content_type": content_type,
            }
        )
        print(name, len(data), flush=True)
    (CAP / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
