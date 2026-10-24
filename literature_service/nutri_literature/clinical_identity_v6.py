"""Preserve PMID absence explicitly; verify PMC-only cohort provenance."""

import json
from pathlib import Path
import re

from defusedxml import ElementTree

from .db import digest


def auditable_identity(document, folder):
    if document.get("pmid_mapping_known") is True:
        return str(document.get("pmid", "")).isdigit()
    pmcid = str(document.get("pmcid", ""))
    if not re.fullmatch(r"PMC\d+", pmcid) or document.get("pmid") != pmcid:
        return False
    if document.get("identifier_resolution") != "confirmed_pmc_only":
        return False
    receipts = document.get("pmc_only_receipts", {})

    def read(kind, required_url):
        record = receipts.get(kind, {})
        name = record.get("filename", "")
        if not name or Path(name).name != name or record.get("url") != required_url:
            raise ValueError("PMC-only identity receipt metadata differs")
        raw = (Path(folder) / name).read_bytes()
        if digest(raw) != record.get("sha256"):
            raise ValueError("PMC-only identity receipt hash differs")
        return raw

    try:
        response = json.loads(read("europepmc", "https://www.ebi.ac.uk/europepmc/webservices/rest/search"))
        records = response.get("resultList", {}).get("result", [])
        matches = [r for r in records if r.get("pmcid") == pmcid]
        if (len(matches) != 1 or matches[0].get("source") != "PMC"
                or matches[0].get("id") != pmcid or matches[0].get("pmid")
                or response.get("request", {}).get("queryString") != pmcid):
            return False
        raw = read("ncbi", "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi")
        root = ElementTree.fromstring(raw, forbid_dtd=False, forbid_entities=True, forbid_external=True)
        links = [r for r in root.findall("LinkSet") if r.findtext("IdList/Id") == pmcid[3:]]
        return len(links) == 1 and not links[0].findall("LinkSetDb")
    except (KeyError, ValueError, OSError):
        return False
