#!/usr/bin/env bash
# Build a clean sdist (source only, no wheels) and upload it to PyPI.
# Usage: packaging/publish_pypi.sh [--test]   (--test uploads to TestPyPI)
# Auth: TWINE_USERNAME=__token__ and TWINE_PASSWORD=<pypi token>, or ~/.pypirc.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT=$(pwd)

REPO_ARGS=()
if [[ "${1:-}" == "--test" ]]; then
    REPO_ARGS=(--repository testpypi)
fi

VERSION=$(python3 -c "
import re, pathlib
src = pathlib.Path('src/cqta/__init__.py').read_text()
print(re.search(r'__version__\s*=\s*[\"\']([^\"\']+)[\"\']', src).group(1))
")
echo "Publishing cqta ${VERSION} (sdist only)"

# Clean build in an isolated temp dir; never touch build/ (used by app packaging).
OUT=$(mktemp -d)
trap 'rm -rf "$OUT"' EXIT

python3 -m pip install --quiet --upgrade build twine
# Run from $OUT: the repo's build/ dir would shadow the "build" module.
(cd "$OUT" && python3 -m build --sdist --outdir "$OUT" "$ROOT")

FILES=("$OUT"/*)
if [[ ${#FILES[@]} -ne 1 || "${FILES[0]}" != "$OUT/cqta-${VERSION}.tar.gz" ]]; then
    echo "Unexpected build output: ${FILES[*]}" >&2
    exit 1
fi

python3 -m twine check "${FILES[0]}"
python3 -m twine upload ${REPO_ARGS[@]+"${REPO_ARGS[@]}"} "${FILES[0]}"
