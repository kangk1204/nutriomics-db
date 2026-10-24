"""Source-only100-paper input validation; no gold or model requests."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from nutri_literature.chatgpt_repair_io import HOME, prepare_data
    from nutri_literature.chatgpt_repair_knowledge import Knowledge, prepare
    from nutri_literature.chatgpt_repair_runner import payloads
    from nutri_literature.chatgpt_grounding import Catalog
    from nutri_literature.chatgpt_evaluation import _read
    from nutri_literature.chatgpt_extraction import exact_span
    from nutri_literature.contract_v7 import immutable, sha
    _, sources = prepare_data()
    prepare()
    catalog = Catalog.load(ROOT / 'runtime/chatgpt-inputs/official-id-dictionary.json.gz')
    knowledge = Knowledge(catalog)
    payloads(sources, 'development', catalog, knowledge)
    sizes, aligned = [], 0
    for d in sources:
        path = HOME / 'payloads/development' / ('paper-'+d['pmid']+'.json')
        p = _read(path)
        if p['source_sha256'] != sha(d['text']) or p['source'] != d['text']:
            raise ValueError('Untouched input source differs')
        for m in p['mentions']:
            if not exact_span(d['text'], m):
                raise ValueError('Automatic mention offset mismatch')
            aligned += 1
        sizes.append(len(path.read_text(encoding='utf-8')))
    result = {'papers': 100, 'aligned_automatic_mentions': aligned, 'source_offset_mismatches': 0,
              'prior_labels_and_priorities_in_input': False, 'gold_labels_read': False, 'model_requests': 0,
              'payload_characters': {'maximum': max(sizes), 'mean': sum(sizes)/len(sizes)},
              'source_sha256': sha(Path(__file__).read_bytes())}
    immutable(HOME / 'development-input-dry-run.json', result)
    print(result)


if __name__ == '__main__':
    main()
