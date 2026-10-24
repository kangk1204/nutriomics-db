#!/usr/bin/env bash
set -eu
cd /home/keunsoo/projects/nutriomics-literature-v3-development
export PYTHONPATH="$PWD"
export LITERATURE_HOME=/home/keunsoo/projects/nutriomics-literature/runtime
export LITERATURE_NLP_LOCK="$LITERATURE_HOME/locks/nlp-cpu.lock"
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
exec 9>"$LITERATURE_HOME/experiments/v3/experiment.lock"
flock -n 9 || exit 1
python=/home/keunsoo/projects/nutriomics-literature/.venv-v2/bin/python
lexicon="$LITERATURE_HOME/experiments/v3/chemical-source/mesh-chemicals-08a1a8c546eef06e11a657669da5e10fc155e6426ba6d21c6d35c4e2e3813e6b.json"
"$python" scripts/run_development_v3.py --corpus bioredirect --mode end_to_end --lexicon "$lexicon"
"$python" scripts/summarize_development_v3.py
"$python" scripts/run_development_v3.py --corpus bioredirect --mode gold_entities --lexicon "$lexicon"
"$python" scripts/summarize_development_v3.py
"$python" scripts/run_development_v3.py --corpus bioscope --limit 200 --lexicon "$lexicon"
"$python" scripts/summarize_development_v3.py
"$python" scripts/run_development_v3.py --corpus ebmnlp --limit 200 --lexicon "$lexicon"
"$python" scripts/summarize_development_v3.py
"$python" scripts/run_development_v3.py --corpus evidenceoutcomes --limit 200 --lexicon "$lexicon"
"$python" scripts/summarize_development_v3.py
