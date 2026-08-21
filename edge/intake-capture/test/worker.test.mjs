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
  globalThis.fetch = async (url, init) => {
    fetchCalls.push({ url: String(url), init });
    return originResponder(url, init);
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
