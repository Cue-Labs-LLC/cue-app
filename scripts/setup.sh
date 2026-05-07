#!/usr/bin/env bash
# Bootstrap a Cue dev environment: venv, deps, migrations, sanity check.
# Idempotent — safe to re-run after pulling new dependencies or migrations.
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON_BIN="${PYTHON_BIN:-python3.11}"
VENV_DIR="${VENV_DIR:-.venv}"

if [ ! -d "$VENV_DIR" ]; then
    echo "==> Creating virtualenv at $VENV_DIR ($PYTHON_BIN)"
    "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

echo "==> Upgrading pip"
pip install -U pip

echo "==> Installing requirements"
pip install -r requirements.txt

echo "==> Running migrations"
python manage.py migrate

echo "==> Running Django system check"
python manage.py check

echo
echo "Setup complete. Activate the venv with:"
echo "  source $VENV_DIR/bin/activate"
