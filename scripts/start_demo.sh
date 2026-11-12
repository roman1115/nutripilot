#!/bin/sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_root"

if [ ! -x "$project_root/.venv/bin/python" ]; then
    echo "Missing .venv. Run uv sync --python 3.12 --extra dev --frozen first." >&2
    exit 1
fi

exec "$project_root/.venv/bin/python" scripts/demo_launcher.py --foreground "$@"
