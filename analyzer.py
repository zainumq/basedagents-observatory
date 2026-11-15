#!/usr/bin/env python3
"""Analyzer for BasedAgents observatory datasets (probe.py JSONL output).

Reads one or more JSONL files (one per vantage in the pilot), merges them,
and produces a deterministic availability + latency report with incident
detection.

Key distinctions (the whole point of the schema):
  * Target downtime: samples with ok=false and probe_status="ok" — the
    probe ran and the target failed to answer correctly.
  * Collection gaps: samples with probe_status="probe_error" (our side
    failed) plus rounds with no record at all (probe down). These are
    EXCLUDED from availability math and reported separately. A gap in
    OUR collection is never counted as target downtime.

Availability per endpoint = ok_samples / valid_samples, where
valid_samples = all samples for that endpoint minus probe_error samples.

Incidents: 2+ consecutive failed samples (ok=false, probe_status="ok")
on the same endpoint from the same vantage form an incident window.
Windows from different vantages for the same endpoint that overlap in
time are merged, recording vantage_agreement ("both" when 2+ vantages
saw it, otherwise "one") and which vantages. A single failed sample
is not an incident.

Latency percentiles use linear interpolation on the sorted ok-sample
latencies (same convention as numpy's default percentile).

Output: human-readable summary on stdout; JSON report with --json
(a file path, or "-" for stdout). The JSON is deterministic: same
dataset in, same report out (sorted keys, fixed rounding).

Usage:
  analyzer.py samples.jsonl [--json report.json] [--interval-min 5]
  analyzer.py vantage-a.jsonl vantage-b.jsonl --json report.json
"""

import argparse
import json
import math
import sys
from datetime import datetime, timezone


def parse_ts(ts):
    """Parse an ISO 8601 UTC timestamp (with trailing Z) to epoch seconds."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def fmt_ts(epoch):
    """Format epoch seconds as ISO 8601 UTC with milliseconds, matching
    the probe's timestamp format."""
    dt = datetime.fromtimestamp(epoch, timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + ("%03dZ" % (dt.microsecond // 1000))


def percentile(sorted_values, q):
    """Linear-interpolation percentile (q in 0..100) of a sorted list."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = (q / 100.0) * (len(sorted_values) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return float(sorted_values[int(rank)])
    frac = rank - lo
    return float(sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac)


def r2(x):
    return None if x is None else round(float(x), 2)


def load_records(paths):
    records = []
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    return records


def estimate_interval_s(samples):
    """Median gap (seconds) between consecutive samples of the same
    endpoint+vantage, rounded to the nearest minute. Fallback: 300s."""
    gaps = []
    by_key = {}
    for s in samples:
        by_key.setdefault((s["endpoint"], s["vantage"]), []).append(parse_ts(s["ts"]))
    for ts_list in by_key.values():
        ts_list.sort()
        for a, b in zip(ts_list, ts_list[1:]):
            if b > a:
                gaps.append(b - a)
    if not gaps:
        return 300.0
    gaps.sort()
    med = gaps[len(gaps) // 2] if len(gaps) % 2 == 1 else (gaps[len(gaps) // 2 - 1] + gaps[len(gaps) // 2]) / 2.0
    return max(60.0, round(med / 60.0) * 60.0)


def find_incidents(samples):
    """Per (endpoint, vantage): runs of 2+ consecutive failed samples.

    A failed sample is ok=false with probe_status="ok". A probe_error
    sample breaks a run (target state unknown through a collection gap),
    as does an ok sample.
    Returns a list of per-vantage incident dicts (unmerged).
    """
    incidents = []
    by_key = {}
    for s in samples:
        by_key.setdefault((s["endpoint"], s["vantage"]), []).append(s)
    for (endpoint, vantage), recs in sorted(by_key.items()):
        recs.sort(key=lambda r: parse_ts(r["ts"]))
        run = []
        for rec in recs:
            failed = (not rec["ok"]) and rec.get("probe_status", "ok") == "ok"
            if failed:
                run.append(rec)
            else:
                if len(run) >= 2:
                    incidents.append(_incident_from_run(endpoint, vantage, run))
                run = []
        if len(run) >= 2:
            incidents.append(_incident_from_run(endpoint, vantage, run))
    return incidents


def _incident_from_run(endpoint, vantage, run):
    statuses = sorted({r["http_status"] for r in run if r.get("http_status") is not None})
    errors = sorted({r["error"] for r in run if r.get("error")})
    return {
        "endpoint": endpoint,
        "start_ts": run[0]["ts"],
        "end_ts": run[-1]["ts"],
        "start_epoch": parse_ts(run[0]["ts"]),
        "end_epoch": parse_ts(run[-1]["ts"]),
        "vantages": [vantage],
        "failed_samples": len(run),
        "statuses": statuses,
        "errors": errors,
    }


def merge_incidents(incidents, interval_s):
    """Merge per-vantage incidents for the same endpoint whose windows
    overlap (allowing a one-interval touch). Records vantage agreement."""
    merged = []
    by_endpoint = {}
    for inc in incidents:
        by_endpoint.setdefault(inc["endpoint"], []).append(inc)
    for endpoint, incs in sorted(by_endpoint.items()):
        incs.sort(key=lambda i: i["start_epoch"])
        cur = None
        for inc in incs:
            if cur is None:
                cur = dict(inc)
                continue
            if inc["start_epoch"] <= cur["end_epoch"] + interval_s:
                # Overlap/touch: extend the window and merge evidence.
                cur["end_epoch"] = max(cur["end_epoch"], inc["end_epoch"])
                cur["end_ts"] = fmt_ts(cur["end_epoch"])
                cur["vantages"] = sorted(set(cur["vantages"]) | set(inc["vantages"]))
                cur["failed_samples"] += inc["failed_samples"]
                cur["statuses"] = sorted(set(cur["statuses"]) | set(inc["statuses"]))
                cur["errors"] = sorted(set(cur["errors"]) | set(inc["errors"]))
            else:
                merged.append(cur)
                cur = dict(inc)
        if cur is not None:
            merged.append(cur)
    out = []
    for inc in merged:
        out.append({
            "endpoint": inc["endpoint"],
            "start_ts": inc["start_ts"],
            "end_ts": inc["end_ts"],
            "vantages": inc["vantages"],
            "vantage_agreement": "both" if len(inc["vantages"]) >= 2 else "one",
            "failed_samples": inc["failed_samples"],
            "statuses": inc["statuses"],
            "errors": inc["errors"],
        })
    out.sort(key=lambda i: (i["start_ts"], i["endpoint"]))
    return out


def collection_gaps(records, interval_s):
    """Per-vantage collection gaps (probe downtime), from lifecycle
    records and missing expected samples.

    Returns {vantage: {"known_downtime_windows": [...],
                       "missing_samples": int,
                       "expected_samples": int,
                       "coverage_pct": float,
                       "largest_gap_s": float}}
    """
    samples = [r for r in records if r.get("type", "sample") == "sample"]
    lifecycle = [r for r in records if r.get("type") in ("probe_start", "probe_stop")]
    result = {}
    vantages = sorted({r["vantage"] for r in records if "vantage" in r})
    endpoints = sorted({s["endpoint"] for s in samples})
    for vantage in vantages:
        v_samples = [s for s in samples if s["vantage"] == vantage]
        v_life = sorted(
            [r for r in lifecycle if r["vantage"] == vantage],
            key=lambda r: parse_ts(r["ts"]),
        )
        # Known downtime windows: probe_stop -> next probe_start, but only
        # when the gap exceeds 1.5x the sampling interval. In --once mode
        # (systemd/cron) the probe stops after every round by design, so
        # the normal ~1-interval gap between scheduled rounds is NOT
        # downtime — only a longer gap (a missed round or more) is.
        windows = []
        last_stop = None
        for rec in v_life:
            if rec["type"] == "probe_stop":
                last_stop = parse_ts(rec["ts"])
            elif rec["type"] == "probe_start" and last_stop is not None:
                start = parse_ts(rec["ts"])
                if start - last_stop > 1.5 * interval_s:
                    windows.append({
                        "start_ts": fmt_ts(last_stop),
                        "end_ts": fmt_ts(start),
                        "duration_s": round(start - last_stop, 1),
                    })
                last_stop = None
        # Missing samples: expected rounds across the vantage's window.
        entry = {
            "known_downtime_windows": windows,
            "missing_samples": 0,
            "expected_samples": 0,
            "coverage_pct": None,
            "largest_gap_s": 0.0,
        }
        if v_samples and endpoints:
            ts_list = sorted(parse_ts(s["ts"]) for s in v_samples)
            first, last = ts_list[0], ts_list[-1]
            rounds = int(round((last - first) / interval_s)) + 1 if last >= first else 1
            expected = rounds * len(endpoints)
            entry["expected_samples"] = expected
            entry["missing_samples"] = max(0, expected - len(v_samples))
            entry["coverage_pct"] = r2(100.0 * len(v_samples) / expected) if expected else None
            # Largest gap between consecutive rounds (any endpoint).
            round_ts = sorted({parse_ts(s["ts"]) for s in v_samples})
            gaps = [b - a for a, b in zip(round_ts, round_ts[1:])]
            entry["largest_gap_s"] = round(max(gaps), 1) if gaps else 0.0
        result[vantage] = entry
    return result


def analyze(records, interval_s=None):
    samples = [r for r in records if r.get("type", "sample") == "sample"]
    samples.sort(key=lambda r: (parse_ts(r["ts"]), r["endpoint"], r["vantage"]))
    if interval_s is None:
        interval_s = estimate_interval_s(samples)

    total_requests = len(samples)  # one scheduled request attempt per sample
    probe_errors = sum(1 for s in samples if s.get("probe_status") == "probe_error")

    endpoints = {}
    for name in sorted({s["endpoint"] for s in samples}):
        ep = [s for s in samples if s["endpoint"] == name]
        ep_errors = [s for s in ep if s.get("probe_status") == "probe_error"]
        valid = [s for s in ep if s.get("probe_status", "ok") != "probe_error"]
        ok_samples = [s for s in valid if s["ok"]]
        lat = sorted(s["latency_ms"] for s in ok_samples)
        endpoints[name] = {
            "samples": len(ep),
            "probe_error_samples": len(ep_errors),
            "valid_samples": len(valid),
            "ok_samples": len(ok_samples),
            "availability_pct": r2(100.0 * len(ok_samples) / len(valid)) if valid else None,
            "p50_latency_ms": r2(percentile(lat, 50)),
            "p95_latency_ms": r2(percentile(lat, 95)),
        }

    incidents = merge_incidents(find_incidents(samples), interval_s)
    gaps = collection_gaps(records, interval_s)

    if samples:
        pilot = {
            "first_sample_ts": samples[0]["ts"],
            "last_sample_ts": samples[-1]["ts"],
            "duration_hours": r2((parse_ts(samples[-1]["ts"]) - parse_ts(samples[0]["ts"])) / 3600.0),
        }
    else:
        pilot = {"first_sample_ts": None, "last_sample_ts": None, "duration_hours": 0.0}

    return {
        "pilot_window": pilot,
        "interval_s_used": interval_s,
        "total_requests": total_requests,
        "total_samples": len(samples),
        "probe_error_samples": probe_errors,
        "vantages": sorted({s["vantage"] for s in samples}),
        "endpoints": endpoints,
        "incidents": incidents,
        "collection_gaps": gaps,
    }


def human_summary(report):
    lines = []
    pw = report["pilot_window"]
    lines.append("BasedAgents API observatory report")
    lines.append("  window: %s .. %s (%.2f h)" % (
        pw["first_sample_ts"], pw["last_sample_ts"], pw["duration_hours"]))
    lines.append("  vantages: %s" % (", ".join(report["vantages"]) or "none"))
    lines.append("  total requests: %d (%d samples, %d probe errors)" % (
        report["total_requests"], report["total_samples"], report["probe_error_samples"]))
    lines.append("")
    lines.append("  endpoints:")
    for name, ep in sorted(report["endpoints"].items()):
        avail = ("%.2f%%" % ep["availability_pct"]) if ep["availability_pct"] is not None else "n/a"
        p50 = ("%.1f ms" % ep["p50_latency_ms"]) if ep["p50_latency_ms"] is not None else "n/a"
        p95 = ("%.1f ms" % ep["p95_latency_ms"]) if ep["p95_latency_ms"] is not None else "n/a"
        lines.append("    %-13s avail %s  p50 %s  p95 %s  (%d ok / %d valid, %d probe errors)" % (
            name, avail, p50, p95, ep["ok_samples"], ep["valid_samples"], ep["probe_error_samples"]))
    lines.append("")
    lines.append("  incidents: %d" % len(report["incidents"]))
    for inc in report["incidents"]:
        lines.append("    %s  %s .. %s  agreement=%s (%s)  %d failed samples  statuses=%s  errors=%s" % (
            inc["endpoint"], inc["start_ts"], inc["end_ts"], inc["vantage_agreement"],
            ",".join(inc["vantages"]), inc["failed_samples"], inc["statuses"], inc["errors"]))
    lines.append("")
    lines.append("  collection gaps (probe downtime, NOT target downtime):")
    for vantage, g in sorted(report["collection_gaps"].items()):
        cov = ("%.2f%%" % g["coverage_pct"]) if g["coverage_pct"] is not None else "n/a"
        lines.append("    %s: coverage %s, missing %d/%d expected samples, largest gap %.0f s, %d known downtime windows" % (
            vantage, cov, g["missing_samples"], g["expected_samples"],
            g["largest_gap_s"], len(g["known_downtime_windows"])))
        for w in g["known_downtime_windows"]:
            lines.append("      down %s .. %s (%.0f s)" % (w["start_ts"], w["end_ts"], w["duration_s"]))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Analyze BasedAgents observatory JSONL dataset(s).")
    ap.add_argument("inputs", nargs="+", help="JSONL dataset file(s), one per vantage")
    ap.add_argument("--json", dest="json_out", default=None,
                    help="Write the JSON report to this file ('-' = stdout)")
    ap.add_argument("--interval-min", type=float, default=None,
                    help="Sampling interval in minutes (default: estimate from data)")
    args = ap.parse_args(argv)

    records = load_records(args.inputs)
    interval_s = args.interval_min * 60.0 if args.interval_min else None
    report = analyze(records, interval_s)

    if args.json_out:
        text = json.dumps(report, indent=2, sort_keys=True)
        if args.json_out == "-":
            print(text)
        else:
            with open(args.json_out, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
            print("wrote JSON report to %s" % args.json_out)
    print(human_summary(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
