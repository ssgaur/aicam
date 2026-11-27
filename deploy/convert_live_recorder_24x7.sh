#!/usr/bin/env bash
# Convert the current recorder VM to a sparse, 31-day, low-cost deployment.
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
STATE_FILE="${STATE_FILE:-$SCRIPT_DIR/.aicam-run-state.env}"
SSH_PRIVATE_KEY="${SSH_PRIVATE_KEY:-$HOME/.ssh/id_ed25519}"
TARGET_SIZE="${TARGET_SIZE:-Standard_B2s}"
RETENTION_DAYS="${RETENTION_DAYS:-31}"
MIN_CAPTURE_INTERVAL_SECONDS="${MIN_CAPTURE_INTERVAL_SECONDS:-600}"
ADMIN="${ADMIN:-azureuser}"

for command in az curl scp ssh; do
  command -v "$command" >/dev/null || {
    echo "Missing required command: $command" >&2
    exit 1
  }
done
[[ -f "$STATE_FILE" ]] || {
  echo "Recorder state not found: $STATE_FILE" >&2
  exit 1
}
[[ -f "$SSH_PRIVATE_KEY" ]] || {
  echo "SSH private key not found: $SSH_PRIVATE_KEY" >&2
  exit 1
}
[[ "$RETENTION_DAYS" =~ ^[1-9][0-9]*$ ]] && ((RETENTION_DAYS <= 365)) || {
  echo "RETENTION_DAYS must be between 1 and 365." >&2
  exit 2
}
[[ "$MIN_CAPTURE_INTERVAL_SECONDS" =~ ^[0-9]+$ ]] &&
  ((MIN_CAPTURE_INTERVAL_SECONDS <= 86400)) || {
  echo "MIN_CAPTURE_INTERVAL_SECONDS must be between 0 and 86400." >&2
  exit 2
}

# shellcheck disable=SC1090
source "$STATE_FILE"
: "${RG:?}"
: "${VM:?}"
: "${PUBLIC_IP:?}"
[[ "$RG" == "aicam-rg" && "$VM" == "aicam-recorder" ]] || {
  echo "Refusing unexpected recorder boundary: $RG/$VM" >&2
  exit 1
}

SSH_OPTIONS=(
  -i "$SSH_PRIVATE_KEY"
  -o BatchMode=yes
  -o ConnectTimeout=15
  -o StrictHostKeyChecking=accept-new
)

echo ">>> Installing sparse-ingest and retention policy before resizing"
scp "${SSH_OPTIONS[@]}" \
  "$REPO_ROOT/backend/main.py" \
  "$REPO_ROOT/backend/runtime_policy.py" \
  "$ADMIN@$PUBLIC_IP:/tmp/" >/dev/null
ssh "${SSH_OPTIONS[@]}" "$ADMIN@$PUBLIC_IP" \
  "RETENTION_DAYS='$RETENTION_DAYS' MIN_INTERVAL='$MIN_CAPTURE_INTERVAL_SECONDS' bash -s" <<'REMOTE'
set -euo pipefail
install -m 644 /tmp/main.py "$HOME/aicam/backend/main.py"
install -m 644 /tmp/runtime_policy.py "$HOME/aicam/backend/runtime_policy.py"
rm -f /tmp/main.py /tmp/runtime_policy.py
sed -i \
  -e '/^AICAM_BLOB_RETENTION_DAYS=/d' \
  -e '/^AICAM_MIN_CAPTURE_INTERVAL_SECONDS=/d' \
  -e '/^AICAM_NATIVE_ONLY=/d' \
  "$HOME/aicam/backend/.env"
{
  printf 'AICAM_BLOB_RETENTION_DAYS=%s\n' "$RETENTION_DAYS"
  printf 'AICAM_MIN_CAPTURE_INTERVAL_SECONDS=%s\n' "$MIN_INTERVAL"
  printf 'AICAM_NATIVE_ONLY=1\n'
} >>"$HOME/aicam/backend/.env"
sudo systemctl restart aicam.service
for _ in $(seq 1 60); do
  if curl -kfsS https://127.0.0.1:8100/healthz >/dev/null; then
    exit 0
  fi
  sleep 2
done
sudo journalctl -u aicam.service -n 100 --no-pager >&2
exit 1
REMOTE

CURRENT_SIZE="$(
  az vm show -g "$RG" -n "$VM" --query hardwareProfile.vmSize -o tsv
)"
if [[ "$CURRENT_SIZE" != "$TARGET_SIZE" ]]; then
  echo ">>> Resizing $VM from $CURRENT_SIZE to $TARGET_SIZE"
  az vm deallocate -g "$RG" -n "$VM" -o none
  if ! az vm resize -g "$RG" -n "$VM" --size "$TARGET_SIZE" -o none; then
    echo "Target size unavailable; restoring $CURRENT_SIZE." >&2
    az vm resize -g "$RG" -n "$VM" --size "$CURRENT_SIZE" -o none || true
    az vm start -g "$RG" -n "$VM" -o none || true
    exit 1
  fi
  az vm start -g "$RG" -n "$VM" -o none
fi

echo ">>> Waiting for the complete stack after VM restart"
for _ in $(seq 1 90); do
  if curl -fsS --max-time 10 \
      "https://recorder-${PUBLIC_IP//./-}.sslip.io/healthz" >/dev/null &&
     curl -fsS --max-time 10 \
      "https://neighbourly-${PUBLIC_IP//./-}.sslip.io/healthz" >/dev/null; then
    break
  fi
  sleep 5
done
HEALTH="$(
  curl -fsS "https://recorder-${PUBLIC_IP//./-}.sslip.io/healthz"
)"
python3 - "$HEALTH" "$RETENTION_DAYS" "$MIN_CAPTURE_INTERVAL_SECONDS" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
policy = payload["policy"]
assert payload["ok"] is True
assert policy["blob_retention_days"] == int(sys.argv[2])
assert policy["minimum_capture_interval_seconds"] == int(sys.argv[3])
assert policy["native_only"] is True
PY
curl -fsS "https://neighbourly-${PUBLIC_IP//./-}.sslip.io/healthz" >/dev/null

SERVICES="$(
  ssh "${SSH_OPTIONS[@]}" "$ADMIN@$PUBLIC_IP" \
    'systemctl is-active aicam.service neighbourly-community.service telegram-camera-miniapp.service telegram-camera-bot.service'
)"
[[ "$SERVICES" == $'active\nactive\nactive\nactive' ]] || {
  echo "One or more services did not recover: ${SERVICES//$'\n'/,}" >&2
  exit 1
}

cat >>"$STATE_FILE" <<EOF
LONG_LIVED_MODE=1
VM_SIZE=$TARGET_SIZE
AICAM_BLOB_RETENTION_DAYS=$RETENTION_DAYS
AICAM_MIN_CAPTURE_INTERVAL_SECONDS=$MIN_CAPTURE_INTERVAL_SECONDS
LONG_LIVED_CONFIGURED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF
chmod 600 "$STATE_FILE"

echo "24x7 recorder conversion passed."
echo "  VM size:            $TARGET_SIZE"
echo "  Blob retention:     $RETENTION_DAYS days"
echo "  Accepted frequency: one clip per $MIN_CAPTURE_INTERVAL_SECONDS seconds/device"
echo "  Static public IP:   $PUBLIC_IP"
