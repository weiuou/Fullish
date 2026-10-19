#!/usr/bin/env bash
# Run after server-setup.sh: sudo bash scripts/server-ssh-setup.sh KEY.pub
set -euo pipefail
umask 077
[[ $EUID -eq 0 && $# -eq 1 ]] || { echo 'Usage: sudo bash server-ssh-setup.sh KEY.pub' >&2; exit 64; }
[[ -f /opt/fullish/image-repository ]] || { echo 'Run server-setup.sh first.' >&2; exit 1; }
key=$(tr -d '\r' < "$1")
[[ $key =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+(\ [A-Za-z0-9._@-]+)?$ ]] || { echo 'Use a dedicated Ed25519 public key.' >&2; exit 64; }
ssh-keygen -lf "$1" >/dev/null
source_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
account=fullish-deploy
account_home=/var/lib/fullish-deploy
if id "$account" >/dev/null 2>&1; then
    [[ $(getent passwd "$account" | cut -d: -f6) == "$account_home" ]] || { echo 'Existing account has a different home.' >&2; exit 1; }
else
    useradd --system --user-group --home-dir "$account_home" --shell /bin/bash "$account"
fi
# Root owns the home and key file, so the deploy user cannot widen its access.
install -d -o root -g root -m 755 "$account_home" "$account_home/.ssh"
install -o root -g root -m 755 "$source_root/scripts/ssh-dispatch.sh" /usr/local/bin/fullish-deploy-ssh
install -o root -g root -m 755 "$source_root/scripts/ssh-entrypoint.sh" /opt/fullish/bin/ssh-entrypoint.sh
sudoers=$(mktemp /etc/sudoers.d/fullish-deploy.XXXXXX)
trap 'rm -f -- "$sudoers"' EXIT
printf '%s ALL=(root) NOPASSWD: /opt/fullish/bin/ssh-entrypoint.sh\n' "$account" > "$sudoers"
chmod 440 "$sudoers"
visudo --check --file "$sudoers"
install -m 440 "$sudoers" /etc/sudoers.d/fullish-deploy
printf 'restrict,command="/usr/local/bin/fullish-deploy-ssh" %s\n' "$key" > "$account_home/.ssh/authorized_keys"
chmod 644 "$account_home/.ssh/authorized_keys"
printf 'Installed restricted SSH deployment account: %s\n' "$account"
