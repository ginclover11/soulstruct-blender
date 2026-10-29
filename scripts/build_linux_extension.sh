#!/usr/bin/env bash
# Build the Soulstruct Blender extension for Linux (x86-64), compiling `pyrelink` from the Firelink sources.
#
# Usage:  scripts/build_linux_extension.sh /path/to/Firelink [--no-build-extension]
#
# Environment:
#   PYTHON              Python 3.13 interpreter to use (default: python3.13)
#   BLENDER_EXECUTABLE  Blender 5.1+ binary used for `extension build` (default: `blender` from PATH)
#   BUILD_VENV          Directory of the throw-away build virtualenv (default: ./.build-venv)
#
# NOTE: this script has not been run end to end yet; see README "Linux".
set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: $0 /path/to/Firelink [--no-build-extension]" >&2
    exit 2
fi
FIRELINK_DIR="$(cd "$1" && pwd)"
shift
EXTRA_ARGS=("$@")

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3.13}"
BUILD_VENV="${BUILD_VENV:-$REPO_DIR/.build-venv}"

[[ -f "$FIRELINK_DIR/pyproject.toml" ]] || { echo "Not a Firelink checkout: $FIRELINK_DIR" >&2; exit 1; }
command -v "$PYTHON" >/dev/null || { echo "Python 3.13 not found (set PYTHON=...)." >&2; exit 1; }
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 13) else 1)' \
    || { echo "$PYTHON is not Python 3.13 (needed to match Blender 5.1+)." >&2; exit 1; }
for tool in cmake ninja git; do
    command -v "$tool" >/dev/null || { echo "Missing required tool: $tool" >&2; exit 1; }
done

if [[ ! -d "$BUILD_VENV" ]]; then
    "$PYTHON" -m venv "$BUILD_VENV"
fi
# shellcheck disable=SC1091
source "$BUILD_VENV/bin/activate"
python -m pip install --upgrade pip wheel
# Build backend for pyrelink (installed here so `--no-build-isolation` rebuilds are fast), plus what
# `prepare_extension.py` imports (soulstruct + havok pinned as in requirements.txt).
python -m pip install "scikit-build-core>=0.10" pybind11
# `colorama` is imported by soulstruct-havok but not declared as one of its dependencies.
python -m pip install colorama
python -m pip install $(grep -E '^(soulstruct|soulstruct-havok)==' "$REPO_DIR/io_soulstruct/requirements.txt")

cd "$REPO_DIR"
python prepare_extension.py \
    --update-wheels \
    --pypi-soulstruct \
    --platform linux-x64 \
    --no-build-isolation \
    --firelink-source-dir "$FIRELINK_DIR" \
    ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}

echo
echo "Done. If the extension was built, the .zip is in: $REPO_DIR/Releases"
echo "(blender_manifest.toml now lists linux-x64 wheels; do not commit that change to the Windows manifest.)"
