#!/bin/zsh
set -euo pipefail

exec python3 "${0:A:h}/../scripts/update_macos.py" "$@"
