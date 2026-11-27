#!/usr/bin/env bash
# Protect the public recorder hostname with a device token plus temporary operator-IP access.
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="${STATE_FILE:-$SCRIPT_DIR/.aicam-24x7-run-state.env}"
SSH_PRIVATE_KEY="${SSH_PRIVATE_KEY:-$HOME/.ssh/id_ed25519}"
ADMIN="${ADMIN:-azureuser}"

for command in curl openssl ssh; do
  command -v "$command" >/dev/null || {
    echo "Missing required command: $command" >&2
    exit 1
  }
done
[[ -f "$STATE_FILE" ]] || {
  echo "Long-lived recorder state not found: $STATE_FILE" >&2
  exit 1
}
# shellcheck disable=SC1090
source "$STATE_FILE"
: "${PUBLIC_IP:?}"
[[ "${LONG_LIVED_MODE:-0}" == "1" ]] || {
  echo "Device auth is only supported after long-lived conversion." >&2
  exit 1
}

OPERATOR_IP="${OPERATOR_IP:-$(curl -4fsS https://ifconfig.me)}"
DEVICE_TOKEN="${AICAM_DEVICE_TOKEN:-${AICAM_DEVICE_TOKEN_STATE:-$(openssl rand -hex 32)}}"
ALLOW_OPERATOR_IP="${ALLOW_OPERATOR_IP:-1}"
[[ "$DEVICE_TOKEN" =~ ^[A-Fa-f0-9]{64}$ ]] || {
  echo "AICAM_DEVICE_TOKEN must be exactly 64 hexadecimal characters." >&2
  exit 2
}
[[ "$ALLOW_OPERATOR_IP" == "0" || "$ALLOW_OPERATOR_IP" == "1" ]] || {
  echo "ALLOW_OPERATOR_IP must be 0 or 1." >&2
  exit 2
}
DOMAIN="recorder-${PUBLIC_IP//./-}.sslip.io"
SSH_OPTIONS=(
  -i "$SSH_PRIVATE_KEY"
  -o BatchMode=yes
  -o ConnectTimeout=15
  -o StrictHostKeyChecking=accept-new
)

ssh "${SSH_OPTIONS[@]}" "$ADMIN@$PUBLIC_IP" \
  "OPERATOR_IP='$OPERATOR_IP' DEVICE_TOKEN='$DEVICE_TOKEN' DOMAIN='$DOMAIN' ALLOW_OPERATOR_IP='$ALLOW_OPERATOR_IP' bash -s" <<'REMOTE'
set -euo pipefail
sudo install -d -m 755 /etc/nginx/snippets
umask 077
auth_file="$(mktemp)"
cat >"$auth_file" <<EOF
set \$aicam_device_access 0;
if (\$http_x_aicam_token = $DEVICE_TOKEN) { set \$aicam_device_access 1; }
if (\$aicam_device_access = 0) { return 401; }
EOF
if [[ "$ALLOW_OPERATOR_IP" == "1" ]]; then
  sed -i "2i if (\\\$remote_addr = $OPERATOR_IP) { set \\\$aicam_device_access 1; }" "$auth_file"
fi
sudo install -m 600 "$auth_file" /etc/nginx/snippets/aicam-device-auth.conf
rm -f "$auth_file"

sudo python3 - <<'PY'
from pathlib import Path

path = Path("/etc/nginx/sites-available/aicam-trusted")
text = path.read_text(encoding="utf-8")
include = "        include /etc/nginx/snippets/aicam-device-auth.conf;\n"
if include not in text:
    marker = "    location / {\n"
    if marker not in text:
        raise SystemExit("Recorder nginx location was not found.")
    text = text.replace(marker, marker + include, 1)
    path.write_text(text, encoding="utf-8")
PY
sudo nginx -t
sudo systemctl reload nginx

without_token="$(curl -sS -o /dev/null -w '%{http_code}' "https://$DOMAIN/healthz")"
with_token="$(curl -sS -o /dev/null -w '%{http_code}' \
  -H "X-AiCam-Token: $DEVICE_TOKEN" "https://$DOMAIN/healthz")"
[[ "$without_token" == "401" && "$with_token" == "200" ]]
REMOTE

if grep -q '^AICAM_DEVICE_TOKEN_STATE=' "$STATE_FILE"; then
  python3 - "$STATE_FILE" "$DEVICE_TOKEN" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
token = sys.argv[2]
lines = path.read_text(encoding="utf-8").splitlines()
path.write_text(
    "\n".join(
        f"AICAM_DEVICE_TOKEN_STATE={token}"
        if line.startswith("AICAM_DEVICE_TOKEN_STATE=")
        else line
        for line in lines
    )
    + "\n",
    encoding="utf-8",
)
PY
else
  printf 'AICAM_DEVICE_TOKEN_STATE=%s\n' "$DEVICE_TOKEN" >>"$STATE_FILE"
fi
chmod 600 "$STATE_FILE"

if [[ "$ALLOW_OPERATOR_IP" == "1" ]]; then
  curl -fsS "https://$DOMAIN/healthz" >/dev/null
else
  curl -fsS -H "X-AiCam-Token: $DEVICE_TOKEN" \
    "https://$DOMAIN/healthz" >/dev/null
fi
echo "Public recorder device authentication is active."
if [[ "$ALLOW_OPERATOR_IP" == "1" ]]; then
  echo "The operator IP remains allowed until the sparse Android APK is installed."
else
  echo "Token-only mode is active."
fi
