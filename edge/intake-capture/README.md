# Edge intake capture

A Cloudflare Worker in front of `POST /webhook` that makes an intake outage
cost zero submissions.

## Why

`api/routes/webhook.py` states the contract: a 200 means *we have durably taken
responsibility for this submission*, and the plugin stops retrying on the
strength of it. The plugin's whole retry budget is ~17 minutes (10 attempts,
`1000ms << attempt`), so an outage longer than that loses everything even when
the origin is answering honestly — the 2026-08-18 window was 87 minutes.

`utils/webhook_spool.py` already covers the case where *Redis* refuses the
enqueue. It cannot cover the acceptor crashing, a bad deploy, or the box being
gone, because it runs inside the process that is down. This Worker moves the
durable step to the edge, where it survives all three.

It runs at Cloudflare, which already terminates TLS for 100% of this traffic —
so it adds no new failure domain, and needs no plugin release.

## What it does

| Origin result | Worker does | Client sees |
|---|---|---|
| 2xx | ledger datapoint | the origin's response |
| 400 / 401 / 403 | ledger datapoint, no capture | the origin's response |
| 5xx, 413, timeout, network error | **raw body → R2** | 200 if stored, **503** if not |

The 503 is the important half. Never return 2xx for a submission that is not
durably stored somewhere — that is the entire lesson of 2026-08-18.

Replayed by `scripts/drain_r2_spool.py` (every 5 min via
`droptracker-r2-drain.timer`), which re-POSTs the raw body byte-for-byte and
deletes from R2 only after a 200.

`FORCE_SPOOL_SAMPLE` (default 0.001) also captures ~370 successful requests a
day. Those replay as no-ops thanks to GUID dedup, and they are what stops the
capture-and-drain path from rotting between incidents.

## Prerequisites

Replay safety rests on `data/submissions/common.ensure_can_create` being
unbounded in time and blind to transport. **Do not deploy this against an origin
where `tests/unit/test_replay_window_fidelity.py::TestGuidDedupIsTransportBlind`
fails** — that filter is what turned the last outage replay into 35,619
duplicate drops.

```bash
./venv/bin/python -m pytest tests/unit/test_replay_window_fidelity.py -q
```

## Setup

### 1. Origin hostname (must be done before the Worker route)

The Worker cannot set a `Host` header, so fetching `api.droptracker.io` would
re-enter its own route. It fetches `api-origin.droptracker.io` instead.

- Cloudflare DNS: `api-origin` → the prod origin IP, **proxied (orange)**, and
  do **not** attach a Worker route to it.
- nginx: add it to the API block's `server_name` and set the body cap. See
  `deploy/nginx/api-droptracker-io.conf`.

```bash
sudo cp /etc/nginx/sites-available/default /etc/nginx/sites-available/default.bak-preedge
sudo sed -i 's/^    server_name api\.droptracker\.io;$/    server_name api.droptracker.io api-origin.droptracker.io;\n    client_max_body_size 16M;/' /etc/nginx/sites-available/default
sudo nginx -t && sudo systemctl reload nginx
```

Verify before going further — this must return the same JSON as the live host:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1/webhook -H 'Host: api-origin.droptracker.io'
```

### 2. R2 bucket and Analytics Engine

```bash
npx wrangler r2 bucket create droptracker-intake-spool
```

Analytics Engine needs no creation step; the dataset appears on first write.

### 3. Deploy the Worker

```bash
cd /store/droptracker/disc/edge/intake-capture
npm install
npx wrangler deploy
```

`wrangler.toml` already binds `SPOOL` (R2) and `LEDGER` (Analytics Engine) and
declares the two routes. Deploying with the routes in place puts it live
immediately — to stage first, comment out the `[[routes]]` blocks and test
against the `workers.dev` URL.

### 4. Drain credentials and timer

Create an R2 API token scoped to **this bucket only** — it holds unprocessed
player submissions — and fill in `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`,
`R2_SECRET_ACCESS_KEY` in `.env`. Then:

```bash
sudo cp deploy/systemd/droptracker-r2-drain.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now droptracker-r2-drain.timer
```

## Verifying

Against the `workers.dev` URL, before any route is attached:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' -X POST https://<worker>.workers.dev/webhook \
  -F payload_json='{"embeds":[{"fields":[{"name":"type","value":"drop"}]}]}'
```

- Happy path → the origin's status, and **no** R2 object.
- Point `ORIGIN_HOST` at a black-hole name → **200**, and an R2 object appears.
- Unbind `SPOOL` → **503**, not 200. Test this one explicitly; it is the invariant.
- Malformed body → the origin's 400 passes through, no capture.

Drain, twice — the second pass must write nothing, which is GUID dedup holding:

```bash
./venv/bin/python -m scripts.drain_r2_spool            # dry run
./venv/bin/python -m scripts.drain_r2_spool --apply
```

## Operating it

```bash
npx wrangler tail                                       # live Worker logs
./venv/bin/python -m scripts.drain_r2_spool             # what is waiting
./venv/bin/python -m scripts.drain_r2_spool --source dead   # Redis dead letters
```

`scripts/health_watch.py` (every 2 min) alerts on `webhook:dead` above 25 and on
any file in the local disk spool. The R2 spool is surfaced by the drain timer's
own journal rather than a separate probe, since health_watch has no R2
credentials.

Rollback is deleting the Worker route in the Cloudflare dashboard. Traffic
returns to the plain proxy path immediately; no deploy, no restart.

## What this does not cover

- **The `useApi=false` cohort — 28% of submissions.** Those POST straight to
  Discord and never reach this host. Recovery stays
  `scripts/replay_webhook_window.py`, which reads them back out of channel
  history.
- **A client↔Cloudflare partition.** Only durable client-side queueing fixes
  that. The plugin persists 50 entries but `loadSubmissions()` never
  re-dispatches them, and screenshots are `transient` so they do not survive a
  restart at all.
- **Redis losing already-queued entries.** Those were legitimately 200'd, so
  nothing at the edge can help — see the AOF note in the deploy docs.
