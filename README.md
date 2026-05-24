# valvo-intelligence-ragha

## API Rate Limiting

The backend uses Flask-Limiter from `extensions.py` and applies a moving-window
rate limit per browser/device.

### Device Criteria

- The frontend stores a stable device ID in browser `localStorage` under
  `valvo_client_device_id_v1`.
- Every backend request made through the frontend API helper sends that value in
  the `X-Client-Device-ID` header.
- The backend hashes `X-Client-Device-ID` and uses it as the rate-limit bucket.
- Same `X-Client-Device-ID` means same device bucket.
- Different `X-Client-Device-ID` values mean separate device buckets.
- If a client does not send `X-Client-Device-ID`, the backend falls back to an
  approximate bucket made from IP address plus `User-Agent`.

### Current Per-Device Limits

The current global limits are:

- `100 per 10 seconds`
- `300 per minute`
- `2000 per hour`

That means one device can make up to 100 requests in any 10-second moving
window. Request 101 from the same device inside that window receives HTTP `429`
with a JSON `rate_limit_exceeded` response.

Some routes may also define stricter route-level limits. If a route has a
stricter decorator, that route can return `429` before the global per-device
limit is reached.

### Local Verification

Use the backend port, not the Vite frontend port, when testing rate limiting:

```bash
./.venv/bin/python scripts/rate_limit_threshold_probe.py \
  --url http://127.0.0.1:5001/ \
  --max-requests 105 \
  --log-file backend_100_per_10s_threshold.log \
  --device-id verify-100-per-10s
```

Expected result: 100 successful responses, then request 101 returns `429`.
