/**
 * Contract tests for the edge capture Worker.
 *
 * The rule everything else serves: never return 2xx for a submission that is
 * not durably stored somewhere. On 2026-08-18 the origin answered 200 to
 * ~40,800 submissions it had thrown away and the plugin, trusting the 200,
 * never retried. These tests exist so that cannot be reintroduced quietly.
 *
 * Run: npm test
 */
import { test, describe, beforeEach } from "node:test";
import assert from "node:assert/strict";

import worker from "../src/index.js";

const ORIGIN = "api-origin.droptracker.io";

let fetchCalls;
let originResponder;
let configResponder;
let mirrorResponder;

function makeEnv(over = {}) {
  const puts = [];
  const points = [];
  return {
    ORIGIN_HOST: ORIGIN,
    ORIGIN_TIMEOUT_MS: "15000",
    FORCE_SPOOL_SAMPLE: "0",
    SPOOL: { put: async (k, b, o) => { puts.push({ key: k, body: b, opts: o }); } },
    LEDGER: { writeDataPoint: (d) => points.push(d) },
    _puts: puts,
    _points: points,
    ...over,
  };
}

const ctx = { waitUntil: (p) => { if (p && p.catch) p.catch(() => {}); } };

/**
 * A ctx that can be drained. The mirror config is refreshed inside waitUntil,
 * so "the isolate has since learned the config" is only reachable in a test by
 * awaiting what waitUntil was handed.
 */
function makeCtx() {
  const pending = [];
  return {
    waitUntil(p) {
      if (p && p.then) {
        pending.push(p);
        if (p.catch) p.catch(() => {});
      }
    },
    async flush() {
      while (pending.length) await Promise.allSettled(pending.splice(0));
    },
  };
}

const MIRROR = "dev-api.droptracker.io";

/** Every fetch the Worker made to `host`. */
function callsTo(host) {
  return fetchCalls.filter((c) => c.url.startsWith(`https://${host}`));
}

/** A realistic plugin submission: payload_json first, guid as the 5th field. */
function multipartBody(guid = "1787272773-4146262546365686368-229304989") {
  const boundary = "----dtBoundary123";
  const payload = JSON.stringify({
    embeds: [{
      title: "Level Up!",
      fields: [
        { name: "type", value: "level_up" },
        { name: "player_name", value: "vut4" },
        { name: "acc_hash", value: "4146262546365686368" },
        { name: "p_v", value: "6.0" },
        { name: "guid", value: guid },
      ],
    }],
  });
  const text =
    `--${boundary}\r\nContent-Disposition: form-data; name="payload_json"\r\n\r\n` +
    `${payload}\r\n--${boundary}--\r\n`;
  return {
    body: new TextEncoder().encode(text).buffer,
    contentType: `multipart/form-data; boundary=${boundary}`,
  };
}

function postRequest(url = "https://api.droptracker.io/webhook", guid) {
  const { body, contentType } = multipartBody(guid);
  return new Request(url, {
    method: "POST",
    headers: { "content-type": contentType, "CF-Connecting-IP": "203.0.113.9", "CF-Ray": "ray123" },
    body,
  });
}

beforeEach(() => {
  fetchCalls = [];
  originResponder = async () => new Response(JSON.stringify({ message: "Queued" }), { status: 200 });
  configResponder = async () =>
    new Response(JSON.stringify({ version: "t", mirror: { enabled: false, sample: 1 } }), { status: 200 });
  mirrorResponder = async () => new Response("", { status: 200 });
  globalThis.fetch = async (url, init) => {
    const u = String(url);
    fetchCalls.push({ url: u, init });
    if (u.includes("/edge-config")) return configResponder(u, init);
    if (u.startsWith(`https://${MIRROR}`)) return mirrorResponder(u, init);
    return originResponder(u, init);
  };
});

describe("the invariant: 2xx only when durably stored", () => {
  test("origin 5xx + spool succeeds -> 200", async () => {
    originResponder = async () => new Response("bad gateway", { status: 502 });
    const env = makeEnv();
    const res = await worker.fetch(postRequest(), env, ctx);
    assert.equal(res.status, 200);
    assert.equal(env._puts.length, 1, "must have written to R2");
  });

  test("origin 5xx + spool FAILS -> 503, never 200", async () => {
    originResponder = async () => new Response("bad gateway", { status: 502 });
    const env = makeEnv({ SPOOL: { put: async () => { throw new Error("R2 down"); } } });
    const res = await worker.fetch(postRequest(), env, ctx);
    assert.equal(res.status, 503, "a submission we could not store must not read as accepted");
  });

  test("origin unreachable + spool succeeds -> 200", async () => {
    originResponder = async () => { throw new Error("ECONNREFUSED"); };
    const env = makeEnv();
    const res = await worker.fetch(postRequest(), env, ctx);
    assert.equal(res.status, 200);
    assert.equal(env._puts.length, 1);
  });

  test("origin unreachable + no SPOOL binding -> 503", async () => {
    originResponder = async () => { throw new Error("ECONNREFUSED"); };
    const env = makeEnv({ SPOOL: undefined });
    const res = await worker.fetch(postRequest(), env, ctx);
    assert.equal(res.status, 503);
  });
});

describe("decision table", () => {
  test("origin 200 passes through and stores nothing", async () => {
    const env = makeEnv();
    const res = await worker.fetch(postRequest(), env, ctx);
    assert.equal(res.status, 200);
    assert.equal(env._puts.length, 0, "the happy path must not write to R2");
  });

  for (const status of [400, 401, 403]) {
    test(`origin ${status} passes through without capture`, async () => {
      originResponder = async () => new Response("nope", { status });
      const env = makeEnv();
      const res = await worker.fetch(postRequest(), env, ctx);
      assert.equal(res.status, status);
      assert.equal(env._puts.length, 0,
        "the plugin treats this as terminal; replaying it would only re-reject");
    });
  }

  test("413 IS captured, so it replays once the nginx cap is raised", async () => {
    originResponder = async () => new Response("too large", { status: 413 });
    const env = makeEnv();
    const res = await worker.fetch(postRequest(), env, ctx);
    assert.equal(res.status, 200);
    assert.equal(env._puts.length, 1);
  });

  test("origin 500 from the acceptor is captured", async () => {
    originResponder = async () => new Response("boom", { status: 500 });
    const env = makeEnv();
    await worker.fetch(postRequest(), env, ctx);
    assert.equal(env._puts.length, 1);
  });
});

describe("no request may re-enter our own route", () => {
  test("POST is forwarded to ORIGIN_HOST, not the incoming host", async () => {
    const env = makeEnv();
    await worker.fetch(postRequest(), env, ctx);
    assert.equal(fetchCalls.length, 1);
    assert.match(fetchCalls[0].url, new RegExp(`^https://${ORIGIN}/webhook`));
    assert.ok(!fetchCalls[0].url.includes("//api.droptracker.io"),
      "fetching the incoming URL would land back in this Worker");
  });

  test("a non-POST is forwarded to ORIGIN_HOST too", async () => {
    const env = makeEnv();
    const res = await worker.fetch(
      new Request("https://api.droptracker.io/webhook", { method: "GET" }), env, ctx);
    assert.equal(res.status, 200);
    assert.equal(fetchCalls.length, 1);
    assert.match(fetchCalls[0].url, new RegExp(`^https://${ORIGIN}/webhook`));
  });

  test("a non-POST is never captured", async () => {
    const env = makeEnv();
    await worker.fetch(
      new Request("https://api.droptracker.io/webhook", { method: "GET" }), env, ctx);
    assert.equal(env._puts.length, 0);
  });

  test("the query string survives the hop", async () => {
    const env = makeEnv();
    await worker.fetch(postRequest("https://api.droptracker.io/webhook?x=1"), env, ctx);
    assert.ok(fetchCalls[0].url.endsWith("/webhook?x=1"), fetchCalls[0].url);
  });
});

describe("client identity survives the extra hop", () => {
  test("X-Forwarded-For is set from CF-Connecting-IP", async () => {
    const env = makeEnv();
    await worker.fetch(postRequest(), env, ctx);
    const h = new Headers(fetchCalls[0].init.headers);
    assert.equal(h.get("x-forwarded-for"), "203.0.113.9",
      "the acceptor rate-limits on access_route[0]; losing this collapses every client into one bucket");
    assert.equal(h.get("x-dt-edge-capture"), "1");
  });
});

describe("capture metadata", () => {
  test("the guid is recovered from the multipart head", async () => {
    originResponder = async () => new Response("x", { status: 502 });
    const env = makeEnv();
    await worker.fetch(postRequest(undefined, "GUID-ABC-123"), env, ctx);
    assert.equal(env._puts[0].opts.customMetadata.guid, "GUID-ABC-123");
  });

  test("the raw body is stored byte-for-byte", async () => {
    originResponder = async () => new Response("x", { status: 502 });
    const env = makeEnv();
    const { body } = multipartBody();
    await worker.fetch(
      new Request("https://api.droptracker.io/webhook", {
        method: "POST", headers: { "content-type": "multipart/form-data; boundary=----dtBoundary123" }, body,
      }), env, ctx);
    assert.equal(env._puts[0].body.byteLength, body.byteLength);
  });

  test("the key sorts chronologically and records the origin status", async () => {
    originResponder = async () => new Response("x", { status: 504 });
    const env = makeEnv();
    await worker.fetch(postRequest(), env, ctx);
    assert.match(env._puts[0].key, /^webhook\/\d{4}\/\d{2}\/\d{2}\/\d{2}\/\d+-/);
    assert.equal(env._puts[0].opts.customMetadata.origin_status, "504");
  });
});

describe("the warm-path sampler", () => {
  test("a sampled success is captured but still returns the origin's response", async () => {
    const env = makeEnv({ FORCE_SPOOL_SAMPLE: "1" });
    const res = await worker.fetch(postRequest(), env, ctx);
    assert.equal(res.status, 200);
    assert.equal(env._puts.length, 1);
    assert.equal(env._puts[0].opts.customMetadata.sample, "1");
  });

  test("a failed sample write never turns a good 200 into a 503", async () => {
    const env = makeEnv({
      FORCE_SPOOL_SAMPLE: "1",
      SPOOL: { put: async () => { throw new Error("R2 down"); } },
    });
    const res = await worker.fetch(postRequest(), env, ctx);
    assert.equal(res.status, 200);
  });

  test("sampling off means no captures on the happy path", async () => {
    const env = makeEnv({ FORCE_SPOOL_SAMPLE: "0" });
    for (let i = 0; i < 25; i++) await worker.fetch(postRequest(), makeEnv(), ctx);
    assert.equal(env._puts.length, 0);
  });
});

describe("the ledger is observability, never a failure mode", () => {
  test("a throwing ledger does not fail the submission", async () => {
    const env = makeEnv({ LEDGER: { writeDataPoint: () => { throw new Error("nope"); } } });
    const res = await worker.fetch(postRequest(), env, ctx);
    assert.equal(res.status, 200);
  });

  test("a datapoint is written for an ordinary success", async () => {
    const env = makeEnv();
    await worker.fetch(postRequest(), env, ctx);
    assert.equal(env._points.length, 1);
    assert.equal(env._points[0].doubles[0], 200);
  });
});

/**
 * Mirroring production submissions at the dev instance.
 *
 * The governing rule is the Worker's rule 2: a mirror that fails, times out,
 * throws, or was misconfigured must be indistinguishable to the client from one
 * that was never switched on. Nothing about the capture contract may move.
 */
describe("the dev mirror", () => {
  function mirrorEnv(over = {}) {
    return makeEnv({ MIRROR_HOST: MIRROR, MIRROR_TIMEOUT_MS: "5000", ...over });
  }

  function configSays(mirrorCfg) {
    configResponder = async () =>
      new Response(JSON.stringify({ version: "t", mirror: mirrorCfg }), { status: 200 });
  }

  /** One request to teach this isolate the config, then a clean slate. */
  async function prime(env, c) {
    await worker.fetch(postRequest(), env, c);
    await c.flush();
    fetchCalls = [];
  }

  const configFetches = () => fetchCalls.filter((c) => c.url.includes("/edge-config"));

  test("an unset MIRROR_HOST mirrors nothing and never even fetches the config", async () => {
    configSays({ enabled: true, sample: 1 });
    const env = makeEnv(); // no MIRROR_HOST
    const c = makeCtx();
    await worker.fetch(postRequest(), env, c);
    await c.flush();
    assert.equal(callsTo(MIRROR).length, 0);
    assert.equal(configFetches().length, 0, "an unset host must not cost even the config lookup");
  });

  test("a cold isolate does not mirror, even when the config says enabled", async () => {
    configSays({ enabled: true, sample: 1 });
    const env = mirrorEnv();
    const c = makeCtx();
    await worker.fetch(postRequest(), env, c);
    assert.equal(callsTo(MIRROR).length, 0, "the hot path must never block on /edge-config");
    assert.equal(configFetches().length, 1, "but it should kick off the refresh");
  });

  test("once the isolate has the config, submissions are mirrored", async () => {
    configSays({ enabled: true, sample: 1 });
    const env = mirrorEnv();
    const c = makeCtx();
    await prime(env, c);

    await worker.fetch(postRequest(), env, c);
    await c.flush();

    const mirrored = callsTo(MIRROR);
    assert.equal(mirrored.length, 1);
    assert.equal(mirrored[0].url, `https://${MIRROR}/webhook`);
    assert.equal(
      fetchCalls.filter((f) => f.url === `https://${ORIGIN}/webhook`).length, 1,
      "the production leg still happens exactly once",
    );
  });

  test("config says disabled -> never mirrors", async () => {
    configSays({ enabled: false, sample: 1 });
    const env = mirrorEnv();
    const c = makeCtx();
    await prime(env, c);
    await worker.fetch(postRequest(), env, c);
    await c.flush();
    assert.equal(callsTo(MIRROR).length, 0);
  });

  test("sample 0 -> never mirrors, so the throttle works with no redeploy", async () => {
    configSays({ enabled: true, sample: 0 });
    const env = mirrorEnv();
    const c = makeCtx();
    await prime(env, c);
    for (let i = 0; i < 25; i++) await worker.fetch(postRequest(), env, c);
    await c.flush();
    assert.equal(callsTo(MIRROR).length, 0);
  });

  test("a malformed sample turns mirroring off rather than on", async () => {
    configSays({ enabled: true, sample: "banana" });
    const env = mirrorEnv();
    const c = makeCtx();
    await prime(env, c);
    for (let i = 0; i < 10; i++) await worker.fetch(postRequest(), env, c);
    await c.flush();
    assert.equal(callsTo(MIRROR).length, 0, "NaN must fail closed");
  });

  test("an unreachable /edge-config leaves mirroring off", async () => {
    configResponder = async () => { throw new Error("origin down"); };
    const env = mirrorEnv();
    const c = makeCtx();
    await prime(env, c);
    await worker.fetch(postRequest(), env, c);
    await c.flush();
    assert.equal(callsTo(MIRROR).length, 0);
  });

  test("a 500 from /edge-config leaves mirroring off", async () => {
    configResponder = async () => new Response("boom", { status: 500 });
    const env = mirrorEnv();
    const c = makeCtx();
    await prime(env, c);
    await worker.fetch(postRequest(), env, c);
    await c.flush();
    assert.equal(callsTo(MIRROR).length, 0);
  });

  test("a mirror that throws does not change what the client sees", async () => {
    configSays({ enabled: true, sample: 1 });
    mirrorResponder = async () => { throw new Error("dev box is down"); };
    const env = mirrorEnv();
    const c = makeCtx();
    await prime(env, c);

    const res = await worker.fetch(postRequest(), env, c);
    await c.flush();
    assert.equal(res.status, 200);
    assert.equal(await res.json().then((j) => j.message), "Queued");
  });

  test("a mirror that 503s does not change the spool decision", async () => {
    configSays({ enabled: true, sample: 1 });
    mirrorResponder = async () => new Response("nope", { status: 503 });
    const env = mirrorEnv();
    const c = makeCtx();
    await prime(env, c);

    // Only now, so the priming request does not spool a capture of its own.
    originResponder = async () => new Response("bad gateway", { status: 502 });
    const res = await worker.fetch(postRequest(), env, c);
    await c.flush();
    assert.equal(res.status, 200, "the origin failure still spools and still answers 200");
    assert.equal(env._puts.length, 1);
    assert.equal(callsTo(MIRROR).length, 1);
  });

  test("mirroring a success still writes nothing to R2", async () => {
    configSays({ enabled: true, sample: 1 });
    const env = mirrorEnv();
    const c = makeCtx();
    await prime(env, c);
    await worker.fetch(postRequest(), env, c);
    await c.flush();
    assert.equal(env._puts.length, 0, "the mirror is not a capture path");
  });

  test("a non-POST is never mirrored", async () => {
    configSays({ enabled: true, sample: 1 });
    const env = mirrorEnv();
    const c = makeCtx();
    await prime(env, c);
    await worker.fetch(new Request("https://api.droptracker.io/webhook", { method: "GET" }), env, c);
    await c.flush();
    assert.equal(callsTo(MIRROR).length, 0);
  });

  test("the mirror leg carries the real client IP and marks itself", async () => {
    configSays({ enabled: true, sample: 1 });
    const env = mirrorEnv();
    const c = makeCtx();
    await prime(env, c);
    await worker.fetch(postRequest(), env, c);
    await c.flush();

    const h = new Headers(callsTo(MIRROR)[0].init.headers);
    assert.equal(h.get("x-dt-mirror"), "1", "dev keys every mirror-only behaviour off this header");
    assert.equal(h.get("x-forwarded-for"), "203.0.113.9",
      "without this every mirrored submission shares one rate-limit bucket on dev");
    assert.equal(h.get("host"), null);
  });

  test("the query string survives the mirror hop", async () => {
    configSays({ enabled: true, sample: 1 });
    const env = mirrorEnv();
    const c = makeCtx();
    await prime(env, c);
    await worker.fetch(postRequest("https://api.droptracker.io/submit?x=1"), env, c);
    await c.flush();
    assert.equal(callsTo(MIRROR)[0].url, `https://${MIRROR}/submit?x=1`);
  });

  test("the config is fetched once per TTL, not once per request", async () => {
    configSays({ enabled: true, sample: 1 });
    const env = mirrorEnv();
    const c = makeCtx();
    await prime(env, c);
    for (let i = 0; i < 20; i++) await worker.fetch(postRequest(), env, c);
    await c.flush();
    assert.equal(configFetches().length, 0, "20 requests inside the TTL must not be 20 lookups");
  });
});
