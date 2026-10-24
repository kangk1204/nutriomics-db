"""Export source-only licensed text; exact tokenizer remains explicitly optional."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutri_literature.corpus_v8 import export_corpus


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--exclusion-manifest", required=True, type=Path)
    parser.add_argument("--max-tokens", type=int, default=50_000_000)
    parser.add_argument("--tokenizer")
    parser.add_argument("--tokenizer-revision")
    parser.add_argument("--archived-language", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()
    result = export_corpus(args.database, args.output, args.exclusion_manifest, args.max_tokens,
                           args.tokenizer, args.tokenizer_revision, archived_language=args.archived_language)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
