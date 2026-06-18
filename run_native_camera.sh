#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -d venv ]]; then
  echo "Missing venv/. Create/install the Python environment first." >&2
  exit 1
fi

source venv/bin/activate
python native_camera_pipeline.py wizard
