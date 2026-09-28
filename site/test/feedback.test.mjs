// node --test site/test/*.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";
import { buildIssue, defang, hashIp, MIN_FILL_MS, validate } from "../lib/feedback.js";

const form = (over = {}) => ({
  kind: "bug",
  title: "Live view freezes",
  message: "After about a minute the live view stops updating.",
  elapsed: MIN_FILL_MS + 1,
  ...over,
});

test("accepts a normal report and trims it", () => {
  const { value, error } = validate(form({ title: "  Live   view freezes ", os: " macOS 26 " }));
  assert.equal(error, undefined);
  assert.equal(value.title, "Live view freezes");
  assert.equal(value.os, "macOS 26");
  assert.equal(value.kind, "bug");
});

test("flags the honeypot and too-fast submissions as spam", () => {
  assert.deepEqual(validate(form({ website: "http://spam" })), { spam: true });
  assert.deepEqual(validate(form({ elapsed: 500 })), { spam: true });
  assert.deepEqual(validate(form({ elapsed: undefined })), { spam: true });
});

test("rejects short, long and malformed input", () => {
  assert.match(validate(form({ title: "hi" })).error, /title/);
  assert.match(validate(form({ message: "short" })).error, /more/);
  assert.match(validate(form({ message: "x".repeat(5001) })).error, /under/);
  assert.match(validate(form({ github: "not a user!" })).error, /GitHub/);
  assert.match(validate(null).error, /JSON/);
});

test("unknown kinds become other feedback", () => {
  assert.equal(validate(form({ kind: "__proto__" })).value.kind, "other");
});

test("builds a labelled issue that credits a GitHub user", () => {
  const { value } = validate(form({ github: "@octocat", version: "0.3.1", steamos: "20260922" }));
  const issue = buildIssue(value);
  assert.equal(issue.title, "Bug report: Live view freezes");
  assert.deepEqual(issue.labels, ["feedback", "bug"]);
  assert.match(issue.body, /\| Frame Control version \| 0\.3\.1 \|/);
  assert.match(issue.body, /by @octocat\.$/);
});

test("anonymous feedback says replies won't reach the sender", () => {
  const issue = buildIssue(validate(form({ kind: "other" })).value);
  assert.deepEqual(issue.labels, ["feedback"]);
  assert.match(issue.body, /won't see replies/);
});

test("breaks mentions, issue refs and table cells in user text", () => {
  assert.equal(defang("ping @valve about #12"), "ping @\u200bvalve about #\u200b12");
  assert.equal(defang("email me@example.com"), "email me@\u200bexample.com");
  assert.equal(defang("see valve/steam#7 and GH-8"), "see valve/steam#\u200b7 and GH\u200b-8");
  assert.equal(defang("https://github.com/a/b/issues/1"), "https://github\u200b.com/a/b/issues/1");
  const issue = buildIssue(validate(form({ os: "a | b" })).value);
  assert.match(issue.body, /\| a \\\| b \|/);
});

test("hashes IPs without keeping them", async () => {
  const a = await hashIp("203.0.113.9", "salt");
  assert.equal(a.length, 24);
  assert.equal(a, await hashIp("203.0.113.9", "salt"));
  assert.notEqual(a, await hashIp("203.0.113.10", "salt"));
  assert.ok(!a.includes("203"));
});
