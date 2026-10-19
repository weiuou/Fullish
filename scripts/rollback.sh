#!/usr/bin/env bash
set -euo pipefail
# Read the previous release only after acquiring the same lock as normal deploys.
root=${FULLISH_ROOT:-/opt/fullish}
exec bash "$root/bin/deploy.sh" --rollback
