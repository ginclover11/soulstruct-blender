#!/usr/bin/env bash
# Build the Soulstruct Blender extension for Linux (x86-64), compiling `pyrelink` from the Firelink sources.
#
# Usage:  scripts/build_linux_extension.sh /path/to/Firelink [--no-build-extension]
#
# Environment:
#   BLENDER_PYTHON      Python version of YOUR Blender (default: 3.13, what blender.org builds bundle). Blender packages
#                       that use the system Python (Arch/CachyOS) may need e.g. 3.14. Check with:
#                         blender --background --python-expr "import sys; print(sys.version)"
#   PYTHON              Python interpreter to use (default: python$BLENDER_PYTHON; must be that version)
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
BLENDER_PYTHON="${BLENDER_PYTHON:-3.13}"
PYTHON="${PYTHON:-python$BLENDER_PYTHON}"
if [[ "$BLENDER_PYTHON" == "3.13" ]]; then
    BUILD_VENV="${BUILD_VENV:-$REPO_DIR/.build-venv}"
else
    BUILD_VENV="${BUILD_VENV:-$REPO_DIR/.build-venv-$BLENDER_PYTHON}"
fi

[[ -f "$FIRELINK_DIR/pyproject.toml" ]] || { echo "Not a Firelink checkout: $FIRELINK_DIR" >&2; exit 1; }
command -v "$PYTHON" >/dev/null || { echo "Python $BLENDER_PYTHON not found (set PYTHON=...)." >&2; exit 1; }
"$PYTHON" -c "import sys; sys.exit(0 if '%d.%d' % sys.version_info[:2] == '$BLENDER_PYTHON' else 1)" \
    || { echo "$PYTHON is not Python $BLENDER_PYTHON (it must match your Blender's Python)." >&2; exit 1; }
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
    --blender-python "$BLENDER_PYTHON" \
    --no-build-isolation \
    --firelink-source-dir "$FIRELINK_DIR" \
    ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}

echo
echo "Done. If the extension was built, the .zip is in: $REPO_DIR/Releases"
echo "(blender_manifest.toml now lists linux-x64 wheels; do not commit that change to the Windows manifest.)"
