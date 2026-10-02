#!/usr/bin/env bash
# Delete the bounded recorder deployment while preserving shared infrastructure.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="${STATE_FILE:-$SCRIPT_DIR/.aicam-run-state.env}"
START_EPOCH="$(date +%s)"

if [[ "${1:-}" != "--yes" ]]; then
  echo "Refusing destructive cleanup without --yes." >&2
  echo "Usage: $0 --yes" >&2
  exit 2
fi
[[ -f "$STATE_FILE" ]] || {
  echo "Run state not found: $STATE_FILE" >&2
  exit 1
}
# shellcheck disable=SC1090
source "$STATE_FILE"

: "${RG:?}"
: "${VM:?}"
: "${PG_RG:?}"
: "${PG_SERVER:?}"
: "${PG_HOST:?}"
: "${PG_DATABASE:?}"
: "${PG_ROLE:?}"
: "${PG_ADMIN:?}"
: "${BASELINE_CLIP_ID:?}"
: "${BASELINE_FRAME_ID:?}"
: "${BASELINE_DETECTION_ID:?}"
: "${BASELINE_TRACK_ID:?}"
: "${BASELINE_REPORT_ID:?}"

if [[ "$RG" != "aicam-rg" ]]; then
  echo "Refusing to delete unexpected resource group: $RG" >&2
  exit 1
fi

for command in az curl psql; do
  command -v "$command" >/dev/null || {
    echo "Missing required command: $command" >&2
    exit 1
  }
done

echo ">>> Verifying the dedicated resource-group boundary"
if az group exists -n "$RG" | grep -qx true; then
  while IFS=$'\t' read -r name type; do
    case "$name" in
      aicam-recorder*|aicam-vnet|aicam87347ae77de2) ;;
      *)
        echo "Unexpected resource in $RG; refusing group deletion: $name ($type)" >&2
        exit 1
        ;;
    esac
  done < <(
    az resource list -g "$RG" \
      --query '[].[name,type]' -o tsv
  )
fi

OPERATOR_IP="${OPERATOR_IP:-$(curl -4fsS https://ifconfig.me)}"
PG_RULE="aicam-destroy-current"
remove_pg_rule() {
  az postgres flexible-server firewall-rule delete \
    -g "$PG_RG" -n "$PG_SERVER" -r "$PG_RULE" --yes -o none \
    >/dev/null 2>&1 || true
}
trap remove_pg_rule EXIT

az postgres flexible-server firewall-rule create \
  -g "$PG_RG" -n "$PG_SERVER" -r "$PG_RULE" \
  --start-ip-address "$OPERATOR_IP" \
  --end-ip-address "$OPERATOR_IP" \
  -o none

# The optional Neighbourly integration test uses this VM as a bounded read API.
az postgres flexible-server firewall-rule delete \
  -g "$PG_RG" -n "$PG_SERVER" -r neighbourly-community-aicam --yes -o none \
  >/dev/null 2>&1 || true

echo ">>> Removing only rows created after the frozen pre-test baseline"
PG_TOKEN="$(az account get-access-token --resource-type oss-rdbms --query accessToken -o tsv)"
PGPASSWORD="$PG_TOKEN" psql \
  -h "$PG_HOST" -p 5432 -d "$PG_DATABASE" -U "$PG_ADMIN" \
  -v ON_ERROR_STOP=1 \
  -v baseline_clip="$BASELINE_CLIP_ID" \
  -v baseline_frame="$BASELINE_FRAME_ID" \
  -v baseline_detection="$BASELINE_DETECTION_ID" \
  -v baseline_track="$BASELINE_TRACK_ID" \
  -v baseline_report="$BASELINE_REPORT_ID" \
  -v app_role="$PG_ROLE" <<'SQL'
BEGIN;
DELETE FROM native_clip_reports WHERE clip_id > :'baseline_report'::bigint;
DELETE FROM native_detections WHERE id > :'baseline_detection'::bigint;
DELETE FROM native_sampled_frames WHERE id > :'baseline_frame'::bigint;
DELETE FROM native_object_tracks WHERE id > :'baseline_track'::bigint;
DELETE FROM native_clips WHERE id > :'baseline_clip'::bigint;
COMMIT;

SELECT 'remaining_native_clips=' || COUNT(*) FROM native_clips;
SELECT 'remaining_max_clip_id=' || COALESCE(MAX(id), 0) FROM native_clips;

SELECT format('REASSIGN OWNED BY %I TO CURRENT_USER', :'app_role')
WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_role')
\gexec
SELECT format('DROP OWNED BY %I', :'app_role')
WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_role')
\gexec
SELECT format('DROP ROLE %I', :'app_role')
WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_role')
\gexec
SQL
unset PG_TOKEN

ROLE_COUNT="$(
  PG_TOKEN="$(az account get-access-token --resource-type oss-rdbms --query accessToken -o tsv)"
  PGPASSWORD="$PG_TOKEN" psql \
    -h "$PG_HOST" -p 5432 -d "$PG_DATABASE" -U "$PG_ADMIN" \
    -Atc "SELECT COUNT(*) FROM pg_roles WHERE rolname = '$PG_ROLE'"
)"
if [[ "$ROLE_COUNT" != "0" ]]; then
  echo "PostgreSQL role still exists: $PG_ROLE" >&2
  exit 1
fi
remove_pg_rule
trap - EXIT

echo ">>> Deleting dedicated Azure resource group $RG"
if az group exists -n "$RG" | grep -qx true; then
  az group delete -n "$RG" --yes --no-wait
  for _ in $(seq 1 120); do
    if [[ "$(az group exists -n "$RG")" == "false" ]]; then
      break
    fi
    sleep 5
  done
fi

if [[ "$(az group exists -n "$RG")" != "false" ]]; then
  echo "Resource group deletion did not finish within 10 minutes." >&2
  exit 1
fi

rm -f "$STATE_FILE"
ELAPSED="$(( $(date +%s) - START_EPOCH ))"
echo "Recorder teardown verified in ${ELAPSED}s."
echo "Preserved shared PostgreSQL server/database and all pre-test rows."
