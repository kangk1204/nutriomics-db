"""Use the covering public comparison index without changing its view values."""
from .public_omics_index import _projection

def indexed_contrast_source(db):
    """Only for queries fixing series, analysis version and contrast together.

    The broader feature index can otherwise force native row hydration for
    every contrast merely to count one gene. Retain the exact view projection.
    Unknown layouts keep the existing query path.
    """
    row=db.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='public_effect_page' AND tbl_name='effect'").fetchone()
    if not row:return 'study_effect'
    columns=[r[2] for r in db.execute('PRAGMA index_info(public_effect_page)')]
    if columns!=['series_accession','analysis_version','contrast','feature_type','feature_id']:return 'study_effect'
    try:projection,_=_projection(db)
    except ValueError:return 'study_effect'
    return '(SELECT '+projection+' FROM effect INDEXED BY public_effect_page)'
