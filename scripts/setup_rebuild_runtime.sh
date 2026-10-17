#!/usr/bin/env bash
# Recreate the tested Linux x86_64 R runtime without changing a global runtime.
set -euo pipefail
project="${1:?Usage: setup_rebuild_runtime.sh ABSOLUTE_PROJECT_ROOT}"
source_root="$(cd "$(dirname "$0")/.." && pwd)"
[[ "$project" == /* && "$project" != / ]] || { printf 'Use a dedicated absolute project directory\n' >&2; exit 2; }
[[ "$(uname -m)" == x86_64 ]] || { printf 'This explicit lock requires Linux x86_64\n' >&2; exit 2; }
toolchain="$project/toolchain"
mkdir -p "$toolchain/downloads" "$toolchain/bin" "$toolchain/r-library" "$toolchain/micromamba-root"
solver_archive="$toolchain/downloads/micromamba-2.3.2-linux-64.tar.bz2"
solver="$toolchain/bin/micromamba"
solver_sha=5512233cdd8564a671626081026dc861537a963baa06706baab08fac6f3bb9d2
if [[ ! -f "$solver_archive" ]]; then
  curl -fL --retry 4 https://api.anaconda.org/download/conda-forge/micromamba/2.3.2/linux-64/micromamba-2.3.2-0.tar.bz2 -o "$solver_archive"
fi
printf '%s  %s\n' "$solver_sha" "$solver_archive" | sha256sum -c -
if [[ ! -x "$solver" ]]; then
  tar -xOf "$solver_archive" bin/micromamba > "$solver"
  chmod 700 "$solver"
fi
export MAMBA_ROOT_PREFIX="$toolchain/micromamba-root"
prefix="$toolchain/r-nutrigenomics"
lock="$source_root/config/rebuild-runtime-linux-64-20261017.lock.txt"
if [[ -e "$prefix" ]]; then
  printf 'Existing runtime preserved. Use a new project root to rebuild it.\n' >&2
  exit 2
fi
"$solver" --no-rc create --no-pin -y -p "$prefix" -f "$lock"
cp "$lock" "$toolchain/r-environment-linux-64.lock.txt"
overlay="$toolchain/downloads/preprocessCore_1.72.0.tar.gz"
curl -fL --retry 4 https://bioconductor.org/packages/3.22/bioc/src/contrib/preprocessCore_1.72.0.tar.gz -o "$overlay"
printf '%s  %s\n' 4472052371147cfbf91d223eb15f3b030d236de9b4bba6b58a205c368d53884d "$overlay" | sha256sum -c -
sha256sum "$overlay" > "$overlay.sha256"
R_LIBS_USER="$toolchain/r-library" "$solver" run -p "$prefix" R CMD INSTALL --library="$toolchain/r-library" --configure-args=--disable-threading "$overlay"
printf 'package=preprocessCore\nversion=1.72.0\nconfigure_args=--disable-threading\nlibrary=%s\n' "$toolchain/r-library" > "$toolchain/r-overlay-lock.txt"
R_LIBS_USER="$toolchain/r-library" R_THREADS=1 "$prefix/bin/Rscript" --vanilla "$source_root/scripts/check_preprocess_core.R"
printf 'Runtime ready: %s\n' "$prefix"
