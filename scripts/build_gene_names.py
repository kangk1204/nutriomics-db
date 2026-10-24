"""Build a source-bound gene name catalog without changing the source DB."""
import argparse
import json
from nutrigenomics.gene_names import build


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['hgnc', 'inventory', 'korean', 'contracts', 'output']:
        parser.add_argument('--' + name, required=True)
    a = parser.parse_args()
    result = build(a.hgnc, a.inventory, a.korean, a.contracts, a.output)
    print(json.dumps({k: v for k, v in result.items() if k not in ['excluded_bindings', 'namespace_contracts']}))


if __name__ == '__main__':
    main()
