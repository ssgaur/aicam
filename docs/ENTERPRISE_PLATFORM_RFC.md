# RFC 0001 — AiCam Enterprise Platform

**Status:** Draft · **Author:** @ssgaur · **Date:** 2026-06-27 · **Supersedes:** single-device design

> This RFC defines how AiCam evolves from a personal single-phone camera into a **multi-tenant
> SaaS for residential societies** that ingests **100+ concurrent live cameras**, detects
> **vehicles, delivery people, guests and house-helps**, and turns a 1,000+ house community's
> gate/common-area activity into objective, searchable records and alerts.

---

## 1. Goals / Non-goals

### Goals
- Ingest **100+ concurrent cameras**, each sending **10-second clips** (configurable **3–50 MB**,
  i.e. ~2 Mbps cheap Android → ~40 Mbps iPhone/IP-cam).
- **Source-agnostic backend**: start with native live apps (Android CameraX + native iOS),
  but the backend must accept RTSP/IP cameras later **with zero core changes**.
- Detect & classify: **vehicles (+ number plate), delivery, guest, house-help, resident**,
  and track across cameras (gate → block → floor).
- **Multi-tenant** (society → block → house → resident/guard/admin), with auth, RBAC, audit.
- Run it as a **business**: per-camera / per-house SaaS pricing with healthy margins.
- Be **secure & compliant**: real TLS, per-camera auth, India **DPDP Act 2023** for any biometric data.

### Non-goals (for now)
- Live sub-second streaming/PTZ control (we work in 10-second clips, not RTSP fan-out to viewers).
- On-prem-only deployments (cloud-first; edge connector is thin).
- Face recognition is **Phase 4 and consent-gated** — not in the MVP.

---

## 2. Context & personas

A 1,000+ house gated community where **~60% of homes are rented** (owners absent), so maintenance
and security decisions are made slowly and politically by a few committee members. AiCam's value:
**objective, automated records run the gate** — not consensus — and **absentee owners get a phone
app** showing exactly who entered their house/vehicle area.

| Persona | Needs |
|--------|-------|
| **Management committee / admin** | Live overview, daily digest, anomalies, staff attendance, vehicle in/out, billing |
| **Security guard** | Visitor approval, gate pass, vehicle/plate verify, house-help check-in |
| **Resident (incl. absentee owner)** | My visitors, my deliveries, my house-help attendance, alerts |
| **Operator (us)** | Multi-society fleet health, autoscaling, cost/margin per camera |

---

## 3. Architecture (decoupled, queue-driven, autoscaled)

```
 Cameras (phones now; RTSP/IP later)
   │  10s clips · per-camera JWT · quality tier
   ▼
[Ingest API]  register / initiate / complete  ──issues SAS──►  Blob (raw clips, 24–48h hot)
   │ enqueue job on :complete
   ▼
[Queue]  Service Bus / Kafka / Redis Streams      (100 cams = 10 clips/s = 864k/day)
   │ pull · backpressure · autoscale on depth
   ▼
[GPU worker pool]   decode → sample → YOLO(det) → ByteTrack → LPR/ANPR → (P4) Face/Re-ID
   │ events + embeddings
   ├──► Postgres (+pgvector): tenants, cameras, clips, detections, tracks, events, registries
   ├──► Vector store: vehicle/face embeddings (re-ID + semantic search)
   └──► Gemini Flash (gated on activity) → NL event summaries / anomaly narration
   ▼
[App/API behind a domain + TLS]   Admin dashboard · Guard app · Resident app · Billing
```

**Why decoupled:** today YOLO runs inline on the upload request on one VM — that caps throughput
and couples ingest to compute. Splitting **ingest → queue → worker pool** lets each scale
independently and absorbs bursts (the very pile-up we hit on one phone, at 100× scale).

---

## 4. The Clip Ingest Contract (the key abstraction)

The backend **never knows what the camera is**. Every source satisfies the same 4-step contract,
so phones and RTSP cameras are indistinguishable downstream.

```
1. Register   POST /v1/cameras
                 body: { tenant_id, name, location, tier }
                 → { camera_id, camera_token (JWT) }

2. Initiate   POST /v1/cameras/{id}/clips:initiate          (auth: camera_token)
                 body: { started_at, duration_ms, width, height, fps, bytes, codec }
                 → { clip_id, upload_url (SAS, short-lived), quality_tier }

3. Upload     PUT  <upload_url>                              (client → Blob directly)
                 body: the .mp4 bytes

4. Complete   POST /v1/cameras/{id}/clips:complete           (auth: camera_token)
                 body: { clip_id, blob_etag }
                 → 202 Accepted; a processing job is enqueued
```

- **Phone app (CameraX / iOS)** performs 1–4 itself.
- **RTSP/IP camera (Phase 5)** = a thin **edge connector** (MediaMTX/ffmpeg segments the stream
  into 10s clips) that performs the **same** 1–4. No backend change.
- **Direct-to-blob** (step 3) keeps ~200 Mbps of ingest off the API tier.
- Backwards-compat: the current `POST /api/native/upload` stays as a shim that internally does
  initiate→store→complete, so existing phones keep working during migration.

---

## 5. Camera quality tiers (3–50 MB) & why we downgraded

YOLO runs at **640px regardless of bitrate**, so high bitrate is wasted for *detection* — which is
why we dropped the personal camera from ~17 MB (14 Mbps) to ~2.5 MB (2 Mbps). But detail **is**
needed for **license plates and evidence**. Enterprise answer: **adaptive dual-quality, per-camera**.

| Tier | Target clip | Primary use |
|------|-------------|-------------|
| **Low** (cheap Android) | ~3 MB / 2 Mbps | Detection only |
| **Standard** | ~10 MB | Detection + decent LPR |
| **High** (iPhone / IP cam) | up to 50 MB | Forensic/evidence; **retained only when activity is flagged** |

Default: low-res for **all** cameras; capture/keep a high-res copy **only on flagged events**
→ quality where it matters, cost controlled. Tier is part of the camera registry and pushed to
the device at registration.

Measured baseline (current personal camera): 1280×720 H.264 High, ~30 fps (~295 frames/clip),
~2.08 Mbps, ~2.5 MB, no audio.

---

## 6. Intelligence stack

| Capability | Tech | Notes |
|-----------|------|-------|
| Object detection | YOLOv8/v11 (m on GPU) | person, car, truck, motorcycle, bicycle, bus |
| Multi-object tracking | ByteTrack | per-clip + cross-clip continuity |
| **Vehicle plate (LPR/ANPR)** | detector + OCR | match plate → **resident-vehicle registry** → resident vs visitor |
| Role classification | person attrs + context | delivery / guest / house-help / resident |
| Cross-camera **re-ID** | embeddings + pgvector | gate → block → floor journeys |
| **Face match** *(P4, consent-gated)* | embeddings vs staff/resident registry | DPDP Act: explicit consent, retention limits, deletion |
| **NL summaries / anomalies** | **Gemini Flash**, gated on `has_activity` | daily digest + alerts |

**Gemini cost control:** Gemini tokenizes video at **1 fps** (258 tok/frame default, 66 low-res),
not 30 fps. Gate on YOLO's `has_activity`, use low media-resolution + Batch API:

| Strategy (per 10s clip) | $/clip |
|---|---|
| Default res, Standard | $0.0009 |
| Low res, Batch | $0.0002 |

At 100 cameras, **only active clips (~15%)** + low-res + batch ≈ **₹700–5,000/month** total
(vs ₹19,600/mo if every clip were sent at default res).

---

## 7. Multi-tenant data model (sketch)

```
tenant(id, name, plan, …)                         -- a society
site(id, tenant_id, name)                          -- a society can have phases/sites
block(id, site_id, name)
house(id, block_id, number, owner_resident_id, is_rented)
resident(id, tenant_id, house_id, name, phone, role)   -- owner/tenant/guard/admin
camera(id, tenant_id, site_id, name, location, tier, token_hash, status)
clip(id, tenant_id, camera_id, started_at, duration_ms, blob_key, quality_tier, status)
detection(id, clip_id, cls, conf, bbox, track_id)
track(id, tenant_id, camera_id, cls, first_clip_id, last_seen, embedding vector)  -- pgvector
event(id, tenant_id, type, subject_ref, camera_id, at, summary, severity)         -- visitor/vehicle/help/anomaly
vehicle_registry(id, tenant_id, house_id, plate, label)
staff_registry(id, tenant_id, house_id, role, name, consent_at, embedding vector) -- P4, consented
audit_log(id, tenant_id, actor, action, target, at)
```

Every row carries `tenant_id`; partition `clip`/`detection` by tenant + time. Managed Postgres +
read replica; `pgvector` for embeddings/search.

---

## 8. Scaling math (100 cameras)

| Layer | Load | Sizing |
|------|------|--------|
| Ingest | 10 clips/s; ~200 Mbps @2 Mbps | Direct-to-blob via SAS (not through API) |
| Compute | ~50 frames/s (5/clip) | CPU nano ≈ maxes one D4s_v5; **GPU for real models.** 1–2× T4 (batched) w/ headroom |
| Storage | 2 Mbps → ~2.1 TB/day rolling 24h | Blob hot ≈ ₹3–4k/mo (**50 MB clips = 10× cost**) |
| DB | 864k clip-rows/day | Partition by tenant/time + read replica |

---

## 9. Cost & unit economics (100 cameras, indicative)

| Item | ₹/month |
|------|--------:|
| GPU workers (1–2× T4, some spot) | 25k–45k |
| Storage (2 Mbps rolling) | 3k–4k |
| Postgres managed + vector | 8k–15k |
| Gemini (gated) | 1k–5k |
| **Infra total** | **~₹40k–70k → ₹400–700 / camera / mo** |

SaaS price **₹1,500–3,000 / camera / mo** (or per-house) → healthy margin. The capitalistic pitch:
the system **runs the gate objectively**, replaces slow committee politics, and gives absentee
owners verifiable records — a service they will pay for per house.

---

## 10. Security & compliance (design in from day 1)

1. **Domain + real TLS** (Let's Encrypt via Caddy/Nginx) → removes the self-signed-cert hacks and
   the open `:8100`; clients trust it natively. **Prerequisite for everything.**
2. **Auth:** per-camera JWT (ingest), per-user OAuth/JWT (apps), RBAC by tenant/role, rate limits.
3. **India DPDP Act 2023:** face/biometric data needs **explicit consent, purpose limitation,
   retention limits, deletion rights**. Vehicle/plate + visitor logs are lower-risk; **face match
   is the sensitive piece → Phase 4, opt-in only.** Default retention: media 24–48h, events longer.
4. **Audit logs** on every privileged action; per-tenant data isolation.

---

## 11. Reliability & observability

- Idempotent processing (clip_id dedupe), dead-letter queue, retry with backoff (mirrors the
  phone-side UploadQueue, now server-side).
- Autoscale workers on queue depth; backpressure to apps when overloaded.
- Metrics: per-camera health/heartbeat, ingest rate, queue depth, processing latency, model FPS,
  cost/clip. Alert on camera offline, queue backlog, worker errors.

---

## 12. Phased roadmap

| Phase | Scope | Outcome |
|------|-------|---------|
| **P0** | Domain + TLS + per-camera auth | Secure, professional base; drop self-signed cert |
| **P1** | Camera registry + **Clip Ingest Contract** + queue + worker pool + multi-tenant schema | Scales to 100 cameras, source-agnostic; existing phone keeps working via shim |
| **P2** | GPU workers, ByteTrack, **LPR + vehicle registry**, per-camera quality tiers | Real detection + vehicle identity |
| **P3** | Resident/Guard/Admin apps, staff attendance, **Gemini digests**, alerts | Product the society uses daily |
| **P4** | **Face match (consent-gated)**, cross-camera re-ID, analytics, **billing/SaaS** | Full platform + revenue |
| **P5** | **RTSP/IP edge connector** | IP cameras via the same contract, zero core change |

---

## 13. Open decisions

- **Cloud/runtime for workers:** Azure Container Apps vs AKS vs VM Scale Set (GPU). (Currently all
  Azure; VM is `aicam-server` D4s_v5.)
- **Queue:** Azure Service Bus vs Redis Streams vs Kafka (start simplest that gives DLQ + autoscale).
- **Face recognition:** in scope at all, or vehicle+visitor only? (Legal/DPDP weight.)
- **Pricing unit:** per-camera vs per-house vs per-society tier.
- **Domain name:** pick and register (see RFC §10).

---

## 14. Relationship to existing code

- Existing repo: `ssgaur/aicam` (this RFC lives here). Phone app `AiCameraX/`, pipeline
  `native_camera_pipeline.py`, backend `backend/main.py`, web `viewer.html`, Flutter `AiCamViewer/`.
- A native SwiftUI viewer also ships as the **Camera tab** in the Neighbourly app
  (`ssgaur/blf-telegram-automation`) — the resident-facing app surface can grow from there.
- P1 keeps `POST /api/native/upload` as a compatibility shim so nothing breaks during migration.
