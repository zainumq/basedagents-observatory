# Verification log

## Unit tests — 15 Nov 2026 ~06:22 WIB

```bash
cd ~/workspace/basedagents-observatory
python3 -m unittest test_analyzer.py
```

Result: **13 tests, OK** (availability math, p50/p95, incident
detection, vantage agreement, probe-gap vs target-downtime
distinction, determinism, multi-file load).

One fix during testing: `fmt_ts` in `analyzer.py` dropped the
`.000` milliseconds when they were zero, so merged-incident and
gap-window timestamps did not match the probe's format. Fixed to
always emit milliseconds; tests green after the fix.

## Live smoke test — 15 Nov 2026 06:23–06:25 WIB (2026-11-14T23:23–23:25Z)

Three `--once` rounds, ~60 s apart (12 requests total — a handful,
well under the 5-minute ceiling). Command:

```bash
cd ~/workspace/basedagents-observatory
for i in 1 2 3; do
  python3 probe.py --once --vantage smoke --out smoke/samples.jsonl
  [ $i -lt 3 ] && sleep 60
done
```

Output:

```text
--- round 1 23:23:10 ---
2026-11-14T23:23:13.356921Z smoke round: 4/4 ok, max latency 1007.0 ms
--- round 2 23:24:13 ---
2026-11-14T23:24:16.281216Z smoke round: 4/4 ok, max latency 906.7 ms
--- round 3 23:25:16 ---
2026-11-14T23:25:18.728526Z smoke round: 4/4 ok, max latency 676.3 ms
```

(The shell loop's exit code was 1 because of the trailing
`[ $i -lt 3 ]` test on the last iteration — a shell artifact, not a
probe failure. All three rounds completed 4/4 ok.)

### Findings — all four endpoints are public (no auth), HTTP 200

| Endpoint | Status | Latency (3 rounds) | Cache headers seen |
|---|---|---|---|
| `GET /v1/status` | 200 | 954 / 907 / 676 ms | `cache-control: public, max-age=0, must-revalidate`, `etag` |
| `GET /v1/tasks?status=open` | 200 | 701 / 642 / 537 ms | same as above, `etag` |
| `GET /v1/tasks/settled` | 200 | 473 / 660 / 591 ms | `cache-control: public, max-age=60`, `etag` |
| `GET /v1/tasks/task_LHJPMel3PnqH4aqCwza2c` | 200 | 1007 / 594 / 520 ms | same as `status`, `etag` |

Notably, **`/v1/tasks/settled` is public** (previously unverified):
it answers 200 with `cache-control: public, max-age=60`. No `age` or
`x-cache` header was observed on any endpoint in this run; the probe
records them whenever present.

Samples: `smoke/samples.jsonl` (12 samples + 6 lifecycle records).
These are smoke data, NOT pilot data.

### Analyzer regeneration — same session

```bash
python3 analyzer.py smoke/samples.jsonl --json smoke/report.json --interval-min 1
```

Result: report regenerated from the raw dataset — 12 total requests,
100.00% availability on all four endpoints, p50/p95 computed,
0 incidents, coverage 100.00% (0/12 missing). JSON report saved to
`smoke/report.json`.

One fix from this run: the first analyzer version listed the 60 s
gaps between `--once` rounds as "known downtime windows". In `--once`
mode the probe stops after every round by design, so a ~1-interval
gap is a normal scheduled gap, not downtime. The analyzer now only
flags stop→start gaps longer than 1.5× the sampling interval.
After the fix the smoke report correctly shows 0 known downtime
windows, and the unit tests (including the 50-minute genuine
downtime window case) still pass 13/13.
