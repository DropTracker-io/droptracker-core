/**
 * Durable capture in front of POST /webhook.
 *
 * The contract this exists to protect is stated in api/routes/webhook.py: a 200
 * means "we have durably taken responsibility for this submission", and the
 * plugin stops retrying on the strength of it. On 2026-08-18 the origin
 * answered 200 to ~40,800 submissions it had thrown away, and because the
 * plugin's retry budget is only ~17 minutes (10 attempts, 1000ms << attempt,
 * SubmissionManager.scheduleRetryOrFail) an 87-minute outage was unrecoverable
 * even once the origin started answering honestly.
 *
 * This Worker moves the durable step to the edge. When the origin cannot take a
 * submission, the raw request body goes to R2 and the client is told we have it;
 * scripts/drain_r2_spool.py replays it byte-for-byte once the origin is back.
 *
 * Two rules govern everything below:
 *
 *   1. Never return 2xx for a submission that is not durably stored somewhere.
 *      If the origin fails AND the R2 write fails, answer 503 so the plugin
 *      keeps its copy. This is the whole lesson of 2026-08-18.
 *
 *   2. A bug in this Worker must never be worse than not having it. Every
 *      failure path falls back to forwarding the request untouched.
 *
 * Replay safety rests on data/submissions/common.ensure_can_create being
 * unbounded in time and transport-blind. Do not deploy this against an origin
 * where tests/unit/test_replay_window_fidelity.py is failing.
 */

const SPOOL_ON_STATUS = (status) =>
  status === 0 ||        // network error, DNS failure, or origin timeout
  status >= 500 ||       // 502/503/504 from nginx, 500 from the acceptor
  status === 413;        // body over nginx's cap; replays cleanly once raised

// The plugin treats these as terminal and will not retry, so capturing them
// would only produce objects that re-reject forever on replay.
const TERMINAL_STATUS = new Set([400, 401, 403]);

export default {
  async fetch(request, env, ctx) {
    // The route should only ever send us POSTs, but a stray method must not be
    // buffered or captured.
    if (request.method !== "POST") return fetch(request);

    // Read the body once. Streaming is not an option: if the origin fails
    // mid-stream the bytes are gone and there is nothing left to spool.
    let body;
    try {
      body = await request.arrayBuffer();
    } catch {
      // Body never consumed, so the original request is still forwardable.
      return fetch(request);
    }

    try {
      return await handle(request, body, env, ctx);
    } catch (err) {
      // Last-ditch: forward once with no capture rather than fail the client.
      try {
        return await forward(request, body, env);
      } catch {
        return json({ error: "Intake capture unavailable" }, 503);
      }
    }
  },
};

async function handle(request, body, env, ctx) {
  const started = Date.now();

  let response = null;
  let status = 0;
  try {
    response = await forward(request, body, env);
    status = response.status;
  } catch {
    status = 0; // treated as a hard failure below
  }

  const mustSpool = SPOOL_ON_STATUS(status);

  // The spool branch only runs during a disaster, which is exactly when you
  // find out it has rotted. Sampling a small fraction of *successful* requests
  // keeps the R2 write and the drain path continuously exercised; GUID dedup
  // makes the resulting replay a no-op.
  const sampleRate = Number(env.FORCE_SPOOL_SAMPLE ?? 0.001);
  const isSample =
    !mustSpool && status >= 200 && status < 300 && Math.random() < sampleRate;

  let stored = false;
  if (mustSpool || isSample) {
    const write = spool(body, request, env, status, isSample);
    // Register with waitUntil as well as awaiting: ~0.9% of clients hang up
    // before the response (3,324 499s/day), and the capture must survive that.
    ctx.waitUntil(write);
    stored = await write;
  }

  ledger(env, {
    path: new URL(request.url).pathname,
    guid: mustSpool || isSample ? extractGuid(body) : null,
    ip: request.headers.get("CF-Connecting-IP"),
    ray: request.headers.get("CF-Ray"),
    colo: request.cf?.colo,
    status,
    bytes: body.byteLength,
    ms: Date.now() - started,
    spooled: stored,
  });

  if (mustSpool) {
    if (!stored) {
      // Rule 1. The client keeps its copy and retries.
      return json({ error: "Queue temporarily unavailable" }, 503);
    }
    return json({ message: "Queued" }, 200);
  }

  // Terminal rejections and every success pass straight through, so the plugin
  // sees exactly what the origin said.
  return response;
}

function forward(request, body, env) {
  const url = new URL(request.url);
  const target = `https://${env.ORIGIN_HOST}${url.pathname}${url.search}`;

  const headers = new Headers(request.headers);
  // Host is managed by the runtime; the origin distinguishes us by ORIGIN_HOST.
  headers.delete("host");
  headers.set("X-DT-Edge-Capture", "1");

  // api/__init__.py rate-limits on request.access_route[0], so the real client
  // IP has to survive the extra hop or every submission collapses into one
  // bucket.
  const clientIp = request.headers.get("CF-Connecting-IP");
  if (clientIp) {
    headers.set("X-Forwarded-For", clientIp);
    headers.set("X-Real-IP", clientIp);
  }

  return fetch(target, {
    method: "POST",
    headers,
    body,
    signal: AbortSignal.timeout(Number(env.ORIGIN_TIMEOUT_MS ?? 15000)),
  });
}

async function spool(body, request, env, status, isSample) {
  const now = new Date();
  const p = (n) => String(n).padStart(2, "0");
  const key =
    `webhook/${now.getUTCFullYear()}/${p(now.getUTCMonth() + 1)}/` +
    `${p(now.getUTCDate())}/${p(now.getUTCHours())}/` +
    `${now.getTime()}-${request.headers.get("CF-Ray") || crypto.randomUUID()}.bin`;

  try {
    await env.SPOOL.put(key, body, {
      httpMetadata: {
        contentType:
          request.headers.get("content-type") || "application/octet-stream",
      },
      // R2 stores an MD5 etag of its own, so there is no reason to burn CPU
      // hashing the body here. Values must be strings.
      customMetadata: {
        origin_status: String(status),
        path: new URL(request.url).pathname,
        client_ip: request.headers.get("CF-Connecting-IP") || "",
        guid: extractGuid(body) || "",
        bytes: String(body.byteLength),
        sample: isSample ? "1" : "0",
        captured_at: now.toISOString(),
      },
    });
    return true;
  } catch {
    return false;
  }
}

/**
 * Best-effort GUID for the ledger and for R2 metadata.
 *
 * The plugin builds the multipart body with payload_json before the file part
 * (SubmissionManager.sendWebhookWithRetry), and guid is the 5th embed field, so
 * it is always inside the first chunk. Full multipart parsing on every request
 * would cost real CPU for a value nothing depends on -- the durable copy is the
 * raw body, and the origin re-derives the guid itself on replay.
 */
function extractGuid(body) {
  try {
    const head = new TextDecoder("utf-8", { fatal: false }).decode(
      new Uint8Array(body, 0, Math.min(body.byteLength, 16384)),
    );
    const field = head.match(
      /"name"\s*:\s*"guid"\s*,\s*"value"\s*:\s*"([^"]{1,64})"/i,
    );
    if (field) return field[1];
    const plain = head.match(/"(?:guid|unique_id)"\s*:\s*"([^"]{1,64})"/i);
    return plain ? plain[1] : null;
  } catch {
    return null;
  }
}

/**
 * One datapoint per request. This is the always-on ledger that answers "did we
 * ever receive GUID X?" -- the question nobody could answer on 2026-08-18.
 *
 * Deliberately not an R2 object per request: at ~11.2M requests/month that is
 * ~$50/mo in Class A operations, versus Analytics Engine which is included.
 */
function ledger(env, e) {
  if (!env.LEDGER) return;
  try {
    env.LEDGER.writeDataPoint({
      blobs: [
        e.path,
        e.guid || "",
        e.ip || "",
        e.ray || "",
        e.colo || "",
        e.spooled ? "spooled" : "forwarded",
      ],
      doubles: [e.status, e.bytes, e.ms],
      indexes: [(e.guid || "").slice(0, 32)],
    });
  } catch {
    // The ledger is observability, never a reason to fail a submission.
  }
}

function json(payload, status) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}
