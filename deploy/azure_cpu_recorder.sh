#!/usr/bin/env bash
# Provision a bounded Central India CPU deployment for one AiCameraX recorder.
#
# The camera API has no application-layer authentication yet, so inbound SSH and
# HTTPS are restricted to OPERATOR_IP/32. Active deployments cannot be rerun;
# destroy and deploy cleanly after an operator IP change.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
STATE_FILE="${STATE_FILE:-$SCRIPT_DIR/.aicam-run-state.env}"
DEPLOY_STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
DEPLOY_START_EPOCH="$(date +%s)"

LOCATION="${LOCATION:-centralindia}"
RG="${RG:-aicam-rg}"
VM="${VM:-aicam-recorder}"
SIZE="${SIZE:-Standard_D4s_v5}"
ADMIN="${ADMIN:-azureuser}"
SSH_PUBLIC_KEY="${SSH_PUBLIC_KEY:-$HOME/.ssh/id_ed25519.pub}"
SSH_PRIVATE_KEY="${SSH_PRIVATE_KEY:-${SSH_PUBLIC_KEY%.pub}}"
PORT="${PORT:-8100}"

VNET="${VNET:-aicam-vnet}"
SUBNET="${SUBNET:-aicam-subnet}"
NSG="${NSG:-aicam-recorder-nsg}"
NIC="${NIC:-aicam-recorder-nic}"
PIP="${PIP:-aicam-recorder-ip}"
OS_DISK="${OS_DISK:-aicam-recorder-osdisk}"
STORAGE="${STORAGE:-aicam87347ae77de2}"

PG_RG="${PG_RG:-assamese-learn-rg}"
PG_SERVER="${PG_SERVER:-assamese-learn-db}"
PG_HOST="${PG_HOST:-assamese-learn-db.postgres.database.azure.com}"
PG_DATABASE="${PG_DATABASE:-aicam}"
PG_ROLE="${PG_ROLE:-aicam_app_user}"
PG_ADMIN="${PG_ADMIN:-}"

for command in az curl openssl psql scp ssh tar; do
  command -v "$command" >/dev/null || {
    echo "Missing required command: $command" >&2
    exit 1
  }
done
[[ -f "$SSH_PUBLIC_KEY" ]] || {
  echo "SSH public key not found: $SSH_PUBLIC_KEY" >&2
  exit 1
}
[[ -f "$SSH_PRIVATE_KEY" ]] || {
  echo "SSH private key not found: $SSH_PRIVATE_KEY" >&2
  exit 1
}
if [[ -z "$PG_ADMIN" ]]; then
  PG_ADMIN="$(
    az postgres flexible-server microsoft-entra-admin list \
      -g "$PG_RG" -s "$PG_SERVER" \
      --query '[0].principalName' -o tsv
  )"
fi
[[ -n "$PG_ADMIN" ]] || {
  echo "Microsoft Entra PostgreSQL administrator was not found." >&2
  exit 1
}
[[ ! -e "$STATE_FILE" ]] || {
  echo "Active or incomplete deployment state already exists: $STATE_FILE" >&2
  echo "Run deploy/destroy_cpu_recorder.sh --yes before creating another deployment." >&2
  exit 1
}
if [[ "$(az group exists -n "$RG")" == "true" ]]; then
  echo "Dedicated resource group already exists without run state: $RG" >&2
  echo "Inspect and clean the incomplete deployment before retrying." >&2
  exit 1
fi

OPERATOR_IP="${OPERATOR_IP:-$(curl -4fsS https://ifconfig.me)}"
OPERATOR_CIDR="${OPERATOR_IP%/32}/32"
PG_PASSWORD="$(openssl rand -hex 24)"
ENV_FILE="$(mktemp)"
cleanup() {
  rm -f "$ENV_FILE"
}
trap cleanup EXIT

echo ">>> Resource group and private Blob storage"
az group create -g "$RG" -l "$LOCATION" -o none
if ! az storage account show -g "$RG" -n "$STORAGE" -o none 2>/dev/null; then
  az storage account create \
    -g "$RG" -n "$STORAGE" -l "$LOCATION" \
    --sku Standard_LRS \
    --kind StorageV2 \
    --min-tls-version TLS1_2 \
    --allow-blob-public-access false \
    -o none
fi
STORAGE_KEY="$(az storage account keys list -g "$RG" -n "$STORAGE" --query '[0].value' -o tsv)"
for container in clips frames; do
  az storage container create \
    --account-name "$STORAGE" \
    --account-key "$STORAGE_KEY" \
    --name "$container" \
    --public-access off \
    -o none
done

echo ">>> VM $VM ($SIZE) and IP-restricted network rules"
if ! az vm show -g "$RG" -n "$VM" -o none 2>/dev/null; then
  az vm create \
    -g "$RG" -n "$VM" \
    --location "$LOCATION" \
    --image Ubuntu2204 \
    --size "$SIZE" \
    --admin-username "$ADMIN" \
    --ssh-key-values "$SSH_PUBLIC_KEY" \
    --vnet-name "$VNET" \
    --subnet "$SUBNET" \
    --nsg "$NSG" \
    --nsg-rule NONE \
    --nic-delete-option Delete \
    --public-ip-address "$PIP" \
    --public-ip-address-allocation static \
    --public-ip-sku Standard \
    --os-disk-name "$OS_DISK" \
    --os-disk-size-gb 64 \
    --storage-sku StandardSSD_LRS \
    -o none
else
  az vm start -g "$RG" -n "$VM" -o none
fi

az network nsg rule create \
  -g "$RG" --nsg-name "$NSG" -n AllowOperatorSSH \
  --priority 1000 \
  --source-address-prefixes "$OPERATOR_CIDR" \
  --destination-port-ranges 22 \
  --access Allow --protocol Tcp --direction Inbound \
  -o none
az network nsg rule create \
  -g "$RG" --nsg-name "$NSG" -n AllowOperatorAiCam \
  --priority 1010 \
  --source-address-prefixes "$OPERATOR_CIDR" \
  --destination-port-ranges "$PORT" \
  --access Allow --protocol Tcp --direction Inbound \
  -o none

PUBLIC_IP="$(az network public-ip show -g "$RG" -n "$PIP" --query ipAddress -o tsv)"

echo ">>> Dedicated PostgreSQL role in the existing aicam database"
PG_VM_RULE="aicam-recorder-vm"
az postgres flexible-server firewall-rule create \
  -g "$PG_RG" -n "$PG_SERVER" -r "$PG_VM_RULE" \
  --start-ip-address "$PUBLIC_IP" \
  --end-ip-address "$PUBLIC_IP" \
  -o none
PG_RULE="aicam-provision-current"
az postgres flexible-server firewall-rule create \
  -g "$PG_RG" -n "$PG_SERVER" -r "$PG_RULE" \
  --start-ip-address "$OPERATOR_IP" \
  --end-ip-address "$OPERATOR_IP" \
  -o none
remove_pg_rule() {
  az postgres flexible-server firewall-rule delete \
    -g "$PG_RG" -n "$PG_SERVER" -r "$PG_RULE" --yes -o none \
    >/dev/null 2>&1 || true
}
trap 'remove_pg_rule; cleanup' EXIT

PG_TOKEN="$(az account get-access-token --resource-type oss-rdbms --query accessToken -o tsv)"
BASELINE_ASSIGNMENTS="$(
  PGPASSWORD="$PG_TOKEN" psql \
    -h "$PG_HOST" -p 5432 -d "$PG_DATABASE" -U "$PG_ADMIN" \
    -At -v ON_ERROR_STOP=1 <<'SQL'
SELECT 'BASELINE_CLIP_COUNT=' || COUNT(*) FROM native_clips;
SELECT 'BASELINE_CLIP_ID=' || COALESCE(MAX(id), 0) FROM native_clips;
SELECT 'BASELINE_FRAME_COUNT=' || COUNT(*) FROM native_sampled_frames;
SELECT 'BASELINE_FRAME_ID=' || COALESCE(MAX(id), 0) FROM native_sampled_frames;
SELECT 'BASELINE_DETECTION_COUNT=' || COUNT(*) FROM native_detections;
SELECT 'BASELINE_DETECTION_ID=' || COALESCE(MAX(id), 0) FROM native_detections;
SELECT 'BASELINE_TRACK_COUNT=' || COUNT(*) FROM native_object_tracks;
SELECT 'BASELINE_TRACK_ID=' || COALESCE(MAX(id), 0) FROM native_object_tracks;
SELECT 'BASELINE_REPORT_COUNT=' || COUNT(*) FROM native_clip_reports;
SELECT 'BASELINE_REPORT_ID=' || COALESCE(MAX(clip_id), 0) FROM native_clip_reports;
SQL
)"

# Persist cleanup data before any database ownership or service mutation. A failed
# standalone deployment can now run the normal guarded destroy path.
cat >"$STATE_FILE" <<EOF
RG=$RG
VM=$VM
PUBLIC_IP=$PUBLIC_IP
STORAGE=$STORAGE
PG_RG=$PG_RG
PG_SERVER=$PG_SERVER
PG_HOST=$PG_HOST
PG_DATABASE=$PG_DATABASE
PG_ROLE=$PG_ROLE
PG_ADMIN=$PG_ADMIN
PG_VM_RULE=$PG_VM_RULE
$BASELINE_ASSIGNMENTS
DEPLOY_STATUS=provisioning
DEPLOY_STARTED_AT=$DEPLOY_STARTED_AT
EOF
chmod 600 "$STATE_FILE"

PGPASSWORD="$PG_TOKEN" psql \
  -h "$PG_HOST" -p 5432 -d "$PG_DATABASE" -U "$PG_ADMIN" \
  -v ON_ERROR_STOP=1 \
  -v app_role="$PG_ROLE" \
  -v app_password="$PG_PASSWORD" \
  -v app_database="$PG_DATABASE" \
  >/dev/null <<'SQL'
SELECT 'CREATE ROLE ' || quote_ident(:'app_role') || ' LOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_role')
\gexec
ALTER ROLE :"app_role" PASSWORD :'app_password';
GRANT CONNECT ON DATABASE :"app_database" TO :"app_role";
GRANT USAGE, CREATE ON SCHEMA public TO :"app_role";
SELECT format('ALTER TABLE public.%I OWNER TO %I', tablename, :'app_role')
FROM pg_tables
WHERE schemaname = 'public' AND tablename LIKE 'native_%'
\gexec
SELECT format('ALTER SEQUENCE public.%I OWNER TO %I', sequencename, :'app_role')
FROM pg_sequences
WHERE schemaname = 'public' AND sequencename LIKE 'native_%'
\gexec
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO :"app_role";
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO :"app_role";
SQL
unset PG_TOKEN
remove_pg_rule
trap cleanup EXIT

# The old VM IP was released; its database firewall rule must not remain trusted.
az postgres flexible-server firewall-rule delete \
  -g "$PG_RG" -n "$PG_SERVER" -r aicam-server --yes -o none \
  >/dev/null 2>&1 || true

cat >"$ENV_FILE" <<EOF
AICAM_CLOUD=1
AZURE_STORAGE_ACCOUNT=$STORAGE
AZURE_STORAGE_KEY=$STORAGE_KEY
AZURE_STORAGE_CLIPS_CONTAINER=clips
AZURE_STORAGE_FRAMES_CONTAINER=frames
AICAM_PG_DSN=postgresql://$PG_ROLE:$PG_PASSWORD@$PG_HOST:5432/$PG_DATABASE?sslmode=require
PYTHONUNBUFFERED=1
EOF
chmod 600 "$ENV_FILE"
unset STORAGE_KEY PG_PASSWORD

SSH_OPTIONS=(
  -i "$SSH_PRIVATE_KEY"
  -o BatchMode=yes
  -o ConnectTimeout=10
  -o StrictHostKeyChecking=accept-new
)

echo ">>> Waiting for SSH on $PUBLIC_IP"
for _ in $(seq 1 36); do
  if ssh "${SSH_OPTIONS[@]}" "$ADMIN@$PUBLIC_IP" true 2>/dev/null; then
    break
  fi
  sleep 5
done
ssh "${SSH_OPTIONS[@]}" "$ADMIN@$PUBLIC_IP" true

echo ">>> Installing recorder runtime"
scp "${SSH_OPTIONS[@]}" "$REPO_ROOT/requirements-recorder.txt" \
  "$ADMIN@$PUBLIC_IP:/tmp/requirements-recorder.txt" >/dev/null
ssh "${SSH_OPTIONS[@]}" "$ADMIN@$PUBLIC_IP" \
  "PUBLIC_IP='$PUBLIC_IP' PORT='$PORT' bash -s" <<'REMOTE'
set -euo pipefail
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  ca-certificates ffmpeg git openssl python3-venv

if [[ ! -d "$HOME/aicam/.git" ]]; then
  git clone --depth 1 https://github.com/ssgaur/aicam.git "$HOME/aicam"
else
  git -C "$HOME/aicam" pull --ff-only
fi

python3 -m venv "$HOME/aicam/venv"
"$HOME/aicam/venv/bin/pip" install --quiet --upgrade pip wheel
"$HOME/aicam/venv/bin/pip" install --quiet \
  torch torchvision --index-url https://download.pytorch.org/whl/cpu
"$HOME/aicam/venv/bin/pip" install --quiet -r /tmp/requirements-recorder.txt

mkdir -p "$HOME/aicam/certs" "$HOME/aicam/data/native_camera"
if [[ ! -f "$HOME/aicam/certs/aicam.key" ]]; then
  openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
    -keyout "$HOME/aicam/certs/aicam.key" \
    -out "$HOME/aicam/certs/aicam.crt" \
    -subj "/CN=$PUBLIC_IP" \
    -addext "subjectAltName=IP:$PUBLIC_IP" \
    >/dev/null 2>&1
  chmod 600 "$HOME/aicam/certs/aicam.key"
fi

sudo tee /etc/systemd/system/aicam.service >/dev/null <<UNIT
[Unit]
Description=AiCam single-camera recorder backend
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$HOME/aicam/backend
EnvironmentFile=$HOME/aicam/backend/.env
ExecStart=$HOME/aicam/venv/bin/uvicorn main:app --host 0.0.0.0 --port $PORT --ssl-keyfile $HOME/aicam/certs/aicam.key --ssl-certfile $HOME/aicam/certs/aicam.crt
Restart=always
RestartSec=5
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
UNIT
REMOTE

echo ">>> Uploading current safe runtime source and private environment"
tar -C "$REPO_ROOT" -czf - \
  backend/main.py \
  native_camera_pipeline.py \
  postgres_store.py \
  viewer.html \
  checkpoints/yolov8n.pt \
  | ssh "${SSH_OPTIONS[@]}" "$ADMIN@$PUBLIC_IP" \
      'tar -xzf - -C "$HOME/aicam"'
scp "${SSH_OPTIONS[@]}" "$ENV_FILE" "$ADMIN@$PUBLIC_IP:/tmp/aicam.env" >/dev/null

ssh "${SSH_OPTIONS[@]}" "$ADMIN@$PUBLIC_IP" "PORT='$PORT' bash -s" <<'REMOTE'
set -euo pipefail
install -m 600 /tmp/aicam.env "$HOME/aicam/backend/.env"
rm -f /tmp/aicam.env /tmp/requirements-recorder.txt
sudo systemctl daemon-reload
sudo systemctl enable --now aicam.service >/dev/null

for _ in $(seq 1 60); do
  if curl -kfsS "https://127.0.0.1:$PORT/healthz" >/tmp/aicam-health.json; then
    cat /tmp/aicam-health.json
    echo
    exit 0
  fi
  sleep 3
done
sudo journalctl -u aicam.service -n 80 --no-pager >&2
exit 1
REMOTE

echo ">>> Verifying the IP-restricted HTTPS endpoint"
curl -kfsS "https://$PUBLIC_IP:$PORT/healthz"
echo

DEPLOY_FINISHED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
DEPLOY_ELAPSED_SECONDS="$(( $(date +%s) - DEPLOY_START_EPOCH ))"
cat >"$STATE_FILE" <<EOF
RG=$RG
VM=$VM
PUBLIC_IP=$PUBLIC_IP
STORAGE=$STORAGE
PG_RG=$PG_RG
PG_SERVER=$PG_SERVER
PG_HOST=$PG_HOST
PG_DATABASE=$PG_DATABASE
PG_ROLE=$PG_ROLE
PG_ADMIN=$PG_ADMIN
PG_VM_RULE=$PG_VM_RULE
$BASELINE_ASSIGNMENTS
DEPLOY_STATUS=ready
DEPLOY_STARTED_AT=$DEPLOY_STARTED_AT
DEPLOY_FINISHED_AT=$DEPLOY_FINISHED_AT
DEPLOY_ELAPSED_SECONDS=$DEPLOY_ELAPSED_SECONDS
EOF
chmod 600 "$STATE_FILE"

echo
echo "AiCam recorder is ready:"
echo "  Backend:   https://$PUBLIC_IP:$PORT"
echo "  Viewer:    https://$PUBLIC_IP:$PORT/viewer"
echo "  Storage:   $STORAGE (private clips + frames)"
echo "  Database:  $PG_SERVER/$PG_DATABASE via dedicated role $PG_ROLE"
echo "  Allowed:   $OPERATOR_CIDR only"
echo "  Elapsed:   ${DEPLOY_ELAPSED_SECONDS}s"
echo "  Run state: $STATE_FILE"
echo
echo "After testing:"
echo "  bash deploy/destroy_cpu_recorder.sh --yes"
