"""Fixed official USDA codebook and existing-study publication captures."""
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from nutrigenomics.expansion import ALLOWED_HOSTS, fetch
ROOT=Path('research/2026-09-30/expansion/context-joins')
URLS={
    'USDA-supporting-2022.zip':'https://fdc.nal.usda.gov/fdc-datasets/FoodData_Central_Supporting_Data_csv_2022-10-28.zip',
    'USDA-SR28-documentation.pdf':'https://www.ars.usda.gov/ARSUserFiles/80400525/Data/SR/SR28/sr28_doc.pdf',
    'USDA-SR-legacy-documentation.pdf':'https://www.ars.usda.gov/ARSUserFiles/80400525/Data/SR-Legacy/SR-Legacy_Doc.pdf',
    'fish-oil-trial.html':'https://pmc.ncbi.nlm.nih.gov/articles/PMC9286410/',
}


def main():
    ALLOWED_HOSTS.add('www.ars.usda.gov')
    manifest=ROOT/'definitions-manifest.json'
    if manifest.exists():raise ValueError('Replay existing captures')
    rows=[]
    for name,url in URLS.items():
        try:
            row=fetch(url,ROOT/name,max_bytes=5_000_000)
            row['file']=name;rows.append(row)
            print(name,row['bytes'])
        except (OSError,ValueError) as error:
            rows.append({'file':name,'url':url,'error':str(error)})
            print(name,error)
    manifest.write_text(json.dumps(rows,indent=2),encoding='utf-8')


if __name__=='__main__':main()
