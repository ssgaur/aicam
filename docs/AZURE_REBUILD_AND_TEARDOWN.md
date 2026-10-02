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

The supported rebuild path is now the idempotent deployment script:

```bash
bash deploy/azure_cpu_recorder.sh
```

It creates a dedicated `aicam-rg` boundary in Central India:

| Resource | Current baseline |
|---|---|
| VM | `aicam-recorder`, `Standard_D4s_v5`, Ubuntu 22.04 |
| Network | Explicit VNet, NIC, NSG, and static public IP |
| API | HTTPS on 8100 with a self-signed IP certificate |
| Access boundary | SSH and 8100 limited to the current operator IPv4 `/32` |
| Blob | Private `clips` and `frames` containers; no public blob access |
| PostgreSQL | Existing `aicam` DB via a dedicated `aicam_app_user` role |
| Retention | Blob cleaner removes rolling media older than 24 hours |

The deployment intentionally installs the CPU recorder requirements, not SAM2.
YOLO recording/upload/viewing works on D4s_v5; the experimental SAM2 deployment
remains a separate GPU concern. Secrets are generated at deployment time and
written only to the VM's mode-600 `backend/.env`.

The camera API still has no per-camera authentication. The `/32` NSG restriction
is therefore mandatory. Re-run the script whenever the operator's public IP
changes; do not broaden port 8100 to the internet.

Stop compute billing after a test:

```bash
az vm deallocate -g aicam-rg -n aicam-recorder
```

For a disposable test, remove the full deployment, its dedicated database role,
and only rows above the pre-test ID baseline captured in the ignored run-state
file:

```bash
bash deploy/destroy_cpu_recorder.sh --yes
```

The destroy script refuses any resource group except `aicam-rg`, aborts if an
unexpected resource is found there, waits for Azure deletion to finish, verifies
the dedicated PostgreSQL role is gone, and preserves the shared PostgreSQL
server, `aicam` database/schema, and every row at or below the frozen baseline.

Start a later clean run:

```bash
bash deploy/azure_cpu_recorder.sh
```

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
