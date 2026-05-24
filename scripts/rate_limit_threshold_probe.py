#!/usr/bin/env python3
"""
Find the first request where the API starts returning 429 Too Many Requests.

This is intentionally aimed at local/self-owned URLs. By default it refuses to
probe non-localhost targets unless --allow-non-local is passed.
"""
from __future__ import annotations

import argparse
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Find rate-limit denial threshold")
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:5001/",
        help="Backend URL to probe. Use port 5001 to test Flask, not Vite port 5173.",
    )
    parser.add_argument("--max-requests", type=int, default=1000)
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.0,
        help="Delay between requests in seconds. Keep 0 for burst testing.",
    )
    parser.add_argument("--log-file", default="rate_limit_threshold_probe.log")
    parser.add_argument("--device-id", default="rate-limit-threshold-probe")
    parser.add_argument("--auth-token", default="", help="Optional Bearer token")
    parser.add_argument(
        "--allow-non-local",
        action="store_true",
        help="Allow probing URLs outside localhost/127.0.0.1",
    )
    return parser.parse_args()


def configure_logger(log_file: str) -> logging.Logger:
    path = Path(log_file).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("rate_limit_threshold_probe")
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


def assert_local_target(url: str, allow_non_local: bool) -> None:
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname
    if allow_non_local or host in LOCAL_HOSTS:
        return
    raise SystemExit(
        f"Refusing to probe non-local host {host!r}. "
        "Use --allow-non-local only for systems you own and are authorized to test."
    )


def request_once(index: int, url: str, device_id: str, auth_token: str) -> dict:
    started = time.perf_counter()
    headers = {
        "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
        "User-Agent": "valvo-rate-limit-threshold-probe/1.0",
        "X-Client-Device-ID": device_id,
    }
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            body = response.read(300)
            status = response.status
            response_headers = dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        body = exc.read(300)
        status = exc.code
        response_headers = dict(exc.headers.items())
    except Exception as exc:
        return {
            "index": index,
            "status": None,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "error": type(exc).__name__,
            "message": str(exc),
        }

    return {
        "index": index,
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
    assert_local_target(args.url, args.allow_non_local)
    logger = configure_logger(args.log_file)

    if args.max_requests <= 0:
        raise SystemExit("--max-requests must be positive")
    if args.sleep < 0:
        raise SystemExit("--sleep cannot be negative")

    logger.info(
        "starting url=%s max_requests=%s sleep=%s device_id=%s",
        args.url,
        args.max_requests,
        args.sleep,
        args.device_id,
    )

    started = time.perf_counter()
    statuses: dict[str, int] = {}
    first_429 = None

    for index in range(1, args.max_requests + 1):
        result = request_once(index, args.url, args.device_id, args.auth_token)
        key = str(result.get("status") or result.get("error") or "unknown")
        statuses[key] = statuses.get(key, 0) + 1
        logger.info("result=%s", json.dumps(result, sort_keys=True))

        if result.get("status") == 429:
            first_429 = result
            break

        if args.sleep:
            time.sleep(args.sleep)

    summary = {
        "url": args.url,
        "max_requests": args.max_requests,
        "sent_requests": sum(statuses.values()),
        "elapsed_s": round(time.perf_counter() - started, 3),
        "statuses": statuses,
        "first_429_at_request": first_429.get("index") if first_429 else None,
        "first_429": first_429,
    }
    logger.info("summary=%s", json.dumps(summary, sort_keys=True))
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
