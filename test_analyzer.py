#!/usr/bin/env python3
"""Unit tests for analyzer.py, using a synthetic dataset.

Covers: availability math (probe_error excluded), p50/p95, incident
detection (consecutive failures), vantage agreement (merge), and the
probe-gap vs target-downtime distinction.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import analyzer


def ts(minute):
    """ISO timestamp, `minute` minutes after 2026-11-15T00:00:00Z."""
    return "2026-11-15T%02d:%02d:00.000Z" % (minute // 60, minute % 60)


def sample(minute, endpoint, vantage, ok=True, latency=100.0, status=200,
           probe_status="ok", error=None):
    return {
        "type": "sample",
        "ts": ts(minute),
        "endpoint": endpoint,
        "vantage": vantage,
        "latency_ms": latency,
        "http_status": status if probe_status == "ok" else None,
        "ok": ok,
        "cache": {},
        "error": error,
        "probe_status": probe_status,
    }


def lifecycle(kind, minute, vantage):
    rec = {"type": kind, "ts": ts(minute), "vantage": vantage}
    if kind == "probe_start":
        rec["interval_min"] = 5
        rec["endpoints"] = ["status"]
    else:
        rec["reason"] = "done"
    return rec


class TestAvailability(unittest.TestCase):
    def test_availability_excludes_probe_errors(self):
        # 10 samples: 8 ok, 1 failed (target), 1 probe_error.
        # Availability must be 8/9 = 88.89%, NOT 8/10.
        recs = [sample(m * 5, "status", "vantage-a", ok=True, latency=100.0)
                for m in range(8)]
        recs.append(sample(40, "status", "vantage-a", ok=False, status=500,
                           error="http 500"))
        recs.append(sample(45, "status", "vantage-a", ok=False,
                           probe_status="probe_error", error="dns: boom"))
        rep = analyzer.analyze(recs, interval_s=300.0)
        ep = rep["endpoints"]["status"]
        self.assertEqual(ep["samples"], 10)
        self.assertEqual(ep["probe_error_samples"], 1)
        self.assertEqual(ep["valid_samples"], 9)
        self.assertEqual(ep["ok_samples"], 8)
        self.assertAlmostEqual(ep["availability_pct"], 88.89, places=2)
        self.assertEqual(rep["probe_error_samples"], 1)

    def test_availability_all_probe_errors_is_none(self):
        recs = [sample(m * 5, "status", "vantage-a", ok=False,
                       probe_status="probe_error", error="dns: boom")
                for m in range(3)]
        rep = analyzer.analyze(recs, interval_s=300.0)
        self.assertIsNone(rep["endpoints"]["status"]["availability_pct"])


class TestLatency(unittest.TestCase):
    def test_p50_p95_ok_samples_only(self):
        # Latencies 10..100 step 10 (10 ok samples); one failed sample
        # with latency 9999 must NOT enter the percentile math.
        recs = [sample(m * 5, "status", "vantage-a", ok=True,
                       latency=float(10 * (m + 1))) for m in range(10)]
        recs.append(sample(50, "status", "vantage-a", ok=False, status=500,
                           latency=9999.0, error="http 500"))
        rep = analyzer.analyze(recs, interval_s=300.0)
        ep = rep["endpoints"]["status"]
        # Linear interpolation: p50 of 10..100 = 55.0; p95 = 95.5.
        self.assertAlmostEqual(ep["p50_latency_ms"], 55.0, places=2)
        self.assertAlmostEqual(ep["p95_latency_ms"], 95.5, places=2)


class TestIncidents(unittest.TestCase):
    def test_two_consecutive_failures_is_incident(self):
        recs = [
            sample(0, "status", "vantage-a", ok=True),
            sample(5, "status", "vantage-a", ok=False, status=500, error="http 500"),
            sample(10, "status", "vantage-a", ok=False, status=502, error="http 502"),
            sample(15, "status", "vantage-a", ok=True),
        ]
        rep = analyzer.analyze(recs, interval_s=300.0)
        self.assertEqual(len(rep["incidents"]), 1)
        inc = rep["incidents"][0]
        self.assertEqual(inc["endpoint"], "status")
        self.assertEqual(inc["start_ts"], ts(5))
        self.assertEqual(inc["end_ts"], ts(10))
        self.assertEqual(inc["vantage_agreement"], "one")
        self.assertEqual(inc["vantages"], ["vantage-a"])
        self.assertEqual(inc["failed_samples"], 2)
        self.assertEqual(inc["statuses"], [500, 502])
        self.assertEqual(inc["errors"], ["http 500", "http 502"])

    def test_single_failure_is_not_incident(self):
        recs = [
            sample(0, "status", "vantage-a", ok=True),
            sample(5, "status", "vantage-a", ok=False, status=500, error="http 500"),
            sample(10, "status", "vantage-a", ok=True),
        ]
        rep = analyzer.analyze(recs, interval_s=300.0)
        self.assertEqual(rep["incidents"], [])

    def test_probe_error_breaks_incident_run(self):
        # fail, probe_error, fail -> no 2-consecutive run (state unknown
        # through the gap), so no incident.
        recs = [
            sample(0, "status", "vantage-a", ok=False, status=500, error="http 500"),
            sample(5, "status", "vantage-a", ok=False,
                   probe_status="probe_error", error="dns: boom"),
            sample(10, "status", "vantage-a", ok=False, status=500, error="http 500"),
        ]
        rep = analyzer.analyze(recs, interval_s=300.0)
        self.assertEqual(rep["incidents"], [])

    def test_vantage_agreement_both(self):
        # Both vantages fail over overlapping windows -> one merged
        # incident with agreement "both".
        recs = []
        for v in ("vantage-a", "vantage-b"):
            recs.append(sample(0, "status", v, ok=True))
            recs.append(sample(5, "status", v, ok=False, status=500, error="http 500"))
            recs.append(sample(10, "status", v, ok=False, status=500, error="http 500"))
            recs.append(sample(15, "status", v, ok=True))
        rep = analyzer.analyze(recs, interval_s=300.0)
        self.assertEqual(len(rep["incidents"]), 1)
        inc = rep["incidents"][0]
        self.assertEqual(inc["vantage_agreement"], "both")
        self.assertEqual(inc["vantages"], ["vantage-a", "vantage-b"])
        self.assertEqual(inc["failed_samples"], 4)

    def test_non_overlapping_incidents_stay_separate(self):
        recs = []
        # vantage-a fails at 5,10; vantage-b fails at 30,35 (no overlap).
        for m in (0, 5, 10, 15, 20, 25, 30, 35, 40):
            ok_a = m not in (5, 10)
            ok_b = m not in (30, 35)
            recs.append(sample(m, "status", "vantage-a", ok=ok_a,
                               status=200 if ok_a else 500,
                               error=None if ok_a else "http 500"))
            recs.append(sample(m, "status", "vantage-b", ok=ok_b,
                               status=200 if ok_b else 500,
                               error=None if ok_b else "http 500"))
        rep = analyzer.analyze(recs, interval_s=300.0)
        self.assertEqual(len(rep["incidents"]), 2)
        self.assertEqual(rep["incidents"][0]["vantages"], ["vantage-a"])
        self.assertEqual(rep["incidents"][1]["vantages"], ["vantage-b"])


class TestCollectionGaps(unittest.TestCase):
    def test_known_downtime_window(self):
        recs = [
            lifecycle("probe_start", 0, "vantage-a"),
            sample(0, "status", "vantage-a"),
            sample(5, "status", "vantage-a"),
            lifecycle("probe_stop", 10, "vantage-a"),
            # probe down 10 -> 60, then restarts
            lifecycle("probe_start", 60, "vantage-a"),
            sample(60, "status", "vantage-a"),
            sample(65, "status", "vantage-a"),
            lifecycle("probe_stop", 70, "vantage-a"),
        ]
        rep = analyzer.analyze(recs, interval_s=300.0)
        gaps = rep["collection_gaps"]["vantage-a"]
        self.assertEqual(len(gaps["known_downtime_windows"]), 1)
        w = gaps["known_downtime_windows"][0]
        self.assertEqual(w["start_ts"], ts(10))
        self.assertEqual(w["end_ts"], ts(60))
        self.assertEqual(w["duration_s"], 3000.0)
        # 4 samples over a 65-min window at 5-min intervals: 14 expected
        # rounds x 1 endpoint = 14 expected; 4 present -> 10 missing.
        self.assertEqual(gaps["expected_samples"], 14)
        self.assertEqual(gaps["missing_samples"], 10)
        self.assertAlmostEqual(gaps["coverage_pct"], 28.57, places=2)

    def test_probe_gap_not_counted_as_incident_or_downtime(self):
        # All samples ok except a stretch of probe_errors: no incidents,
        # availability stays 100% over valid samples.
        recs = [sample(m * 5, "status", "vantage-a", ok=True) for m in range(4)]
        recs += [sample(m * 5, "status", "vantage-a", ok=False,
                        probe_status="probe_error", error="dns: boom")
                 for m in range(4, 8)]
        recs += [sample(m * 5, "status", "vantage-a", ok=True) for m in range(8, 10)]
        rep = analyzer.analyze(recs, interval_s=300.0)
        self.assertEqual(rep["incidents"], [])
        self.assertAlmostEqual(rep["endpoints"]["status"]["availability_pct"], 100.0)
        self.assertEqual(rep["probe_error_samples"], 4)


class TestTotalsAndWindow(unittest.TestCase):
    def test_total_requests_and_pilot_window(self):
        recs = [
            lifecycle("probe_start", 0, "vantage-a"),
            sample(0, "status", "vantage-a"),
            sample(0, "tasks_open", "vantage-a"),
            sample(5, "status", "vantage-a"),
            sample(5, "tasks_open", "vantage-a"),
            lifecycle("probe_stop", 10, "vantage-a"),
        ]
        rep = analyzer.analyze(recs, interval_s=300.0)
        self.assertEqual(rep["total_requests"], 4)
        self.assertEqual(rep["total_samples"], 4)
        self.assertEqual(rep["pilot_window"]["first_sample_ts"], ts(0))
        self.assertEqual(rep["pilot_window"]["last_sample_ts"], ts(5))
        self.assertAlmostEqual(rep["pilot_window"]["duration_hours"], 5 / 60.0, places=2)

    def test_deterministic_output(self):
        recs = [sample(m * 5, "status", "vantage-a", ok=(m % 3 != 0),
                       status=200 if m % 3 != 0 else 500,
                       error=None if m % 3 != 0 else "http 500")
                for m in range(12)]
        r1 = analyzer.analyze(list(recs), interval_s=300.0)
        r2 = analyzer.analyze(list(reversed(recs)), interval_s=300.0)
        self.assertEqual(json.dumps(r1, sort_keys=True), json.dumps(r2, sort_keys=True))

    def test_load_multiple_files(self):
        recs_a = [sample(0, "status", "vantage-a"), sample(5, "status", "vantage-a")]
        recs_b = [sample(0, "status", "vantage-b"), sample(5, "status", "vantage-b")]
        with tempfile.TemporaryDirectory() as d:
            pa = os.path.join(d, "a.jsonl")
            pb = os.path.join(d, "b.jsonl")
            for path, recs in ((pa, recs_a), (pb, recs_b)):
                with open(path, "w") as fh:
                    for r in recs:
                        fh.write(json.dumps(r) + "\n")
            loaded = analyzer.load_records([pa, pb])
            self.assertEqual(len(loaded), 4)
            rep = analyzer.analyze(loaded, interval_s=300.0)
            self.assertEqual(rep["vantages"], ["vantage-a", "vantage-b"])
            self.assertEqual(rep["total_requests"], 4)


if __name__ == "__main__":
    unittest.main()
