"""Small read-only discovery requests; no assay downloads."""

import urllib.request
from pathlib import Path

queries = {
    "geo_trials": "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=gds&term=%28diet+OR+resveratrol+OR+omega-3%29+AND+Homo+sapiens%5BOrganism%5D+AND+gse%5BEntry+Type%5D+AND+%28randomized+OR+randomised+OR+volunteers%29&retmax=20&retmode=json",
    "wastyk_html": "https://pmc.ncbi.nlm.nih.gov/articles/PMC9020749/",
    "david_html": "https://pmc.ncbi.nlm.nih.gov/articles/PMC3957428/",
    "hall_html": "https://pmc.ncbi.nlm.nih.gov/articles/PMC7946062/",
    "geo_diet": "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=gds&term=diet%5BTitle%5D+AND+Homo+sapiens%5BOrganism%5D+AND+gse%5BEntry+Type%5D&retmax=15&retmode=json",
    "wastyk": "https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=EXT_ID:34256014&format=json",
    "david": "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC3957428/fullTextXML",
    "hall": "https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=TITLE:%22Ultra-Processed%20Diets%20Cause%20Excess%20Calorie%20Intake%22&format=json",
}
root = Path("research/2026-09-30/expansion/discovery")
root.mkdir(parents=True, exist_ok=True)
for key, url in queries.items():
    try:
        with urllib.request.urlopen(url, timeout=25) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("discovery byte cap exceeded")
        (root / (key + ".txt")).write_bytes(raw)
        if key.endswith("_html") or key == "david":
            import re

            print(key, sorted(set(re.findall(r"(?:PRJNA|SRP|ERP|GSE|SRX|PXD)\d+", raw.decode()))))
        else:
            print(key, raw.decode()[:7000])
    except (OSError, ValueError) as error:
        print(key, type(error).__name__, str(error))
