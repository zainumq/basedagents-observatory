#!/usr/bin/env python3
"""Read-only uptime/latency probe for the BasedAgents public API.

Samples four public GET endpoints once per round and appends one JSON record
per sample (plus probe start/stop records) to a JSONL file.

Endpoints (base https://api.basedagents.ai):
  status       GET /v1/status
  tasks_open   GET /v1/tasks?status=open
  tasks_settled GET /v1/tasks/settled
  task_detail  GET /v1/tasks/task_LHJPMel3PnqH4aqCwza2c

Rules this probe follows:
  * Read-only: GET requests only, never POST/PUT/DELETE.
  * No retries: one attempt per sample. A failed attempt IS a sample
    (ok=false) — retrying would hide target downtime.
  * 5-minute interval per vantage is the ceiling. This is monitoring,
    not a load test. Do not sample faster in the pilot.
  * Timeout 10s per request.

Schema (one JSON object per line in the output file):
  Sample record:
    {"type": "sample", "ts": "2026-11-15T06:30:00.123Z",
     "endpoint": "status", "vantage": "vantage-a",
     "latency_ms": 812.34, "http_status": 200, "ok": true,
     "cache": {"cache-control": "...", "etag": "..."},
     "error": null, "probe_status": "ok"}
  Lifecycle records:
    {"type": "probe_start", "ts": "...", "vantage": "vantage-a",
     "interval_min": 5, "endpoints": ["status", ...]}
    {"type": "probe_stop", "ts": "...", "vantage": "vantage-a",
     "reason": "shutdown"}

  probe_status distinguishes OUR collection failures from TARGET downtime:
    "ok"          — the probe ran and attempted the request; ok/http_status
                    describe what the target did (including timeouts and
                    connection refusals, which are target-side signals).
    "probe_error" — the probe itself failed before a request could be
                    attempted or recorded (e.g. DNS resolution failure on
                    our side, local network error). These samples are
                    EXCLUDED from availability math by the analyzer and are
                    reported separately as collection gaps.

  A missing round (no record at all) is also a collection gap; the
  probe_start/probe_stop records let the analyzer attribute gaps to
  probe downtime rather than target downtime.

Usage:
  probe.py --once   --vantage vantage-a --out samples.jsonl
  probe.py --daemon --vantage vantage-a --out samples.jsonl [--interval 5]

Exit code is 0 as long as the probe itself ran (even if endpoints failed);
exit 1 only on probe-side errors (bad args, unwritable output).
"""

import argparse
import json
import signal
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

BASE_URL = "https://api.basedagents.ai"
USER_AGENT = "basedagents-observatory/1.0 (read-only probe)"
TIMEOUT_S = 10

ENDPOINTS = [
    ("status", "/v1/status"),
    ("tasks_open", "/v1/tasks?status=open"),
    ("tasks_settled", "/v1/tasks/settled"),
    ("task_detail", "/v1/tasks/task_LHJPMel3PnqH4aqCwza2c"),
]

CACHE_HEADERS = ("cache-control", "etag", "age", "x-cache")


def iso_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def classify_error(exc):
    """Return (probe_status, error_string) for a request exception.

    DNS / local-network failures are OUR problem (probe_error).
    Timeouts and connection refusals are target-side signals (ok status,
    recorded as a failed sample, not a probe error).
    """
    if isinstance(exc, socket.timeout) or isinstance(exc, TimeoutError):
        return "ok", "timeout"
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, socket.gaierror):
            return "probe_error", "dns: %s" % reason
        if isinstance(reason, ConnectionRefusedError):
            return "ok", "connection refused"
        if isinstance(reason, socket.timeout) or isinstance(reason, TimeoutError):
            return "ok", "timeout"
        if isinstance(reason, OSError):
            # Other local/network errors (unreachable network, etc.): our side.
            return "probe_error", "network: %s" % reason
        return "probe_error", "urlerror: %s" % reason
    return "probe_error", "%s: %s" % (type(exc).__name__, exc)


def sample_endpoint(name, path, vantage):
    url = BASE_URL + path
    ts = iso_now()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            # Read a bounded amount: we only need the status/headers, but
            # reading a little proves the body actually arrives.
            resp.read(65536)
            latency_ms = (time.perf_counter() - start) * 1000.0
            cache = {}
            for h in CACHE_HEADERS:
                v = resp.headers.get(h)
                if v is not None:
                    cache[h] = v
            return {
                "type": "sample",
                "ts": ts,
                "endpoint": name,
                "vantage": vantage,
                "latency_ms": round(latency_ms, 2),
                "http_status": resp.status,
                "ok": 200 <= resp.status < 300,
                "cache": cache,
                "error": None,
                "probe_status": "ok",
            }
    except urllib.error.HTTPError as e:
        # The server answered with an error status: a real target signal.
        latency_ms = (time.perf_counter() - start) * 1000.0
        cache = {}
        for h in CACHE_HEADERS:
            v = e.headers.get(h) if e.headers else None
            if v is not None:
                cache[h] = v
        return {
            "type": "sample",
            "ts": ts,
            "endpoint": name,
            "vantage": vantage,
            "latency_ms": round(latency_ms, 2),
            "http_status": e.code,
            "ok": False,
            "cache": cache,
            "error": "http %d" % e.code,
            "probe_status": "ok",
        }
    except Exception as e:  # noqa: BLE001 - every failure mode is a sample
        latency_ms = (time.perf_counter() - start) * 1000.0
        probe_status, error = classify_error(e)
        return {
            "type": "sample",
            "ts": ts,
            "endpoint": name,
            "vantage": vantage,
            "latency_ms": round(latency_ms, 2),
            "http_status": None,
            "ok": False,
            "cache": {},
            "error": error,
            "probe_status": probe_status,
        }


def write_record(fh, record):
    fh.write(json.dumps(record, sort_keys=True) + "\n")
    fh.flush()


def run_round(fh, vantage):
    """One round: sample all endpoints, append records, print a summary."""
    results = []
    for name, path in ENDPOINTS:
        rec = sample_endpoint(name, path, vantage)
        write_record(fh, rec)
        results.append(rec)
    ok_count = sum(1 for r in results if r["ok"])
    max_lat = max((r["latency_ms"] for r in results), default=0.0)
    print(
        "%s %s round: %d/%d ok, max latency %.1f ms"
        % (iso_now(), vantage, ok_count, len(results), max_lat),
        flush=True,
    )
    return results


def next_boundary(interval_s):
    """Next wall-clock boundary aligned to the interval (for --daemon).

    Aligning rounds to clock boundaries makes samples from different
    vantages line up, which the analyzer uses for vantage agreement.
    """
    now = time.time()
    return (int(now // interval_s) + 1) * interval_s


def main(argv=None):
    ap = argparse.ArgumentParser(description="BasedAgents API observatory probe (read-only).")
    ap.add_argument("--vantage", required=True, help="Vantage label, e.g. vantage-a")
    ap.add_argument("--out", required=True, help="JSONL output file (appended to)")
    ap.add_argument("--interval", type=float, default=5.0,
                    help="Sampling interval in minutes (default 5; 5 min is the ceiling)")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="One round, then exit")
    mode.add_argument("--daemon", action="store_true",
                      help="Loop forever, one round per interval")
    args = ap.parse_args(argv)

    if args.interval < 1:
        ap.error("--interval below 1 minute is not allowed (monitoring, not load)")

    stop_requested = {"flag": False}

    def on_signal(signum, _frame):
        stop_requested["flag"] = True

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    try:
        fh = open(args.out, "a", encoding="utf-8")
    except OSError as e:
        print("cannot open output file %s: %s" % (args.out, e), file=sys.stderr)
        return 1

    with fh:
        write_record(fh, {
            "type": "probe_start",
            "ts": iso_now(),
            "vantage": args.vantage,
            "interval_min": args.interval,
            "endpoints": [name for name, _ in ENDPOINTS],
        })
        try:
            if args.once:
                run_round(fh, args.vantage)
            else:
                interval_s = args.interval * 60.0
                while not stop_requested["flag"]:
                    run_round(fh, args.vantage)
                    deadline = next_boundary(interval_s)
                    while not stop_requested["flag"] and time.time() < deadline:
                        time.sleep(min(1.0, deadline - time.time()))
        finally:
            write_record(fh, {
                "type": "probe_stop",
                "ts": iso_now(),
                "vantage": args.vantage,
                "reason": "shutdown" if stop_requested["flag"] else "done",
            })
    return 0


if __name__ == "__main__":
    sys.exit(main())
