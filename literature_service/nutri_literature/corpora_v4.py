"""Fresh official holdout ingestion; source inputs and annotations stay separate."""
from collections import defaultdict
import csv
import io
import json
from pathlib import Path
import re
import time
import tarfile
import zipfile
from defusedxml import ElementTree
from .db import digest, dumps, now
from .training_v4 import rct_documents, text_key


def bioc_xml(raw, gene=False):
    root=ElementTree.fromstring(raw,forbid_dtd=False,forbid_entities=True,forbid_external=True)
    docs=[]
    for document in root.findall("document"):
        passages=document.findall("passage")
        length=max(int(p.findtext("offset"))+len(p.findtext("text") or "") for p in passages)
        chars=[" "]*length;sections=[];pmid=document.findtext("id")
        for passage in passages:
            info={i.get("key"):i.text for i in passage.findall("infon")}
            pmid=info.get("article-id_pmid",pmid)
            text=passage.findtext("text") or "";offset=int(passage.findtext("offset"))
            if not text:
                continue
            chars[offset:offset+len(text)]=text
            sections.append({"start":offset,"end":offset+len(text),"label":info.get("section_type",info.get("type","Abstract"))})
        text="".join(chars);entities=[]
        for annotation in document.findall(".//annotation"):
            info={i.get("key"):i.text for i in annotation.findall("infon")}
            if gene:
                if info.get("type") not in {"Gene","GENERIF"}:
                    continue
                identifiers=info.get("NCBI Gene identifier",info.get("identifier",""))
            else:
                if info.get("type","").casefold()!="chemical":
                    continue
                identifiers=info.get("identifier","")
            ids=[s.removeprefix("MESH:").removeprefix("MeSH:") for s in re.split(r"[;,|]",identifiers) if s and s!="-"]
            for location in annotation.findall("location"):
                start=int(location.get("offset"));end=start+int(location.get("length"))
                if not 0<=start<end<=len(text):
                    raise ValueError("Official BioC annotation falls outside source")
                # Curated display names can differ (e.g. b versus Greek beta).
                # Native positions are retained without realignment; both the
                # original annotation surface and exact source slice are saved.
                entities.append({"kind":"gene" if gene else "component","start":start,"end":end,"text":text[start:end],"native_ids":ids,
                                 "published_annotation_text":annotation.findtext("text"),
                                 "source_surface_differs":annotation.findtext("text")!=text[start:end]})
        docs.append({"pmid":str(pmid),"pmcid":None if gene else "PMC"+document.findtext("id"),"text":text,"sections":sections,
                     "entities":entities,"relations":[],"source_hash":digest(text)})
    return docs


def _download(url,path):
    import requests
    if not path.exists():
        response=requests.get(url,timeout=(15,180));response.raise_for_status();path.write_bytes(response.content)
    return path.read_bytes()


def prepare(sources, output, lexicon_path, clinical_manifest, benchmarks=None):
    """Audit overlap without removing difficult test cases from the denominator."""
    sources,output=Path(sources),Path(output);output.mkdir(parents=True,exist_ok=True)
    import requests
    corpora={}
    held_ids=sources/"nlmgene-heldout-ids.txt"
    ids=_download("https://ftp.ncbi.nlm.nih.gov/pub/lu/NLMGene/Pmidlist.Test.txt",held_ids).decode().split()
    gene_docs=[]
    for i,pmid in enumerate(ids):
        path=sources/("nlmgene-"+pmid+".xml")
        gene_docs.extend(bioc_xml(_download("https://ftp.ncbi.nlm.nih.gov/pub/lu/NLMGene/Corpus/"+pmid+".BioC.XML",path),True))
        if i%20==0:
            print(dumps({"holdout":"nlmgene","downloaded":i+1}),flush=True)
        time.sleep(.12)
    corpora["nlmgene"]=(gene_docs,["gene_detection","normalization"],"https://ftp.ncbi.nlm.nih.gov/pub/lu/NLMGene/")
    raw=_download("https://ftp.ncbi.nlm.nih.gov/pub/lu/NLMChem/NLM-Chem-corpus.zip",sources/"nlmchem.zip")
    z=zipfile.ZipFile(io.BytesIO(raw));test_ids=z.read("FINAL_v1/pmcids_test.txt").decode().split()
    chemical=[]
    for pmcid in test_ids:
        chemical.extend(bioc_xml(z.read("FINAL_v1/ALL/"+pmcid+"_v1.xml")))
    corpora["nlmchem"]=(chemical,["normalization"],"https://ftp.ncbi.nlm.nih.gov/pub/lu/NLMChem/")
    (sources/"nlmchem-heldout-ids.txt").write_text("\n".join(d["pmid"] for d in chemical))
    rct=[]
    for doc in rct_documents(sources/"rct-test.txt",True):
        parts=[];roles=[];offset=0
        for row in doc["sentences"]:
            parts.append(row["text"]);roles.append({"start":offset,"end":offset+len(row["text"]),"text":row["text"],"label":row["label"]})
            offset+=len(row["text"])+1
        text="\n".join(parts)
        rct.append({"pmid":doc["pmid"],"text":text,"sections":[{"start":0,"end":len(text),"label":"Abstract"}],
                    "source_hash":digest(text),"entities":[],"relations":[],"rhetorical_labels":roles})
    corpora["pubmedrct"]=(rct,[],"https://github.com/Franck-Dernoncourt/pubmed-rct")
    raw=_download("https://evidence-inference.ebm-nlp.com/v2.0.tar.gz",sources/"evidenceinference.tar.gz")
    with tarfile.open(fileobj=io.BytesIO(raw),mode="r:gz") as archive:
        def read(name):
            return archive.extractfile(name).read().decode("utf-8-sig")
        test=set(read("splits/ev2_test_article_ids.txt").split())
        train=set(read("splits/ev2_train_article_ids.txt").split())
        dev=set(read("splits/ev2_validation_article_ids.txt").split())
        if test&(train|dev):
            raise ValueError("Evidence Inference official article splits overlap")
        prompts={r["PromptID"]:r for r in csv.DictReader(io.StringIO(read("prompts_merged.csv"))) if r["PMCID"] in test}
        annotations=defaultdict(list)
        for row in csv.DictReader(io.StringIO(read("annotations_merged.csv"))):
            if row["PromptID"] in prompts and row["Valid Label"]=="1" and row["Valid Reasoning"]=="1":
                annotations[row["PromptID"]].append(row)
        # Official PMCID-to-PMID conversion permits article-level train overlap checks.
        pmids={}
        ids=sorted(test)
        for start in range(0,len(ids),100):
            chunk=ids[start:start+100]
            path=sources/("ei-idmap-"+digest(dumps(chunk))+".json")
            if not path.exists():
                response=requests.get("https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/",
                                      params={"ids":",".join("PMC"+i for i in chunk),"format":"json"},timeout=90)
                if response.status_code==403:
                    # Another official ID mapping interface; no inferred links.
                    response=requests.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi",
                        params=[("dbfrom","pmc"),("db","pubmed"),("linkname","pmc_pubmed")]+[("id",i) for i in chunk],timeout=90)
                    response.raise_for_status()
                    (sources/(digest(response.content)+".elink.xml")).write_bytes(response.content)
                    mapped=[]
                    for node in ElementTree.fromstring(response.content).findall("LinkSet"):
                        src=node.findtext("IdList/Id"); targets=node.findall("LinkSetDb/Link/Id")
                        if src and len(targets)==1:
                            mapped.append({"pmcid":"PMC"+src,"pmid":targets[0].text})
                    path.write_text(dumps({"records":mapped,"source_url":response.url}),encoding="utf-8")
                else:
                    response.raise_for_status();path.write_bytes(response.content)
            pmids.update({r["pmcid"].removeprefix("PMC"):r["pmid"] for r in json.loads(path.read_bytes())["records"] if r.get("pmid")})
            time.sleep(.4)
        evidence=[]
        for pmcid in sorted(test):
            text=read("txt_files/PMC"+pmcid+".txt"); comparisons=[]
            for prompt_id,prompt in prompts.items():
                if prompt["PMCID"]!=pmcid:
                    continue
                quotes=[];labels=[]
                for row in annotations[prompt_id]:
                    try:
                        lo,hi=int(row["Evidence Start"]),int(row["Evidence End"])
                    except ValueError:
                        continue
                    if not 0<=lo<hi<=len(text):
                        raise ValueError("Evidence Inference source evidence offset mismatch")
                    q={"start":lo,"end":hi,"text":text[lo:hi]}
                    if q not in quotes:
                        quotes.append(q)
                    labels.append(row["Label Code"])
                comparisons.append({"prompt_id":prompt_id,"intervention":prompt["Intervention"],"comparator":prompt["Comparator"],
                                    "outcome":prompt["Outcome"],"evidence":quotes,"annotator_labels":labels})
            evidence.append({"pmid":pmids.get(pmcid,"PMC"+pmcid),"pmcid":"PMC"+pmcid,"text":text,
                             "sections":[{"start":0,"end":len(text),"label":"Fulltext"}],"entities":[],"relations":[],
                             "source_hash":digest(text),"comparisons":comparisons,"pmid_mapping_known":pmcid in pmids})
    corpora["evidenceinference"]=(evidence,[],"https://evidence-inference.ebm-nlp.com/download/")
    (sources/"evidenceinference-heldout-ids.txt").write_text("\n".join(d["pmid"] for d in evidence))
    training=json.loads(Path(clinical_manifest).read_bytes())
    training_ids=set().union(*(set(v) for v in training["training_pmids"].values()))
    prior=[]
    if benchmarks:
        for path in Path(benchmarks).rglob("inputs.json"):
            value=json.loads(path.read_bytes())
            prior.extend({"corpus":value.get("corpus"),"split":value.get("split"),"pmid":d["pmid"],"text_key":text_key(d["text"])} for d in value["documents"])
    specs=[]
    for name,(documents,tasks,source_url) in corpora.items():
        folder=output/name;folder.mkdir(parents=True,exist_ok=True)
        inputs=[]
        for doc in documents:
            row={k:doc[k] for k in ("pmid","text","sections","source_hash")}
            if name=="pubmedrct":
                row["rhetorical_sentences"]=[{**s,"label":"Abstract"} for s in doc["rhetorical_labels"]]
            inputs.append(row)
        for filename,value in (("inputs.json",{"corpus":name,"split":"test","documents":inputs}),
                               ("gold.json",{"corpus":name,"split":"test","documents":documents})):
            path=folder/filename
            raw=dumps(value)
            if path.exists() and path.read_text()!=raw:
                raise ValueError("Immutable holdout source changed")
            if not path.exists():
                path.write_text(raw,encoding="utf-8")
        ids={d["pmid"] for d in documents};texts={text_key(d["text"]) for d in documents}
        specs.append({"corpus":name,"official_split":"test","source_url":source_url,"papers":len(documents),
                      "inputs_path":str(folder/"inputs.json"),"gold_path":str(folder/"gold.json"),
                      "inputs_sha256":digest((folder/"inputs.json").read_bytes()),"gold_sha256":digest((folder/"gold.json").read_bytes()),
                      "chemical_lexicon_path":str(lexicon_path),"annotation_tasks":tasks,
                      "training_overlap_articles":len(ids&training_ids),"training_overlap_pmids":sorted(ids&training_ids),
                      "published_surface_differences":sum(e.get("source_surface_differs",False) for d in documents for e in d["entities"]),
                      "prior_evaluation_overlap":[{k:r[k] for k in ("corpus","split","pmid")} for r in prior if r["pmid"] in ids or r["text_key"] in texts],
                      "pmid_mapping_missing":sum(d.get("pmid_mapping_known") is False for d in documents),
                      "pretraining_overlap":"unknown"})
    audit={"schema":"v4-holdout-audit","completed":all(s["pmid_mapping_missing"]==0 for s in specs),"at":now(),"specs":specs,
           "training_manifest_sha256":digest(Path(clinical_manifest).read_bytes()),
           "source_adapter_sha256":digest(Path(__file__).read_bytes()),"pretraining_overlap":"unknown"}
    (output/"audit.json").write_text(dumps(audit),encoding="utf-8")
    return audit
