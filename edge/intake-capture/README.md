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

Verify before going further. An empty POST returns **400** — that is the app
rejecting a body with no `payload_json`, and it is the correct answer. What
matters is that it is the *app's* JSON 400 and not nginx's HTML one, and that it
matches the live host byte for byte:

```bash
curl -s -X POST http://127.0.0.1/webhook -H 'Host: api-origin.droptracker.io'
curl -s -X POST http://127.0.0.1/webhook -H 'Host: api.droptracker.io'
```

Both must print `{"error":"Expected multipart/form-data"}`. For a positive
check, a well-formed body should come back `{"message":"Queued"}` — note this
really does enqueue, so use a guid you can recognise and expect it to be
rejected downstream:

```bash
curl -s -X POST http://127.0.0.1/webhook -H 'Host: api-origin.droptracker.io' \
  -F 'payload_json={"embeds":[{"title":"edge probe","fields":[{"name":"type","value":"drop"},{"name":"guid","value":"edge-probe"}]}]}'
```

A probe like that has no player, so it dead-letters and leaves its entry in
`webhook:dead` plus any attachment in `WEBHOOK_TEMP_DIR`. Clean both up
afterwards rather than leaving them to look like real lost submissions.

### 2. wrangler and credentials

`node` here is nvm-managed, so source it first.

```bash
export NVM_DIR="$HOME/.nvm" && . "$NVM_DIR/nvm.sh" && nvm use 20
cd /store/droptracker/disc/edge/intake-capture
npm install
```

**Authenticate with an API token, not `wrangler login`.** The OAuth flow
redirects to `http://localhost:8976/oauth/callback` — localhost of whatever
machine the *browser* is on. On a headless box that is never the machine
running wrangler, so copying the URL to a desktop cannot complete the flow.

Create a token at dash.cloudflare.com → My Profile → API Tokens → Create Token,
starting from the **Edit Cloudflare Workers** template and adding
**Account → Workers R2 Storage → Edit**. It needs, at minimum:

| Scope | Permission |
|---|---|
| Account | Workers Scripts → Edit |
| Account | Workers R2 Storage → Edit |
| Zone (`droptracker.io`) | Workers Routes → Edit |
| Zone (`droptracker.io`) | Zone → Read |

**Set the account id too.** Without it wrangler tries to discover the account
via `GET /client/v4/memberships`, which is a *User*-scoped endpoint an
account-scoped token cannot call — it fails with a misleading
`Authentication failed (status: 400) [code: 9106]`. The account id is not a
secret, and it is the same value as `R2_ACCOUNT_ID` in step 5 (dashboard →
any zone → Overview → Account ID, or the R2 page).

In the shell you deploy from — `read -rs` keeps the token out of shell
history:

```bash
read -rs CLOUDFLARE_API_TOKEN && export CLOUDFLARE_API_TOKEN
export CLOUDFLARE_ACCOUNT_ID='<account id>'
npx wrangler r2 bucket list
```

Use `r2 bucket list` to validate, **not `wrangler whoami`** — whoami also goes
through `/memberships` and will fail for the same reason even when the token is
perfectly good. An empty list (or your existing buckets) means auth works.

Do not put the API token in `.env` — it is a deploy-time credential, unrelated
to the R2 keys in step 5 and far broader than them. If you ever need `whoami`
to work, add **User → Memberships → Read** to the token.

### 3. R2 bucket and Analytics Engine

**Enable R2 first.** It is off until you opt in: dashboard → R2 → Enable /
Purchase R2. Cloudflare asks for a payment method even though the usage here
fits the free tier. Until you do, every R2 call returns
`Please enable R2 through the Cloudflare Dashboard [code: 10042]` — which is a
billing gate, not a token problem.

R2's free tier genuinely covers this: 10 GB storage, 1M Class A ops/month. In
steady state the Worker writes only the 0.1% sample (~370 objects/day, ~11k
Class A/month) and the drain deletes each within five minutes, so stored bytes
sit near zero. An 87-minute outage of the size we have actually seen would
write ~22k objects totalling ~450 MB.

```bash
npx wrangler r2 bucket create droptracker-intake-spool
```

Analytics Engine needs no creation step; the dataset appears on first write.

**Workers Paid is the other gate — but not yet.** The free plan allows 100k
requests/day and `POST /webhook` alone is ~372k/day, so the production deploy
in step 4 needs the $5/mo Workers Paid plan (10M requests included, ~1.2M
overage at $0.30/M ≈ $0.36). Staging costs nothing meaningful, so do the whole
validation pass on the free plan and upgrade only just before attaching the
production routes.

### 4. Deploy the Worker

**Always pass `--env` explicitly.** There are two targets and they differ only
by that flag:

| Command | Worker | Routes |
|---|---|---|
| `wrangler deploy --env=""` | `droptracker-intake-capture-staging` | none, workers.dev only |
| `wrangler deploy --env production` | `droptracker-intake-capture` | **live on api.droptracker.io** |

Routes are declared only under `[env.production]`. That is deliberate: Wrangler
*inherits* a top-level `routes` block into every named environment (vars and
bindings are not inherited, routes are), so a top-level route block means every
environment you deploy grabs production traffic. On 2026-08-21 exactly that put
an unvalidated worker in front of live intake. Two names and one route block
removes the question.

Staging first — this touches nothing:

```bash
npx wrangler deploy --env=""
```

Run the checks below against the `workers.dev` URL it prints. Only when they
pass:

```bash
npx wrangler deploy --env production
```

To hand the routes back to the plain proxy at any point, delete the production
worker. Traffic falls straight through to the origin, which is how it ran
before any of this existed:

```bash
npx wrangler delete droptracker-intake-capture
```

### 5. Drain credentials and timer

These are **not** the deploy token from step 3. The drain script talks to R2
over the S3-compatible API, which needs its own access key pair: dash →
R2 → Manage R2 API Tokens → Create, scoped to **this bucket only** and
**Object Read & Write** (it holds unprocessed player submissions, so do not
reuse a wider token). That page also shows the account id.

Fill `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` in `.env`,
confirm the drain can see the bucket, then install the timer:

```bash
./venv/bin/python -m scripts.drain_r2_spool
```

It should print `nothing to drain` rather than a credentials error. Then:

```bash
sudo cp deploy/systemd/droptracker-r2-drain.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now droptracker-r2-drain.timer
```

## Verifying

**A browser will not tell you much.** The Worker only captures POSTs; anything
else it proxies straight to the origin. So on the `workers.dev` URL expect
`GET /` → 404 and `GET /webhook` → 405, both coming from the intake API. The
useful browser check is `GET /ping`, which should return 200 — that proves the
Worker reached `ORIGIN_HOST`. (Before the route-re-entry fix, a GET returned
Cloudflare's "There is nothing here yet" placeholder, because the Worker was
re-fetching its own URL.)

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

Rollback is `npx wrangler delete droptracker-intake-capture`, or removing the
route in the dashboard. Traffic returns to the plain proxy path immediately; no
deploy, no restart, and the origin never knew the difference.

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
