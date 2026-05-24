# Valvo Backend — Deployment Guide

## Cloud Run Deploy Command

```bash
gcloud run deploy valvo-backend \
  --source ./Backend \
  --region asia-south1 \
  --allow-unauthenticated \
  --memory 2Gi \
  --cpu 2 \
  --min-instances 1 \
  --max-instances 6 \
  --concurrency 24 \
  --set-env-vars="DB_HOST=<from_secret_manager>,\
DB_NAME=postgres,\
DB_USER=<from_secret_manager>,\
DB_PASSWORD=<from_secret_manager>,\
DB_PORT=6543,\
SUPABASE_URL=<from_secret_manager>,\
SUPABASE_SERVICE_KEY=<from_secret_manager>,\
SUPABASE_JWT_SECRET=<from_secret_manager>,\
GEMINI_API_KEY=<from_secret_manager>,\
ANTHROPIC_API_KEY=<from_secret_manager>,\
SARVAM_API_KEY=<from_secret_manager>,\
ALLOWED_ORIGINS=https://app.valvointelligence.com,https://valvointelligence.com"
```

## ⚠️ Security Rules
- NEVER hardcode credentials in this file or any source file
- All secrets must come from Google Secret Manager or Cloud Run env vars set via console
- Rotate credentials immediately if they appear in git history

## Environment Variables Reference

| Variable | Description |
|---|---|
| DB_HOST | Supabase transaction pooler host |
| DB_NAME | Always `postgres` |
| DB_USER | Supabase pooler user (format: postgres.PROJECT_ID) |
| DB_PASSWORD | Supabase DB password |
| DB_PORT | Always `6543` (transaction pooler) |
| SUPABASE_URL | Project URL from Supabase dashboard |
| SUPABASE_SERVICE_KEY | Service role key (never expose to frontend) |
| SUPABASE_JWT_SECRET | JWT secret from Supabase dashboard |
| GEMINI_API_KEY | Google AI Studio key |
| ANTHROPIC_API_KEY | Anthropic console key |
| SARVAM_API_KEY | Sarvam AI dashboard key |
| ALLOWED_ORIGINS | Comma-separated frontend domains |
| RATELIMIT_ENABLED | Enable/disable API rate limiting. Default: `true` |
| RATELIMIT_STRATEGY | Rate limit algorithm used by Flask-Limiter. Default: `moving-window` |
| RATELIMIT_DEFAULTS | Comma-separated per-route fallback limits. Default: `120 per minute,1200 per hour` |
| RATELIMIT_APPLICATION_LIMITS | Global per-device burst/abuse limits applied to all endpoints. Default: `100 per 10 seconds,300 per minute,2000 per hour` |
| RATELIMIT_STORAGE_URI | Flask-Limiter storage URI. Default `memory://`; use shared Redis/Memorystore in multi-worker production |
| TRUST_PROXY_HEADERS | Trust `X-Forwarded-*` headers via Werkzeug ProxyFix. Default: `true` for Cloud Run |
| DHAN_PARTNER_ID | Dhan-issued partner ID (for the consent flow). Leave unset to disable Dhan integration. |
| DHAN_PARTNER_SECRET | Dhan-issued partner secret. Sent in the `partner_secret` header on consent calls. |
| DHAN_CONSENT_REDIRECT_URI | Where Dhan redirects users after login, e.g. `https://app.valvointelligence.com/auth/dhan/callback`. Must be registered with Dhan and match the frontend route. |
| DHAN_ORDER_PROXY_URL | URL of the order proxy on the whitelisted VM, e.g. `https://pipeline.valvointelligence.com/dhan-proxy/orders`. Required for placing/modifying/cancelling orders (Dhan enforces static-IP on those endpoints). |
| DHAN_ORDER_PROXY_SECRET | Shared secret with the VM proxy. Backend sends it in `X-Proxy-Secret`. |

## Current Cloud Run Configuration
- Memory: 2 GiB
- CPU: 2 vCPU (always allocated)
- CPU Boost: ON
- Min instances: 1 (always warm)
- Max instances: 6
- Concurrency: 24
- Gunicorn: 3 workers, 8 threads, gthread class, --preload
- DB Pool: minconn=2, maxconn=5, port=6543 (transaction mode)

## Rate Limiting Guidelines

### Criteria

Rate limiting is scoped to one browser/device bucket. The frontend creates a
stable device identifier in `localStorage` and sends it on backend requests as
`X-Client-Device-ID`. The backend hashes that header and uses it as the
Flask-Limiter key.

- Same `X-Client-Device-ID`: same rate-limit bucket.
- Different `X-Client-Device-ID`: independent rate-limit bucket.
- Missing `X-Client-Device-ID`: fallback bucket is based on IP address plus
  `User-Agent`.

This is a browser/device identifier, not a physical hardware fingerprint. A
client that clears browser storage or forges the header can look like a new
device. For stronger abuse controls, add a second IP-based or authenticated-user
limit in addition to the current per-device limit.

### Active Limits

The default global per-device limits are:

- `100 per 10 seconds`
- `300 per minute`
- `2000 per hour`

The first 100 requests from the same device inside a 10-second moving window are
allowed. Request 101 inside that same window is denied with HTTP `429` and a JSON
`rate_limit_exceeded` response. The response includes rate-limit headers such as
`Retry-After`, `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and
`X-RateLimit-Reset`.

Route-specific decorators can still be stricter. For example, if an endpoint has
its own lower route limit, that endpoint can return `429` before the global
per-device limit is reached.

### Production Storage Requirement

The default `memory://` storage is acceptable only for local development and
single-process testing. Cloud Run currently uses multiple Gunicorn workers and
can scale to multiple instances, so memory-backed counters are not shared across
all request handlers.

For production, configure a shared backend such as Redis or Google Cloud
Memorystore:

```text
RATELIMIT_STORAGE_URI=redis://<host>:6379/0
```

Without shared storage, each worker or instance can maintain its own separate
counter, so the practical limit can be higher than `100 per 10 seconds`.

### Verification Command

Run the threshold probe against the Flask backend port, not the Vite frontend
port:

```bash
./.venv/bin/python scripts/rate_limit_threshold_probe.py \
  --url http://127.0.0.1:5001/ \
  --max-requests 105 \
  --log-file backend_100_per_10s_threshold.log \
  --device-id verify-100-per-10s
```

Expected result: responses 1 through 100 return `200`; request 101 returns
`429`.
