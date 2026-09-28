// POST /api/feedback: turns the website's feedback form into a GitHub issue.
//
// Environment (Cloudflare Pages → Settings → Variables and Secrets):
//   GITHUB_TOKEN  secret. Fine-grained token with Issues: read and write on GITHUB_REPO only.
//   GITHUB_REPO   owner/name, e.g. saphid/frame-control (wrangler.toml sets it).
//   FEEDBACK_RL   KV namespace binding for rate limits (optional; without it there is no limit).

import { buildIssue, hashIp, validate } from "../../lib/feedback.js";

const PER_IP_PER_HOUR = 5;
const TOTAL_PER_DAY = 100;

const json = (status, data) =>
  new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });

async function overLimit(kv, key, limit, ttl) {
  const count = Number(await kv.get(key)) || 0;
  if (count >= limit) return true;
  await kv.put(key, String(count + 1), { expirationTtl: ttl });
  return false;
}

export async function onRequestPost({ request, env }) {
  if (!env.GITHUB_TOKEN || !env.GITHUB_REPO) {
    return json(503, { error: "Feedback isn't connected to GitHub yet. Use the GitHub link instead." });
  }

  const origin = request.headers.get("origin");
  if (origin && new URL(origin).host !== new URL(request.url).host) {
    return json(403, { error: "Send feedback from the website's form." });
  }

  let input;
  try {
    input = await request.json();
  } catch {
    return json(400, { error: "Send the form as JSON." });
  }

  const checked = validate(input);
  // Bots get a success-shaped answer so they don't learn what tripped them.
  if (checked.spam) return json(200, { ok: true });
  if (checked.error) return json(400, { error: checked.error });

  if (env.FEEDBACK_RL) {
    const ip = request.headers.get("cf-connecting-ip") || "unknown";
    const hour = Math.floor(Date.now() / 3600e3);
    const day = Math.floor(Date.now() / 86400e3);
    // Salted with the secret token, so the stored hashes can't be reversed by trying every IP.
    const who = await hashIp(ip, env.GITHUB_TOKEN);
    if (await overLimit(env.FEEDBACK_RL, `ip:${who}:${hour}`, PER_IP_PER_HOUR, 3900)) {
      return json(429, { error: "That's a lot of feedback in one hour. Try again later, or use GitHub." });
    }
    if (await overLimit(env.FEEDBACK_RL, `day:${day}`, TOTAL_PER_DAY, 90000)) {
      return json(429, { error: "The form has had a busy day. Try again tomorrow, or use GitHub." });
    }
  }

  const res = await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/issues`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${env.GITHUB_TOKEN}`,
      accept: "application/vnd.github+json",
      "x-github-api-version": "2022-11-28",
      "user-agent": "frame-control-website",
      "content-type": "application/json",
    },
    body: JSON.stringify(buildIssue(checked.value)),
  });

  if (!res.ok) {
    console.log(`GitHub answered ${res.status}: ${(await res.text()).slice(0, 500)}`);
    return json(502, { error: "GitHub didn't accept it just now. Try again, or use the GitHub link." });
  }
  const issue = await res.json();
  return json(201, { ok: true, number: issue.number, url: issue.html_url });
}

export const onRequest = () => json(405, { error: "POST only." });
