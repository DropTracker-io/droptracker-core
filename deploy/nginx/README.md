# nginx — DropTracker front-end (blue-green)

The Next.js site (`droptracker-node-blue.service` = blue :31380 +
`droptracker-node-green.service` = green :31381) is served zero-downtime via an
nginx upstream. (`droptracker-node.service` is the deploy *trigger* — a oneshot
that runs `scripts/deploy.sh` — not a server.) These are the nginx-side artifacts
(the rest of the site config lives in `/etc/nginx/sites-available/default`, which
is not vendored here).

## Files

- `droptracker-node-upstream.conf` → install to
  `/etc/nginx/conf.d/droptracker-node-upstream.conf`. Defines
  `upstream droptracker_node { ... }`. **`scripts/deploy.sh` (web repo) rewrites
  this file on every deploy** to flip which colour is primary, so the vendored
  copy here is only a bootstrap template — the live active/backup ordering
  drifts from it and that's expected.

## sites-available/default — required edits (one-time)

In the `www.droptracker.io` server block, both the `location /` and
`location /api/stream` blocks must proxy to the **upstream**, not a fixed port:

```nginx
proxy_pass http://droptracker_node;                       # was http://127.0.0.1:31380
proxy_next_upstream error timeout http_502 http_503 http_504;
```

`proxy_next_upstream` lets nginx transparently retry the standby if the active
instance refuses a connection (runtime crash failover). `conf.d/*.conf` is
`include`d before `sites-enabled/*` in the stock Debian `nginx.conf`, so the
`upstream` block resolves.

## How a deploy uses this

`scripts/deploy.sh` builds the idle colour, restarts its unit, polls
`/api/health` on its port, then rewrites this conf so the fresh colour is primary
and runs `nginx -s reload` (graceful — the listen socket is never dropped and the
new build is already warm). Rollback = re-run `scripts/deploy.sh` (flips back), or
swap the two `server` lines here and `sudo nginx -s reload`.
