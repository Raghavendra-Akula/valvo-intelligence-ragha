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
