#!/usr/bin/env bash
# Deploy AiCam SAM 2 backend to an Azure NC8as_T4_v3 Spot VM.
#
# Prereqs: az CLI logged in, NCASv3_T4 quota >= 8 vCPUs in $LOCATION.
# Cost target: ~$73/mo 24x7 (Spot, eviction=Deallocate).
#
# Usage:  bash deploy/azure_spot_t4.sh
#
set -euo pipefail

LOCATION="${LOCATION:-spaincentral}"
RG="${RG:-aicam-rg}"
VM="${VM:-aicam-t4}"
SIZE="${SIZE:-Standard_NC8as_T4_v3}"
ADMIN="${ADMIN:-azureuser}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_rsa.pub}"
PORT=8100

# Ubuntu 22.04 with NVIDIA drivers + CUDA pre-installed (Azure HPC image)
IMAGE="microsoft-dsvm:ubuntu-hpc:2204:latest"

echo ">>> Creating RG $RG in $LOCATION"
az group create -n "$RG" -l "$LOCATION" -o none

echo ">>> Creating VM $VM ($SIZE, Spot)"
az vm create \
  -g "$RG" -n "$VM" \
  --image "$IMAGE" \
  --size "$SIZE" \
  --admin-username "$ADMIN" \
  --ssh-key-values "$SSH_KEY" \
  --priority Spot \
  --eviction-policy Deallocate \
  --max-price -1 \
  --public-ip-sku Standard \
  --os-disk-size-gb 64 \
  --output table

echo ">>> Opening port $PORT"
az vm open-port -g "$RG" -n "$VM" --port "$PORT" --priority 1010 -o none

PUBLIC_IP=$(az vm show -d -g "$RG" -n "$VM" --query publicIps -o tsv)
echo ">>> Public IP: $PUBLIC_IP"

echo ">>> Provisioning SAM 2 backend over SSH (~5 min)"
ssh -o StrictHostKeyChecking=no "$ADMIN@$PUBLIC_IP" 'bash -s' <<'REMOTE'
set -euo pipefail
sudo apt-get update -qq
sudo apt-get install -y -qq python3.10-venv git build-essential
cd ~
[ -d aicam ] || git clone https://github.com/ssgaur/aicam.git
cd aicam
python3 -m venv venv
source venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet torch torchvision
pip install --quiet fastapi 'uvicorn[standard]' websockets pillow numpy opencv-python-headless python-multipart
pip install --quiet "git+https://github.com/facebookresearch/sam2.git"
mkdir -p checkpoints
[ -f checkpoints/sam2.1_hiera_tiny.pt ] || \
  curl -sL -o checkpoints/sam2.1_hiera_tiny.pt \
    https://dl.fbaipublicfiles.com/segment_anything_2/092824/sam2.1_hiera_tiny.pt

sudo tee /etc/systemd/system/aicam.service >/dev/null <<UNIT
[Unit]
Description=AiCam SAM 2 backend
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$HOME/aicam/backend
Environment=PYTHONUNBUFFERED=1
ExecStart=$HOME/aicam/venv/bin/uvicorn main:app --host 0.0.0.0 --port 8100
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now aicam
sleep 5
curl -s http://127.0.0.1:8100/healthz && echo
REMOTE

echo
echo "=== DONE ==="
echo "Health:  curl http://$PUBLIC_IP:$PORT/healthz"
echo "App:     set Backend = http://$PUBLIC_IP:$PORT in AiCam"
echo "Stop:    az vm deallocate -g $RG -n $VM    # stops billing"
echo "Start:   az vm start      -g $RG -n $VM"
echo "Destroy: az group delete  -n $RG --yes --no-wait"
