# BasedAgents API Observatory

An independent uptime and latency observatory for the BasedAgents public
API (`https://api.basedagents.ai`). A probe samples four public read-only
endpoints every 5 minutes from each vantage point and records structured
samples; an analyzer turns the raw dataset into availability, latency,
and incident reports.

Built for BasedAgents task `task_LHJPMel3PnqH4aqCwza2c`
("Build an independent uptime and latency observatory for the public
API, with a 48-hour pilot dataset").

## Deploy in under 15 minutes

### Prerequisites

- Python 3.10+ and nothing else. Standard library only — no pip
  packages, no database, no Docker.

### Install

```bash
git clone <this repo> /opt/basedagents-observatory   # or just copy the files
mkdir -p /var/lib/basedagents-observatory
```

No build step, no dependencies to install.

### Configure

Two things per vantage point:

1. **Vantage label** — a short name for where the probe runs, e.g.
   `vantage-a`, `vantage-b`. Pick a label that identifies the location
   (city/provider) without leaking anything personal.
2. **Output path** — where samples go, e.g.
   `/var/lib/basedagents-observatory/samples.jsonl`.

### Run (pick one)

**systemd** (recommended for a server):

```bash
sudo cp scheduler/observatory.service scheduler/observatory.timer /etc/systemd/system/
# edit the --vantage label and --out path in observatory.service
sudo systemctl daemon-reload
sudo systemctl enable --now observatory.timer
systemctl list-timers observatory.timer   # check it fires every 5 min
```

**cron:**

```bash
crontab -e
# paste the line from scheduler/cron.example (edit paths/label first)
```

**tmux** (quick start, survives logout):

```bash
tmux new-session -d -s observatory "python3 /opt/basedagents-observatory/probe.py --daemon --interval 5 --vantage vantage-a --out /var/lib/basedagents-observatory/samples.jsonl"
```

**GitHub Actions** (a second vantage point on a CI runner):

```bash
# Push this repo to GitHub. The workflow is already included:
# .github/workflows/probe.yml runs probe.py --once every 5 minutes and
# commits each round's samples to pilot/github-actions.jsonl in the repo.
# No setup beyond pushing — the schedule runs on the default branch.
# Free for public repos.
```

### Verify it works

```bash
# One round right now (writes 4 samples + start/stop records):
python3 probe.py --once --vantage vantage-a --out /var/lib/basedagents-observatory/samples.jsonl
tail -6 /var/lib/basedagents-observatory/samples.jsonl
```

## What it samples

Four public read-only endpoints, once per round (4 requests per round):

| Short name | Request |
|---|---|
| `status` | `GET /v1/status` |
| `tasks_open` | `GET /v1/tasks?status=open` |
| `tasks_settled` | `GET /v1/tasks/settled` |
| `task_detail` | `GET /v1/tasks/task_LHJPMel3PnqH4aqCwza2c` |

Read-only: GET requests only. No authentication, no POST, no writes
of any kind. The probe identifies itself with the User-Agent
`basedagents-observatory/1.0 (read-only probe)`.

### The 5-minute ceiling

One round per 5 minutes per vantage is the maximum sampling rate.
This is monitoring, not a load test — do not sample faster. At 5-minute
intervals one vantage makes 4 requests/round × 12 rounds/hour =
**48 requests/hour**, i.e. **2,304 requests per vantage over 48 hours**
(4,608 total for two vantages). The analyzer reports the exact total
request count from the dataset, so the real number is always visible.

### No retries

Each sample is a single attempt. A failed attempt IS a sample
(`ok: false`). Retrying would hide target downtime.

## Dataset schema

One JSON object per line (JSONL). Two record types:

**Sample** — one per request attempt:

```json
{"type": "sample", "ts": "2026-11-15T06:30:00.123Z",
 "endpoint": "status", "vantage": "vantage-a",
 "latency_ms": 812.34, "http_status": 200, "ok": true,
 "cache": {"cache-control": "no-cache", "etag": "\"abc\""},
 "error": null, "probe_status": "ok"}
```

| Field | Meaning |
|---|---|
| `ts` | ISO 8601 UTC timestamp of the attempt |
| `endpoint` | Short name (`status`, `tasks_open`, `tasks_settled`, `task_detail`) |
| `vantage` | Vantage label from `--vantage` |
| `latency_ms` | Round-trip time in milliseconds |
| `http_status` | HTTP status code, or `null` if no response arrived |
| `ok` | `true` iff the response was HTTP 2xx |
| `cache` | Cache headers seen (`cache-control`, `etag`, `age`, `x-cache`), only those present |
| `error` | Error string (`http 500`, `timeout`, `connection refused`, `dns: ...`) or `null` |
| `probe_status` | `ok` or `probe_error` (see below) |

**Lifecycle** — the probe logs its own start/stop so downtime gaps are
attributable:

```json
{"type": "probe_start", "ts": "...", "vantage": "vantage-a",
 "interval_min": 5, "endpoints": ["status", "tasks_open", "tasks_settled", "task_detail"]}
{"type": "probe_stop", "ts": "...", "vantage": "vantage-a", "reason": "done"}
```

### Probe gaps vs target downtime — how the schema distinguishes them

This is the core design question, and the dataset answers it three ways:

1. **`probe_status: "probe_error"`** — the probe itself failed before a
   request could be attempted or recorded (e.g. DNS resolution failure
   on our side, local network error). These samples are EXCLUDED from
   availability math and reported separately as collection gaps.
   Timeouts and connection refusals are NOT probe errors: they mean the
   target failed to answer, so they count as failed samples.
2. **Missing rounds** — a round with no record at all means the probe
   was not running (probe downtime), not target downtime. The analyzer
   computes expected vs actual sample counts per vantage and reports
   coverage.
3. **`probe_start` / `probe_stop` records** — a `probe_stop` followed
   later by a `probe_start` is a known probe-downtime window, listed
   explicitly in the report — but only when the gap exceeds 1.5× the
   sampling interval, because in `--once` mode (systemd/cron) the probe
   stops after every round by design, so the normal ~1-interval gap
   between scheduled rounds is expected, not downtime. Any gap inside
   such a window is our gap, never the target's.

## Regenerating the report

```bash
python3 analyzer.py samples.jsonl --json report.json
# two vantages (one file each):
python3 analyzer.py vantage-a.jsonl vantage-b.jsonl --json report.json
```

The report is deterministic: the same dataset always produces the same
report (sorted keys, fixed rounding). The analyzer prints a
human-readable summary and, with `--json`, writes the full JSON report.

Report contents:

- **Pilot window** — first/last sample timestamps, duration in hours.
- **Total request count** — one per sample record (one scheduled
  request attempt per sample). Probe-error samples are counted in the
  total and broken out separately.
- **Per endpoint** — total samples, availability % (ok / valid, where
  valid excludes probe-error samples), p50/p95 latency of ok samples.
- **Incidents** — windows of 2+ consecutive failed samples on the same
  endpoint from the same vantage, merged across vantages where windows
  overlap, with start/end timestamps, vantage agreement (`both` when
  2+ vantages saw it, `one` otherwise), and the statuses/errors seen.
  A single failed sample is not an incident.
- **Collection gaps** — per vantage: known probe-downtime windows
  (from start/stop records), missing-sample counts, coverage %, and the
  largest gap between rounds.

## Tests

```bash
python3 -m unittest test_analyzer.py -v
```

The suite uses a synthetic dataset and covers availability math,
p50/p95, incident detection, vantage agreement, and the probe-gap vs
target-downtime distinction.

## Files

| File | Purpose |
|---|---|
| `probe.py` | The sampler (stdlib only) |
| `analyzer.py` | The report generator (stdlib only) |
| `test_analyzer.py` | Unit tests for the analyzer |
| `scheduler/observatory.service` | systemd service (one round) |
| `scheduler/observatory.timer` | systemd timer (every 5 min) |
| `scheduler/cron.example` | Cron line example |
| `scheduler/tmux.example` | tmux daemon one-liner |
| `.github/workflows/probe.yml` | GitHub Actions workflow (one round every 5 min, commits samples to `pilot/`) |
| `RUNS.md` | Verification log (smoke runs against the live API) |
| `smoke/` | Smoke-test samples (NOT pilot data) |
