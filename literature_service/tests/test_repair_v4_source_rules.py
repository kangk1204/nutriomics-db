from nutri_literature.repair_v4_source_rules import nominal_inhibition_witness

def example(text,gene='GENE'):
    c=text.index('Drug');g=text.index(gene)
    return ({'decision':'supported','negated':False,'observation':'background','condition':'48 hours',
             'evidence':[{'start':0,'end':len(text),'text':text}]},
            {'start':c,'end':c+4},{'start':g,'end':g+len(gene)})

def test_inhibitor_role_has_independent_type_witness_and_no_transferred_condition():
    e,c,g=example('Drug, a selective GENE inhibitor, was administered.')
    e['native_type']='Association'
    result=nominal_inhibition_witness(e,c,g)
    assert result['native_type']=='Negative_Correlation'
    assert result['condition'] is None and result['parent_condition_not_transferred']
    assert result['measurement']=='target_inhibition_role' and result['change']=='unspecified'
    assert result['evidence']==e['evidence']
    for vote in (None,'Bind','Association','Positive_Correlation'):
        e['native_type']=vote
        assert nominal_inhibition_witness(e,c,g)['native_type']=='Negative_Correlation'

def test_gene_first_and_of_target_variants():
    for text in ('The direct GENE inhibitor, Drug, was administered.',
                 'Drug is a potent inhibitor of GENE.',
                 'Drug acts as a specific GENE antagonist.'):
        assert nominal_inhibition_witness(*example(text))['native_type']=='Negative_Correlation'

def test_no_question_negation_contrast_or_plan_promotion():
    for text in ('We will test whether Drug is a GENE inhibitor.',
                 'Unlike a GENE inhibitor, Drug had no effect.',
                 'Drug is not a GENE inhibitor.',
                 'Drug is a GENE antagonist and a GENE agonist.',
                 'Is Drug a GENE inhibitor?'):
        assert nominal_inhibition_witness(*example(text)) is None
    e,c,g=example('Drug is a GENE inhibitor.')
    e['observation']='planned'
    assert nominal_inhibition_witness(e,c,g) is None
    e['observation']='unknown';e['decision']='uncertain'
    assert nominal_inhibition_witness(e,c,g) is None

def test_inhibitor_inside_gene_name_is_not_target_role():
    e,c,g=example('Drug is a plasminogen activator inhibitor-1.','plasminogen activator')
    assert nominal_inhibition_witness(e,c,g) is None
