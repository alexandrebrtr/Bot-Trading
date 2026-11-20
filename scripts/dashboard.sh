#!/usr/bin/env bash
# Lance le dashboard en une commande (macOS ou Linux) :
#   ./scripts/dashboard.sh           # données réelles collectées (data/runtime, data/store)
#   ./scripts/dashboard.sh --demo    # données synthétiques de démonstration
# Crée .venv au premier lancement, (ré)installe les dépendances si pyproject.toml change,
# puis ouvre http://127.0.0.1:8501 (accessible depuis ce Mac uniquement).
set -euo pipefail
cd "$(dirname "$0")/.."

PY=""
for cand in python3.13 python3.12 python3.11 python3; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PY="$cand"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.11 ou plus est requis. Sur Mac : brew install python@3.12" >&2
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "Création de l'environnement .venv avec $PY…"
  "$PY" -m venv .venv
fi

STAMP=.venv/.dashboard-deps
HASH=$(.venv/bin/python -c 'import hashlib; print(hashlib.sha256(open("pyproject.toml", "rb").read()).hexdigest())')
if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP")" != "$HASH" ]; then
  echo "Installation des dépendances (une seule fois)…"
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -e ".[dashboard]"
  echo "$HASH" > "$STAMP"
fi

exec .venv/bin/python -m bot_trading dashboard "$@"
