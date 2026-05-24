#!/usr/bin/env python3
"""
Send a fixed number of HTTP requests over a fixed duration and log every result.

Default target is the Vite dashboard route the user asked for. To test the
Flask rate limiter, point --url at a backend API route on port 5001 instead.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HTTP rate-limit probe")
    parser.add_argument(
        "--url",
        default="http://localhost:5173/dashboard",
        help="URL to request",
    )
    parser.add_argument(
        "--requests",
        type=int,
        default=1000,
        help="Total number of requests to send",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=60.0,
        help="Seconds over which requests are scheduled",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=50,
        help="Maximum concurrent worker threads",
    )
    parser.add_argument(
        "--log-file",
        default="rate_limit_probe.log",
        help="Path to write detailed request logs",
    )
    parser.add_argument(
        "--device-id",
        default="rate-limit-probe-device",
        help="Value for X-Client-Device-ID header",
    )
    parser.add_argument(
        "--auth-token",
        default="",
        help="Optional Bearer token for authenticated backend API routes",
    )
    return parser.parse_args()


def configure_logger(log_file: str) -> logging.Logger:
    path = Path(log_file).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("rate_limit_probe")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    logger.info("logging_to=%s", path)
    return logger


def request_once(index: int, url: str, device_id: str, auth_token: str) -> dict:
    started = time.perf_counter()
    headers = {
        "User-Agent": "valvo-rate-limit-probe/1.0",
        "X-Client-Device-ID": device_id,
        "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
    }
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            body = response.read(512)
            status = response.status
            response_headers = dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        body = exc.read(512)
        status = exc.code
        response_headers = dict(exc.headers.items())
    except Exception as exc:
        return {
            "index": index,
            "ok": False,
            "status": None,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "error": type(exc).__name__,
            "message": str(exc),
        }

    return {
        "index": index,
        "ok": 200 <= status < 400,
        "status": status,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "retry_after": response_headers.get("Retry-After"),
        "rate_limit_limit": response_headers.get("X-RateLimit-Limit"),
        "rate_limit_remaining": response_headers.get("X-RateLimit-Remaining"),
        "rate_limit_reset": response_headers.get("X-RateLimit-Reset"),
        "body_sample": body.decode("utf-8", "replace").replace("\n", " ")[:300],
    }


def main() -> int:
    args = parse_args()
    logger = configure_logger(args.log_file)

    if args.requests <= 0:
        raise SystemExit("--requests must be positive")
    if args.duration <= 0:
        raise SystemExit("--duration must be positive")
    if args.workers <= 0:
        raise SystemExit("--workers must be positive")

    logger.info(
        "starting url=%s requests=%s duration=%ss workers=%s device_id=%s",
        args.url,
        args.requests,
        args.duration,
        args.workers,
        args.device_id,
    )

    statuses: dict[str, int] = {}
    errors = 0
    started = time.perf_counter()
    interval = args.duration / args.requests
    futures = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for index in range(1, args.requests + 1):
            target_time = started + ((index - 1) * interval)
            sleep_for = target_time - time.perf_counter()
            if sleep_for > 0:
                time.sleep(sleep_for)
            futures.append(
                pool.submit(request_once, index, args.url, args.device_id, args.auth_token)
            )

        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            key = str(result.get("status") or result.get("error") or "unknown")
            statuses[key] = statuses.get(key, 0) + 1
            if not result.get("ok"):
                errors += 1
            logger.info("result=%s", json.dumps(result, sort_keys=True))

    elapsed = round(time.perf_counter() - started, 2)
    summary = {
        "url": args.url,
        "requests": args.requests,
        "duration_target_s": args.duration,
        "elapsed_s": elapsed,
        "statuses": statuses,
        "non_ok_count": errors,
    }
    logger.info("summary=%s", json.dumps(summary, sort_keys=True))
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
