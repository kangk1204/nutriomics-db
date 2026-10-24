"""Exact native-ID projection used by the frozen v6 refinement scorer."""


def native_mapping(entities):
    mapping={}
    for e in entities:
        identifier,native=e.get('normalized_id'),e.get('payload',{}).get('native_id')
        kind='gene' if e['kind']=='gene' else 'component'
        if native and identifier:
            mapping[(kind,identifier)]={str(native)}
        elif identifier and identifier.startswith(('MESH:','NCBIGene:')):
            mapping[(kind,identifier)]={identifier.split(':',1)[1]}
    return mapping
