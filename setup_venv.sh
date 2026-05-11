#!/usr/bin/env bash
set -euo pipefail

MIN_PYTHON_MAJOR=3
MIN_PYTHON_MINOR=10

if ! command -v python3 >/dev/null 2>&1; then
    echo "Error: python3 is not installed or not in PATH."
    exit 1
fi

python3 - <<PY
import sys
min_version = (${MIN_PYTHON_MAJOR}, ${MIN_PYTHON_MINOR})
if sys.version_info < min_version:
    raise SystemExit(
        f"Error: Python {min_version[0]}.{min_version[1]}+ is required. "
        f"Found {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}."
    )
PY

if [ -d "venv" ]; then
    echo "Virtual environment 'venv' already exists. Reusing it..."
else
    echo "Creating virtual environment 'venv'..."
    python3 -m venv venv
fi

source venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo "Virtual environment set up and packages installed."
