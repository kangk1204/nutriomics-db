"""Local synthetic UI QA only: no research data, accounts, model work or remote calls."""
from pathlib import Path
import runpy
import tempfile

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from nutrigenomics.literature_api import attach_literature
from nutrigenomics.literature_snapshot import build_snapshot

ROOT = Path(__file__).resolve().parents[2]
temporary = tempfile.TemporaryDirectory(prefix='nutriomics-v8-ui-qa-')
home = Path(temporary.name)
fixture = runpy.run_path(str(ROOT/'tests/test_literature_platform.py'))['source']
source = fixture.__wrapped__(home)
build_snapshot(source,home/'snapshot',release_id='QA-SYNTHETIC-NOT-RESEARCH-DATA',
               expansion_manifest={'corpus_id':'v8-qa','query_version':'q-qa','expected_papers':133253,
                                   'pmids':['1','2','999'],'status':'running'},
               diagnostics_manifest={'status':'running','stages':{'qa_candidates':{'total':20,'retained':15,'lost':5}},
                                     'id_changes':{'qa_gene':{'kept':5,'corrected':0,'damaged':0}},
                                     'label_provenance':{'qa_training':{'public_gold':10,'weak_labels':20}}})
app = FastAPI(title='Synthetic v8 UI QA')
attach_literature(app,database_path=home/'snapshot')
app.mount('/explorer',StaticFiles(directory=ROOT/'frontend/explorer/dist',html=True),name='qa-ui')
