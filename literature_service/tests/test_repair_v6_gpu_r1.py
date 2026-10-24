import copy
from pathlib import Path
import numpy as np
import pytest
from nutri_literature.repair_v6_gpu_r1 import common,identity,id_features,id_profiles


class FakeKB:
    chemical_categories=set()
    def by_id(self,kind,identifier):
        return {'kind':kind,'native_id':identifier,'names':['GENE' if identifier=='0' else 'other protein'],
                'taxon':9606 if kind=='gene' else None,'description':'gene expression'}
    def close(self):pass


def mention(text='GENE'):
    return {'kind':'gene','start':2,'end':2+len(text),'text':text,'key':'m',
            'candidate_ids':[str(i) for i in range(70)],'previous_id':None,'nerscore':.9,
            'ner_score':.9,'source':'independent-ner','supported_taxa':[9606],
            'lexical_ids':['0'],'dense_ids':['0'],'exact_ids':['0']}


def test_single_menu_has_fifty_IDs_and_NIL_without_gold_injection():
    m=mention();kb=FakeKB();ids=[r[0] for r in id_features.menu(m,kb)]
    assert len(ids)==51 and ids[0] is None and '60' not in ids
    assert ids==[r[0] for r in id_features.menu(copy.deepcopy(m),kb)]


def test_original_ID_competes_in_same_fifty_slots():
    m={**mention(),'previous_id':'69'};ids=[r[0] for r in id_features.menu(m,FakeKB())]
    assert len(ids)==51 and '69' in ids and '49' not in ids


def test_gold_ID_changes_targets_but_never_source_features():
    m=mention();builder=id_features.Builder(FakeKB())
    a={'pmid':'one','text':'β GENE changed.','entities':[{'native_ids':['GOLD-OUTSIDE']} ]}
    b={**a,'pmid':'two','entities':[{'native_ids':['OTHER-GOLD']} ]}
    ids,x=builder.encode(a,m);ids2,y=builder.encode(b,m)
    assert x==y and ids==ids2 and len(x[0])==len(id_features.FEATURES)
    assert not any(v in id_features.FEATURES for v in ('pmid','native_id','gold_ID'))


def test_source_position_mismatch_raises():
    with pytest.raises(ValueError,match='위치'):
        id_features.Builder(FakeKB()).encode({'text':'β GENE changed.'},{**mention(),'start':3})


def test_native_ID_numbers_are_not_numeric_features():
    class Renamed(FakeKB):
        def by_id(self,kind,identifier):return super().by_id(kind,'0' if identifier=='long-ID' else identifier)
    m={**mention(),'candidate_ids':['0'],'lexical_ids':['0'],'dense_ids':['0'],'exact_ids':['0']}
    _,x=id_features.Builder(FakeKB()).encode({'text':'β GENE changed.'},m)
    n={**m,'candidate_ids':['long-ID'],'lexical_ids':['long-ID'],'dense_ids':['long-ID'],'exact_ids':['long-ID']}
    _,y=id_features.Builder(Renamed()).encode({'text':'β GENE changed.'},n)
    assert x==y


def test_listwise_gradient_matches_numerical_gradient_with_multiple_correct_IDs():
    rng=np.random.default_rng(3);x=rng.normal(size=(5,4));w=rng.normal(size=4)
    y=np.array([0.,.5,.5,1.,0.]);offset=np.array([0,3,5]);weights=np.array([1.,.25])
    _,gradient=identity.listwise_objective(w,x,y,offset,weights,1.)
    for j in range(4):
        delta=np.zeros(4);delta[j]=1e-6
        plus=identity.listwise_objective(w+delta,x,y,offset,weights,1.)[0]
        minus=identity.listwise_objective(w-delta,x,y,offset,weights,1.)[0]
        assert gradient[j]==pytest.approx((plus-minus)/2e-6,abs=1e-6)


def test_real_optimizer_learns_ID_positive_and_automatic_false_mention_NIL():
    from scipy.optimize import minimize
    x=[];y=[]
    for i in range(100):
        positive=i<50
        x.extend([[1.,float(not positive)],[0.,0.]])
        y.extend([float(not positive),float(positive)])
    x=np.asarray(x);y=np.asarray(y);offsets=np.arange(0,201,2);weights=np.ones(100)
    before=identity.listwise_objective(np.zeros(2),x,y,offsets,weights,10.)[0]
    fit=minimize(identity.listwise_objective,np.zeros(2),args=(x,y,offsets,weights,10.),jac=True,method='L-BFGS-B')
    assert fit.success and fit.fun<before*.25
    assert np.exp(x[:2]@fit.x-np.max(x[:2]@fit.x))[1]/np.exp(x[:2]@fit.x-np.max(x[:2]@fit.x)).sum()>.9
    assert np.exp(x[-2:]@fit.x-np.max(x[-2:]@fit.x))[0]/np.exp(x[-2:]@fit.x-np.max(x[-2:]@fit.x)).sum()>.9


def row(kind='gene',correct=True):
    return {'kind':kind,'annotation_scope_complete':True,'species_evidence_compatible':True,
            'proposed_id':'new','previous_id':None,'priority':.999,'margin':5.,'strict_correct':correct}


def test_gene_and_chemical_policies_are_calibrated_separately():
    p=identity.choose_policy([row() for _ in range(50)]+[row('chemical',False) for _ in range(100)])
    assert p['gene']['new']['enabled'] and not p['chemical']['new']['enabled']


def test_small_sample_or_point_precision90_cannot_bypass_wilson_gate():
    assert not identity.choose_policy([row() for _ in range(49)])['gene']['new']['enabled']
    assert not identity.choose_policy([row(correct=i<45) for i in range(50)])['gene']['new']['enabled']


def test_NIL_preserves_old_ID_without_claiming_correction():
    policy=identity.choose_policy([]);r={**row(),'previous_id':'old','proposed_id':None}
    assert identity.select(r,policy)==('old','retained')


def test_unknown_species_cannot_be_automatically_normalized_to_human():
    p=identity.choose_policy([row() for _ in range(50)])
    assert identity.select({**row(),'species_evidence_compatible':False},p)==(None,'NIL')


def test_partial_annotation_never_creates_negatives_for_other_kind():
    d={'entities':[],'relation_annotation_complete':False,'gene_annotation_complete':True}
    assert identity.annotation_scope(d,'gene') and not identity.annotation_scope(d,'chemical')
    assert identity.annotation_scope({'pmid':'source','text':'input'},'gene') is None


@pytest.mark.parametrize('held',[{'pmid':'1','text':'different'}, {'pmid':'2','text':'ＳＡＭＥ'}, {'pmid':'2','text':'different','pmcid':'PMC1'}])
def test_NER_cannot_predict_paper_ID_text_or_PMCID_seen_in_training(held):
    # PMID, normalized text and PMCID are independently protected.
    with pytest.raises(ValueError):id_profiles._disjoint([{'pmid':'1','text':'same','pmcid':'PMC1'}],[held])


def test_revision_cannot_create_a_new_GPU_budget(tmp_path):
    with pytest.raises(FileNotFoundError):common.RuntimeBudget(tmp_path)
    common.atomic(tmp_path/'gpu-budget.json',{'limit_seconds':72*3600,'used_seconds':72*3600,'sessions':[]})
    with pytest.raises(TimeoutError):common.RuntimeBudget(tmp_path).start('cannot-reset')
    assert common.read(tmp_path/'gpu-budget.json')['used_seconds']==72*3600


def test_shared_budget_atomic_update_does_not_replace_its_symlink(tmp_path):
    original=tmp_path/'original.json';common.atomic(original,{'limit_seconds':72*3600,'used_seconds':17.,'sessions':[]})
    revision=tmp_path/'revision';revision.mkdir()
    try:(revision/'gpu-budget.json').symlink_to(original)
    except OSError:pytest.skip('Symbolic links unavailable on this test host')
    budget=common.RuntimeBudget(revision);budget.start('shared');budget.stop()
    assert (revision/'gpu-budget.json').is_symlink()
    assert common.read(original)['used_seconds']>=17.
    assert len(common.read(original)['sessions'])==1


def test_out_of_menu_gold_is_NIL_supervision_without_injection(monkeypatch,tmp_path):
    monkeypatch.setattr(identity,'HOME',tmp_path);monkeypatch.setattr(identity,'Knowledge',FakeKB)
    common.atomic(tmp_path/'knowledge/manifest.json',{'pinned':True})
    m=mention();d={'pmid':'1','text':'β GENE changed.','gene_annotation_complete':True,
                 'relation_annotation_complete':False,'entities':[{'kind':'gene','start':2,'end':6,'native_ids':['OUTSIDE']}]}
    p=tmp_path/'prepared.json';common.atomic(p,{'mentions':[m]})
    profile={'files':[{'pmid':'1','path':'prepared.json','sha256':common.filehash(p)}]}
    path=identity.table([d],profile);a=np.load(path/'arrays.npz');meta=common.read(path/'meta.json')
    assert a['y'][0]==1 and a['y'][1:].sum()==0 and 'OUTSIDE' not in meta[0]['ids']
    assert common.read(path/'manifest.json')['stats']['gold_ID_outside_menu']==1
