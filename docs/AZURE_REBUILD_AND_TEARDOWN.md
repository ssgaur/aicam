# AiCam Azure Rebuild and Teardown

This runbook records the deleted **personal AiCam** cloud deployment so a future
agent can recreate it from the GitHub repository without depending on an old VM.

> **Scope boundary:** the original AiCam resources lived in the shared resource
> group `blf-archiver-rg`, alongside the separate Telegram archiver. Never delete
> that resource group to tear down AiCam. Delete only resources whose names start
> with `aicam-`, plus the `aicamstorage2026` storage account.

## Deleted deployment (August 2026)

| Resource | Value |
|---|---|
| Resource group | `blf-archiver-rg` (shared; retained) |
| VM | `aicam-server` |
| Region | Central India |
| Compute | `Standard_D4s_v5` (4 vCPU, 16 GB RAM) |
| OS disk | 64 GB managed disk |
| Public IP | `20.197.31.88` (released on teardown; do not expect to retain it) |
| Storage account | `aicamstorage2026` |
| Blob containers | `clips`, `frames` |
| Service | `aicam.service`, serving FastAPI on TCP 8100 |

The storage account held rolling video/frame data only. Local SQLite metadata
was on the VM disk; deletion permanently removes it. The GitHub repo contains
the application code and setup documentation, not camera footage or secrets.

## Safe teardown

Use the explicit resource names below. This preserves:

- `blf-archiver` VM and its OS disk
- `blf-archiverVNET`, `blf-archiverNSG`, NIC, and public IP
- the shared Azure Postgres server (`assamese-learn-db`)

```bash
RG=blf-archiver-rg

# 1. Delete the VM first. This releases the VM compute allocation.
az vm delete -g "$RG" -n aicam-server --yes

# 2. Delete its independently managed network resources.
az network nic delete -g "$RG" -n aicam-serverVMNic
az network public-ip delete -g "$RG" -n aicam-serverPublicIP
az network nsg delete -g "$RG" -n aicam-serverNSG

# 3. Delete its managed OS disk.
az disk delete -g "$RG" -n aicam-server_OsDisk_1_666628845f2140c8b1aa9ec8730ebb6d --yes

# 4. Delete rolling clips / frames and the storage account itself.
az storage account delete -g "$RG" -n aicamstorage2026 --yes

# 5. Confirm no AiCam resources remain.
az resource list -g "$RG" \
  --query "[?contains(name, 'aicam') || contains(name, 'AiCam')].[name,type]" -o table
```

## Rebuild baseline (CPU / single-camera)

This recreates the old personal deployment. It is **not** the scalable
enterprise architecture described in [ENTERPRISE_PLATFORM_RFC.md](ENTERPRISE_PLATFORM_RFC.md).

```bash
RG=aicam-rg
LOCATION=centralindia
VM=aicam-server
ADMIN=azureuser

az group create -g "$RG" -l "$LOCATION"
az vm create \
  -g "$RG" -n "$VM" \
  --image Ubuntu2204 \
  --size Standard_D4s_v5 \
  --admin-username "$ADMIN" \
  --ssh-key-values "$HOME/.ssh/id_rsa.pub" \
  --os-disk-size-gb 64 \
  --public-ip-sku Standard

# Keep SSH restricted to the operator's current public IPv4.
MY_IP=$(curl -4 -s ifconfig.me)
az vm open-port -g "$RG" -n "$VM" --port 22 --priority 1000 --source-address-prefixes "$MY_IP"

# Temporary only: TCP 8100 was previously public. For a real rebuild, create a
# domain, terminate real TLS with Caddy/Nginx, and require authentication.
az vm open-port -g "$RG" -n "$VM" --port 8100 --priority 1010

az storage account create \
  -g "$RG" -n <globally-unique-storage-account-name> \
  -l "$LOCATION" --sku Standard_LRS --kind StorageV2
az storage container create --account-name <storage-account-name> --name clips --auth-mode login
az storage container create --account-name <storage-account-name> --name frames --auth-mode login
```

Then clone `https://github.com/ssgaur/aicam`, create its Python virtual
environment, install `requirements.txt`, configure secrets only in the VM's
gitignored `.env`, copy the service unit, and start `aicam.service`. Consult
`README.md`, `SETUP_FROM_SCRATCH.md`, and `.github/copilot-instructions.md`
for the current application details.

## Important rebuild improvements

Do these before reconnecting cameras:

1. Register a domain; terminate publicly trusted TLS with Caddy/Nginx.
2. Close raw public TCP 8100; put the API behind the reverse proxy.
3. Add camera and user auth. The old server had self-signed TLS and no API
   authentication, which is not an acceptable production boundary.
4. Implement P0/P1 in `ENTERPRISE_PLATFORM_RFC.md` before targeting multiple
   cameras: per-camera identity, direct-to-blob ingest, queue, and worker pool.

## Related deployment script

`deploy/azure_spot_t4.sh` provisions a separate experimental **Spot T4 / SAM2**
machine in Spain Central. It is not a drop-in replacement for the old Central
India D4s_v5 deployment and should be reviewed before use.
