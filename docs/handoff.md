# OpenFire UI v2 — Handoff Notes

**From:** Sebastian (`sebaleksander@gmail.com`)
**Date:** 2026-05-07
**Branch:** `sebdevUImaplibre` (off `dev`)
**Target service:** `openfire-ui-socal-deckgl` (Cloud Run, `us-central1`, project `msds603-mlops-project`)
**Live URL of current deploy:** https://openfire-ui-socal-deckgl-222683846563.us-central1.run.app (still the older deck.gl prototype — replace by deploying this branch)

---

## 1. Quick context block to paste into an LLM (Claude / ChatGPT)

> You're helping me work on **OpenFire SoCal UI v2**. It's a public-facing wildfire risk web app.
>
> **Stack:** FastAPI (Python 3.11) backend + static frontend (vanilla JS, no framework). Frontend uses **MapLibre GL JS 4.7** for the basemap and **deck.gl 9** for a GPU-rendered heatmap overlay. They share a single WebGL canvas via `MapboxOverlay({ interleaved: true })`. The `HeatmapLayer` renders *underneath* the basemap's water layer using `beforeId: state.waterBeforeId` — that gives free pixel-perfect water clipping.
>
> **Repo:** `tkovalcik/openfire`, branch `sebdevUImaplibre`. The frontend lives in [frontend-socal-deckgl/](../frontend-socal-deckgl/). The FastAPI app that serves it is [src/ui_socal/app.py](../src/ui_socal/app.py). The Cloud Run service is `openfire-ui-socal-deckgl` in GCP project `msds603-mlops-project`, region `us-central1`, deployed via [.github/workflows/ui_socal_deckgl.yml](../.github/workflows/ui_socal_deckgl.yml) (manual `workflow_dispatch`).
>
> **Data flow:** the live manifest at `gs://openfire/predictions/manifest.json` lists ~26 risk windows (every 5 days, late 2025 → present). Each window is a GeoJSON of grid cells with `risk_probability` ∈ [0,1]. The frontend fetches the manifest, then per-window streams via FastAPI's `/data/{name}` proxy (which checks `STATIC_ROOT/data/` first, then GCS).
>
> **Persistence:** alert subscriptions go to `POST /api/subscriptions` → BigQuery table `msds603-mlops-project.openfire_features.ui_subscriptions` (auto-created on first insert).
>
> **Read first:** [docs/problems_solved.md](problems_solved.md) for the gotchas we already hit. Don't repeat them.

---

## 2. Current progress (what's done)

### Frontend rewrite ([frontend-socal-deckgl/](../frontend-socal-deckgl/))
- [x] Migrated from Leaflet+CircleMarker → MapLibre GL JS + deck.gl 9 (single shared WebGL canvas, seamless zoom)
- [x] GPU heatmap (`HeatmapLayer`, MEAN aggregation, zoom-aware kernel radius 36–300px)
- [x] Water clipping via `beforeId` interleaved-mode (no custom mask geometry)
- [x] Basemap: OpenFreeMap Positron (no API key, env-overridable via `OPENFIRE_BASEMAP_STYLE`)
- [x] Hero block + 3-bullet value prop ("Hyperlocal wildfire risk")
- [x] Time slider + autoplay loop (starts on most recent window, wraps cleanly)
- [x] Continuous-gradient legend (CSS strip; data layer still uses 8-stop band internally)
- [x] Address-lookup panel (Nominatim geocoder, 1 req/sec throttled, finds nearest cell, flies map there)
- [x] Subscribe panel (email + ZIP + risk-threshold slider)
- [x] Hover tooltip (transparent `ScatterplotLayer` for picking; `HeatmapLayer` is a texture so has no native picking)
- [x] Stripped dead Snapshot/Performance/Source panels (telemetry pipeline still fires; only its UI is hidden)
- [x] All 13 frontend asset tests pass

### Backend ([src/ui_socal/app.py](../src/ui_socal/app.py))
- [x] `POST /api/subscriptions` endpoint
- [x] Pydantic `SubscriptionRequest` with email regex + 5-digit ZIP regex + threshold ∈ [0,1]
- [x] Auto-create BQ table on first insert (mirrors existing `_ensure_ui_perf_table` pattern)
- [x] SHA-256 truncated client-IP hash for soft anti-abuse signal

### Docs
- [x] [docs/problems_solved.md](problems_solved.md) — gotchas + lessons (read this first)
- [x] [docs/handoff.md](handoff.md) — this file

---

## 3. Main issues / bottlenecks

| # | Issue | Severity | Notes |
|---|---|---|---|
| 1 | **No auto-deploy on `dev` push.** [.github/workflows/ui_socal_deckgl.yml](../.github/workflows/ui_socal_deckgl.yml) is `workflow_dispatch` only. Every deploy is manual. | medium | Decide: add `push: branches: [dev]` trigger? Tomasko likely intentionally kept it manual for the prototype. |
| 2 | **Subscribe form captures emails but does nothing else.** No SMS, no email alerts, no triggered notification when risk crosses threshold. | high (product) | Pure capture MVP. The actual alert pipeline is unbuilt. |
| 3 | **Heatmap params are not exposed in the UI** — `radiusPixels`, `threshold`, `intensity` are constants in [frontend-socal-deckgl/app.js:43-62](../frontend-socal-deckgl/app.js#L43). Tuning requires a code edit + redeploy. | low | Could expose as URL query params for quick A/B, or just leave as code constants. |
| 4 | **Address lookup uses Nominatim** (free OSM geocoder, 1 req/sec policy). Flaky under load and rate-limited. | low | Fine for demo. For production, swap to a paid geocoder (Mapbox, Google) or pre-computed ZIP centroids. |
| 5 | **The 18MB demo geojson** [frontend-socal/data/socal_20240726_risk.geojson](../frontend-socal/data/socal_20240726_risk.geojson) is the static-demo fallback. Production uses the live manifest, but the static path is what runs when `OPENFIRE_USE_LIVE_MANIFEST=false`. Big file in git. | low | Tomasko already committed it. We didn't make this worse. |
| 6 | **No PR open yet.** Branch `sebdevUImaplibre` pushed but not merged. | — | I'll open the PR; merge can wait until after review. |
| 7 | **Browser cache during dev.** Cloud Run sets `Cache-Control: no-cache` on HTML. Hard refresh (Cmd+Shift+R) is still needed when iterating on `app.js` locally. | trivial | Just a note. |

---

## 4. Targets / features to build (priority order)

### P0 — needed for "real" subscribe behaviour
1. **Outbound alert worker.** Cloud Run *job* (not service) on a scheduler. Reads `ui_subscriptions` + the latest risk windows, finds rows where the cell at the user's ZIP exceeds their `risk_threshold`, and sends an email (SendGrid/Resend) or SMS (Twilio). This is the missing 80% of the subscribe feature.
   - Suggested table additions: `last_alerted_at TIMESTAMP NULLABLE` (so we don't spam on consecutive days) and `is_active BOOLEAN DEFAULT TRUE`.
   - Suggested env vars on the worker: `SENDGRID_API_KEY` (or equivalent), `OPENFIRE_ALERT_FROM_EMAIL`, `OPENFIRE_ALERT_DRY_RUN`.
2. **ZIP → centroid table.** Right now we don't actually translate ZIP → lat/lon at submit time; we just store the ZIP. Either (a) let the worker join against a ZIP-centroid table, or (b) resolve at submit time and store `zip_lat` / `zip_lon` columns. (b) is faster but loses the canonical ZIP if the user moves.

### P1 — product polish
3. **Phone number field** on the subscribe form. Add `phone STRING NULLABLE` to the BQ schema, optional `phone` field on `SubscriptionRequest` (E.164 regex), require at least one of `email`/`phone` via a custom validator. Frontend: one extra `<input type="tel">`.
4. **Crossfade between time-window swaps.** Right now swapping `data` on the `HeatmapLayer` causes a brief flash. Render two layers and fade old → new over ~250ms.
5. **LRU cap on `state.cache`.** Currently unbounded; with 26 windows × ~600KB each (live) → ~16MB in memory. Fine for now but cap at 5–7 once we go to longer histories.

### P2 — analytics / ops
6. **Subscribe-success → BQ event** in `ui_performance_events` (so we can track conversion in the existing dashboard).
7. **`/healthz` endpoint** that probes BQ + GCS connectivity. Useful for Cloud Run uptime checks.
8. **Email verification** (double opt-in). Send a confirmation link before marking the row active. Anti-abuse + GDPR-friendly. Adds two endpoints: `POST /api/subscriptions/verify-send`, `GET /api/subscriptions/verify/{token}`.

### P3 — stretch
9. Expose `radiusPixels` / `threshold` as URL query params for live tuning (`?heat_radius=120&heat_threshold=0.05`).
10. Swap basemap to MapTiler or Stadia for richer styles (env var `OPENFIRE_BASEMAP_STYLE` already supported — just need an API key set in workflow vars).
11. **Polygon AOI overlay toggle** — let the user see the AOI county boundaries as a thin line on top of the heat.

---

## 5. How to deploy this branch (specific commands)

### One-time: confirm gcloud + GitHub access
```bash
# auth check
gcloud auth list                                  # should show your account active
gcloud config set project msds603-mlops-project   # so your default project is right
gh auth status                                    # should show authenticated
```

### A. Open the PR
```bash
gh pr create \
  --base dev \
  --head sebdevUImaplibre \
  --title "feat(ui): SoCal UI v2 — MapLibre + deck.gl heatmap, subscribe form, address lookup" \
  --body "See docs/handoff.md for full context. Replaces the Leaflet/CircleMarker prototype with a GPU heatmap; adds subscribe + address lookup; auto-creates BQ ui_subscriptions on first POST."
```

### B. Merge & deploy

After review, merge to `dev`. **The deploy workflow does NOT auto-trigger on push** — you must run it manually:

```bash
gh workflow run ui_socal_deckgl.yml --ref dev
# or via UI: Actions → "Deploy SoCal UI deck.gl Prototype" → Run workflow
```

That builds `docker/Dockerfile.ui_socal_deckgl`, pushes to Artifact Registry as `us-central1-docker.pkg.dev/msds603-mlops-project/openfire/ui-socal-deckgl:<sha>`, and deploys to Cloud Run service `openfire-ui-socal-deckgl`.

Watch the deploy:
```bash
gh run list --workflow=ui_socal_deckgl.yml --limit 1
gh run watch
```

### C. Smoke test the deployed service
```bash
URL=$(gcloud run services describe openfire-ui-socal-deckgl \
  --project msds603-mlops-project --region us-central1 \
  --format 'value(status.url)')

curl -fsS "$URL/" -o /dev/null && echo OK                  # index.html
curl -fsS "$URL/runtime-config.js" | head -3              # confirms env-config emitter
curl -fsS "$URL/data/manifest.json" | head -3             # GCS proxy works

# subscribe smoke:
curl -fsS -X POST "$URL/api/subscriptions" \
  -H 'content-type: application/json' \
  -d '{"email":"smoketest@openfire.dev","zip":"94110","risk_threshold":0.5}'
# → {"status":"ok"}

bq query --project_id=msds603-mlops-project --use_legacy_sql=false \
  'SELECT COUNT(*) FROM openfire_features.ui_subscriptions'
# → 1 (or whatever count)
```

---

## 6. How to set up the subscriptions DB

**Good news: there's no manual setup.** The first `POST /api/subscriptions` to a deployed service auto-creates the BQ table via [`_ensure_subs_table`](../src/ui_socal/app.py#L168) (same pattern as `_ensure_ui_perf_table`).

If you'd rather pre-create it (so the first user-facing POST doesn't pay the create-table latency):

```bash
bq mk --table \
  --time_partitioning_field=created_at \
  --time_partitioning_type=DAY \
  --clustering_fields=zip,email \
  msds603-mlops-project:openfire_features.ui_subscriptions \
  created_at:TIMESTAMP:REQUIRED,email:STRING:REQUIRED,zip:STRING:REQUIRED,risk_threshold:FLOAT64,source_ip_hash:STRING,user_agent:STRING,app_version:STRING
```

Schema reference (matches [src/ui_socal/app.py `_subs_schema()`](../src/ui_socal/app.py)):

| Column | Type | Mode | Notes |
|---|---|---|---|
| `created_at` | TIMESTAMP | REQUIRED | UTC, server-set |
| `email` | STRING | REQUIRED | lowercased, trimmed |
| `zip` | STRING | REQUIRED | 5-digit, regex-validated |
| `risk_threshold` | FLOAT64 | NULLABLE | 0.0–1.0 (matches `risk_probability` scale) |
| `source_ip_hash` | STRING | NULLABLE | SHA-256[:32] of client IP |
| `user_agent` | STRING | NULLABLE | truncated to 512 chars |
| `app_version` | STRING | NULLABLE | git SHA from `OPENFIRE_UI_VERSION` |

**Required IAM** on the Cloud Run runtime SA (`openfire-ui-socal-runner@msds603-mlops-project.iam.gserviceaccount.com`):
- `roles/bigquery.dataEditor` on dataset `openfire_features` (insert + create-table)
- `roles/bigquery.jobUser` on the project (run insertion jobs)
- `roles/storage.objectViewer` on bucket `openfire` (data proxy)

Existing perf table proves all of these are already granted — no new IAM needed for v2.

To see incoming subscriptions:
```bash
bq query --project_id=msds603-mlops-project --use_legacy_sql=false \
  'SELECT created_at, email, zip, risk_threshold, app_version
   FROM openfire_features.ui_subscriptions
   ORDER BY created_at DESC LIMIT 20'
```

---

## 7. File map for adding features

| Want to... | Edit |
|---|---|
| Tune heatmap smoothness | [frontend-socal-deckgl/app.js:43-62](../frontend-socal-deckgl/app.js#L43) (`HEATMAP_THRESHOLD`, `heatmapRadiusForZoom`) |
| Change basemap style | [frontend-socal-deckgl/config.js:23](../frontend-socal-deckgl/config.js) or env var `OPENFIRE_BASEMAP_STYLE` |
| Change risk color gradient | [frontend-socal-deckgl/config.js](../frontend-socal-deckgl/config.js) (`riskGradient`) |
| Add a field to subscribe form | [frontend-socal-deckgl/index.html](../frontend-socal-deckgl/index.html) (`#subscribe-form`) + [frontend-socal-deckgl/app.js](../frontend-socal-deckgl/app.js) (`bindSubscribeForm`) + [src/ui_socal/app.py](../src/ui_socal/app.py) (`SubscriptionRequest`, `_subs_schema`, `create_subscription`) |
| Add a new API endpoint | [src/ui_socal/app.py](../src/ui_socal/app.py). **Register before** the catch-all `@app.get("/{path:path}")` near line 446. |
| Add a new GeoJSON window source | Push to GCS at `gs://openfire/predictions/`, then update [`gs://openfire/predictions/manifest.json`](https://console.cloud.google.com/storage/browser/openfire/predictions). The frontend picks it up automatically (live manifest is fetched per session). |
| Change Docker / container | [docker/Dockerfile.ui_socal_deckgl](../docker/Dockerfile.ui_socal_deckgl), [docker/requirements.ui_socal.txt](../docker/requirements.ui_socal.txt) |
| Change deploy params | [.github/workflows/ui_socal_deckgl.yml](../.github/workflows/ui_socal_deckgl.yml) |
| Add a frontend asset test | [tests/test_frontend_socal_assets.py](../tests/test_frontend_socal_assets.py) |

---

## 8. Local dev loop

```bash
# (optional) recreate openfire conda env
conda create -n openfire python=3.11 -y
conda activate openfire
pip install -r docker/requirements.ui_socal.txt
pip install -r docker/requirements.dev.txt   # pytest + httpx for tests

# run tests
pytest tests/test_frontend_socal_assets.py -q

# run the FastAPI app locally (live-reloads on .py changes)
OPENFIRE_SOCAL_STATIC_ROOT=$PWD/frontend-socal-deckgl \
OPENFIRE_USE_LIVE_MANIFEST=true \
  uvicorn src.ui_socal.app:app --reload --port 8080

# open http://localhost:8080
```

For pure-frontend iteration (no Python reload), `cd frontend-socal-deckgl && python -m http.server 8080`. **Caveat:** the static server can't proxy GCS, so set `OPENFIRE_USE_LIVE_MANIFEST=false` *or* edit `frontend-socal-deckgl/data/socal_demo_manifest.json` to point at local files.

---

## 9. Open questions for you

1. Auto-deploy on `dev` merge — yes/no?
2. Email vs SMS first for the alert worker?
3. Do we want phone numbers captured *now* (low effort, see P1 above) so we have data when the SMS pipeline lands later?
4. Should the subscribe success/failure be tracked as a `ui_performance_events` row for dashboard parity?
