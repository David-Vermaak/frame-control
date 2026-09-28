// Feedback form → GitHub issue. Pure functions, so tests can run them without
// Cloudflare or GitHub (site/test/feedback.test.mjs).

export const KINDS = {
  bug: { label: "bug", title: "Bug report" },
  idea: { label: "enhancement", title: "Idea" },
  question: { label: "question", title: "Question" },
  other: { label: null, title: "Other feedback" },
};

export const LIMITS = { title: [5, 120], message: [10, 5000], field: 120 };

// Anyone who fills the form in under this many milliseconds is a script.
export const MIN_FILL_MS = 3000;

const GITHUB_LOGIN = /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$/;

const oneLine = (value, max) => String(value ?? "").replace(/\s+/g, " ").trim().slice(0, max);

// Mentions in someone else's text would ping strangers, and issue references
// (#1, owner/repo#1, GH-1, github.com links) would add backlinks to other
// people's issues, so break them all with a zero-width space.
const ZWSP = "\u200b";
export function defang(text) {
  return text
    .replace(/@(?=[A-Za-z0-9])/g, `@${ZWSP}`)
    .replace(/#(?=\d)/g, `#${ZWSP}`)
    .replace(/\b(GH)-(?=\d)/gi, `$1${ZWSP}-`)
    .replace(/\b(github)\.com/gi, `$1${ZWSP}.com`);
}

// Returns { error } or { value } with every field trimmed and bounded.
export function validate(input) {
  if (!input || typeof input !== "object") return { error: "Send the form as JSON." };
  if (oneLine(input.website, 200)) return { spam: true };
  // Measured in the browser with a monotonic clock, so clock skew doesn't matter.
  const elapsed = Number(input.elapsed);
  if (!Number.isFinite(elapsed) || elapsed < MIN_FILL_MS) return { spam: true };

  const kind = Object.hasOwn(KINDS, input.kind) ? input.kind : "other";
  const title = oneLine(input.title, LIMITS.title[1]);
  const message = String(input.message ?? "").replace(/\r\n?/g, "\n").trim();
  if (title.length < LIMITS.title[0]) return { error: "Give it a short title (at least 5 characters)." };
  if (message.length < LIMITS.message[0]) return { error: "Tell us a little more (at least 10 characters)." };
  if (message.length > LIMITS.message[1]) return { error: `Keep it under ${LIMITS.message[1]} characters.` };

  const github = oneLine(input.github, 40).replace(/^@/, "");
  if (github && !GITHUB_LOGIN.test(github)) return { error: "That doesn't look like a GitHub username." };

  return {
    value: {
      kind,
      title,
      message,
      github,
      version: oneLine(input.version, LIMITS.field),
      os: oneLine(input.os, LIMITS.field),
      steamos: oneLine(input.steamos, LIMITS.field),
    },
  };
}

export function buildIssue(value) {
  const kind = KINDS[value.kind];
  const details = [
    ["Frame Control version", value.version],
    ["Computer", value.os],
    ["SteamOS build", value.steamos],
  ].filter(([, v]) => v);

  const lines = [defang(value.message), ""];
  if (details.length) {
    lines.push("| | |", "|---|---|", ...details.map(([k, v]) => `| ${k} | ${defang(v).replace(/\|/g, "\\|")} |`), "");
  }
  lines.push(
    "---",
    value.github
      ? `Sent from the website feedback form by @${value.github}.`
      : "Sent from the website feedback form. The sender left no GitHub username, so they won't see replies here.",
  );

  return {
    title: `${kind.title}: ${value.title}`,
    body: lines.join("\n"),
    labels: ["feedback", ...(kind.label ? [kind.label] : [])],
  };
}

export async function hashIp(ip, salt) {
  const bytes = new TextEncoder().encode(`${salt}:${ip}`);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].slice(0, 12).map((b) => b.toString(16).padStart(2, "0")).join("");
}
