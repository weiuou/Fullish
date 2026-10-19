#!/usr/bin/env bash
# Save this as a TAT SHELL command, with custom parameters enabled.
# Parameter: image. User: root. Timeout: 900 seconds.
# TAT substitutes text before Bash parses it: invoking this command is a
# privileged deployment capability, NOT a sandbox for untrusted parameters.
set -euo pipefail
exec /opt/fullish/bin/deploy.sh '{{image}}'
