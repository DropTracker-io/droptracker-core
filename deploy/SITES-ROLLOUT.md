# Group mini-sites (`*.osrs.site`) — production rollout runbook

Feature branches: `sites-v1` in BOTH repos (disc off `new-api`, web off `main`),
built and verified on the dev box 2026-08-07/08. Everything below is the exact
order for moving it onto the production machine. Nothing here is destructive;
each step has a rollback.

## Pre-flight (verify on dev first)

- [ ] `myclan.osrs.site` renders through Cloudflare (home/records/stats), nav works
- [ ] Downgrade drill: lapse the test sub → site unavailable ≤60s
- [ ] Suspend drill: `group_sites.suspended_at` → branded suspended page
- [ ] Sanitizer suites green: `venv/bin/python -m pytest tests/unit/test_sites_validation.py tests/unit/test_site_sanitizer.py`
- [ ] CSP soak: no real violations in Report-Only (browser console on tenant pages)
- [ ] Owner sign-off on the builder UX (Website tab on dev)

## Rollout order (production)

1. **Merge/pull the branches** on prod (`disc@new-api`, `web@main` or merge
   `sites-v1` in). Backend deps: `venv/bin/pip install nh3 tinycss2`
   (in requirements.txt).
2. **Migration**: `venv/bin/alembic upgrade head` → verify
   `venv/bin/alembic heads` shows a single head (`web88a_group_sites`).
3. **Env** (both `.env` files): `SITES_DOMAIN=osrs.site`. Leave
   `SITES_CSP_ENFORCE` unset until the prod soak completes.
4. **Restart webapi** (`droptracker-webapi`); confirm
   `curl -s localhost:31325/api/v1/sites/resolve?host=nosuch` → 404 JSON.
5. **Entitlement config** (already true if the dev DB change is mirrored):
   `custom_site: true` in the t3 row's `subscription_tiers.entitlements` JSON
   (admin tiers UI or SQL). No code involved.
6. **Web deploy via scripts/deploy.sh ONLY** (blue-green; builds idle colour,
   health-checks, flips upstream). The tenant rewrites are env-driven — with
   SITES_DOMAIN set in `.env` the build picks them up.
7. **nginx**: install `deploy/nginx/osrs-site.conf` →
   `/etc/nginx/sites-available/osrs-site` + symlink into sites-enabled →
   `nginx -t && systemctl reload nginx`. It proxies to the `droptracker_node`
   upstream, so blue-green keeps working unchanged.
8. **Cloudflare**: flip the zone's two proxied A records (`@` and `*`) from the
   dev IP to the prod IP. SSL mode stays **Flexible**. (Enable the free CSAM
   scanning tool on the zone while in the dashboard.)
9. **Verify** (same pre-DNS trick works pre-flip):
   `curl -H "Host: myclan.osrs.site" -H "X-Forwarded-Proto: https" http://<prod-ip>/`
   — then real URLs after the flip.
10. **After a week of clean CSP reports**: set `SITES_CSP_ENFORCE=1` in web
    `.env` + redeploy web (flips tenant CSP from Report-Only to enforcing).

## Rollback

- nginx: remove the two server blocks + reload (catch-all resumes 301→www).
- DNS: point the A records back at dev (or unproxy).
- App: blue-green flip back; migration is additive-only and safe to leave.
- Kill one site: `UPDATE group_sites SET suspended_at=NOW(), suspend_reason=...`
  (superadmin UI pending). Kill the feature: flip `custom_site` off the tier —
  every site 404s within 60s (fail-closed render gate).

## Still open after rollout (phase 4b)

- B2 site-asset uploads (banners); superadmin suspend/review UI; nav editor UI;
- dev-only demo data to undo before/at launch: group 2 `wom_id=3678` (borrowed
  from Realists for the demo), `public_members_list=1`, manual t3 sub leg, the
  `myclan` test site itself.
