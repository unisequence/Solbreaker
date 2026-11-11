#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ ! -x .venv/bin/python ]]; then
    python3 -m venv .venv
fi
if ! .venv/bin/python tools/install_dependencies.py --profile tui --check; then
    .venv/bin/python tools/install_dependencies.py --profile tui
fi
exec .venv/bin/python -m solbreaker tui "$@"
