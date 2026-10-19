#!/usr/bin/env bash
# Forced command for fullish-deploy; accepts exactly one deploy operation.
set -euo pipefail
original=${SSH_ORIGINAL_COMMAND:-}
if [[ ! $original =~ ^deploy\ ([a-z0-9][a-z0-9.:/_-]*@sha256:[a-f0-9]{64})$ ]]; then
    printf 'Only deploy repository@sha256:digest is permitted.\n' >&2
    exit 64
fi
exec /usr/bin/sudo -n /opt/fullish/bin/ssh-entrypoint.sh "${BASH_REMATCH[1]}"
