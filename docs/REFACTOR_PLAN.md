# DropTracker API: Latency Refactor Plan

## Current Problem

The webhook endpoint blocks the request until full processing (DB, WOM, OSRS API) completes. Under ~400 req/min:

- Synchronous DB work (pymysql) blocks the asyncio event loop
- External API calls (WOM, OSRS Wiki) add seconds per request
- Connection pool exhaustion cascades into 20–200+ second request times

## Long-Term Solution: Async Queue Architecture

### Phase 1: Fast Acceptor (minimal change)

1. **Webhook handler** (< 100ms):
   - Validate payload
   - Persist raw payload to Redis list `webhook:queue` (or DB table `webhook_queue`)
   - Return 200 immediately

2. **Background worker process** (new, separate from API):
   - Poll/block on `webhook:queue`
   - Run existing `drop_processor`, `pb_processor`, etc. with full DB/WOM/OSRS logic
   - No request timeout pressure; workers can run synchronously (or with their own pool)

3. **Benefits**:
   - API responds in &lt; 100ms regardless of load
   - No intake limiting (queue absorbs bursts)
   - Processing failures don't block new submissions
   - Can scale workers independently

### Phase 2: Worker Scaling (optional)

- Multiple worker processes consuming the same queue
- Per-worker DB pool tuned for batch processing, not request handling

### Database Considerations for Dev

- **Option A**: Dev uses a DB snapshot/copy so refactor testing doesn't affect prod data
- **Option B**: Dev uses prod DB with a feature flag—new code path only active when flag is set (for canary testing)

---

## Dev Instance Setup

A separate systemd service runs the API on port **31324** (prod stays on 31323).

- **Start**: `sudo systemctl start droptracker-api-dev`
- **Stop**: `sudo systemctl stop droptracker-api-dev`
- **Test**: `curl -X POST http://127.0.0.1:31324/check` (or point a test client at port 31324)
- Dev is **disabled** by default; it does not start on boot.
- Dev uses the **same .env and DB** as prod unless you override (e.g. via `Environment=` in the unit or a `.env.dev`). For safe refactor testing, consider a DB snapshot or feature-flag–gated new code path.

---

## Implementation Checklist (Queue Refactor)

1. Add Redis queue key `webhook:queue` (or DB table `webhook_queue` with columns: id, payload_json, file_ref, created_at, status).
2. Create `webhook_acceptor` in `api/routes/webhook.py`: validate, push to queue, return 200.
3. Create `workers/webhook_consumer.py`: loop that pops from queue, calls existing processors, handles errors.
4. Add systemd unit `droptracker-webhook-worker.service` for the consumer.
5. Test in dev: start dev API + worker, send traffic to 31324, verify queue drains and submissions succeed.
6. Switch prod webhook route to acceptor; deploy worker; monitor.

---

## Production Baseline (Current)

After today's recovery, production is running:

- **Workers**: 4
- **Data DB pool**: 20 + 5 overflow per worker
- **Request timeout**: 35s
- **Max-requests**: 500 (+ jitter) per worker before recycle

This config still produces pool timeouts and 10–40s latency under load. The queue architecture above is the intended fix.
