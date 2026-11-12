#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
stage=.solbreaker-update
if [[ -e "$stage/transaction" ]]; then
    printf '%s\n' 'Solbreaker update was interrupted. See runtime/launcher-update.log.' >&2
    exit 1
fi
if [[ -e .git && -x runtime/release/launch-gui.sh ]]; then
    exec runtime/release/launch-gui.sh --config "$PWD/runtime/manager.json" "$@"
fi
if [[ ! -x .venv/bin/python ]]; then
    python3 -m venv .venv
fi
if ! .venv/bin/python tools/install_dependencies.py --profile gui --check; then
    .venv/bin/python tools/install_dependencies.py --profile gui
fi
status=0
SOLBREAKER_RELEASE_LAUNCHER=1 .venv/bin/python -m solbreaker gui "$@" || status=$?
if [[ "$status" != 42 ]]; then
    exit "$status"
fi

mkdir -p runtime
if .venv/bin/python -B "$stage/apply.py" >runtime/launcher-update.log 2>&1; then
    export SOLBREAKER_UPDATE_RESULT=success
elif [[ -e "$stage/transaction" ]]; then
    cat runtime/launcher-update.log >&2
    exit 1
else
    export SOLBREAKER_UPDATE_RESULT=failed
fi
rm -rf -- "$stage"
exec bash ./launch-gui.sh "$@"
