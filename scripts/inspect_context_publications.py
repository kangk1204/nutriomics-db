import xml.etree.ElementTree as ET
from pathlib import Path
root=Path('research/2026-09-30/expansion/context-joins')
for path in root.glob('*-pubmed.xml'):
    print(path.name)
    for article in ET.fromstring(path.read_bytes()).findall('PubmedArticle'):
        print(article.findtext('MedlineCitation/PMID'),''.join(article.find('MedlineCitation/Article/ArticleTitle').itertext()))
        print(' '.join(''.join(n.itertext()) for n in article.findall('.//AbstractText'))[:6500])
        print('IDs',[(n.attrib,n.text) for n in article.findall('./PubmedData/ArticleIdList/ArticleId')])
