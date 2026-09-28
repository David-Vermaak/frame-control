// The feedback form: posts to /api/feedback (site/functions/api/feedback.js), which opens a GitHub issue.
const form = document.getElementById("feedback");
const errorBox = document.getElementById("error");
const send = document.getElementById("send");
const started = performance.now();

const HINTS = {
  bug: "What you tried, what happened, and what you expected.",
  idea: "What you'd like, and what it would help you do.",
  question: "What you'd like to know.",
  other: "Anything you'd like to tell us.",
};

// Prefill the computer field; people rarely know the exact wording otherwise.
(function guessOs() {
  const ua = navigator.userAgent;
  const guess = /Windows/.test(ua) ? "Windows" : /Macintosh/.test(ua) ? "macOS" : /iPhone|iPad/.test(ua) ? "iOS"
    : /Android/.test(ua) ? "" : /Linux/.test(ua) ? "Linux" : "";
  if (guess) form.elements.os.value = guess;
})();

form.addEventListener("change", (e) => {
  if (e.target.name === "kind") document.getElementById("message-hint").textContent = HINTS[e.target.value];
});

// A prefilled GitHub issue form (.github/ISSUE_TEMPLATE), for when this form can't reach GitHub itself.
function githubUrl(data) {
  const params = data.kind === "idea"
    ? new URLSearchParams({ template: "idea.yml", title: data.title, what: data.message })
    : new URLSearchParams({ template: "bug.yml", title: data.title, description: data.message,
      version: data.version, os: [data.os, data.steamos && `SteamOS ${data.steamos}`].filter(Boolean).join(", ") });
  return `https://github.com/saphid/frame-control/issues/new?${params}`;
}

function showError(message, data) {
  errorBox.textContent = message + " ";
  if (data) {
    const a = document.createElement("a");
    a.href = githubUrl(data);
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = "Open it on GitHub instead";
    errorBox.append(a);
  }
  errorBox.hidden = false;
  errorBox.scrollIntoView({ behavior: "smooth", block: "center" });
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  errorBox.hidden = true;
  const data = Object.fromEntries(new FormData(form));
  data.elapsed = Math.round(performance.now() - started);

  if (data.title.trim().length < 5) {
    form.elements.title.focus();
    return showError("Give it a short title (at least 5 characters).");
  }
  if (data.message.trim().length < 10) {
    form.elements.message.focus();
    return showError("Tell us a little more in Details.");
  }

  send.disabled = true;
  send.textContent = "Sending…";
  try {
    const res = await fetch("/api/feedback", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(data),
    });
    const reply = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(reply.error || "Something went wrong sending that.");

    form.hidden = true;
    document.getElementById("done").hidden = false;
    if (reply.url) {
      const link = document.getElementById("issue-link");
      link.href = reply.url;
      link.hidden = false;
      document.getElementById("done-text").textContent = `Your feedback is now issue #${reply.number} on GitHub.`
        + (data.github ? " You'll be notified when someone replies." : " Bookmark it to follow along.");
    }
    window.scrollTo({ top: 0, behavior: "smooth" });
  } catch (err) {
    showError(err.message || "Couldn't reach the server.", data);
  } finally {
    send.disabled = false;
    send.textContent = "Send feedback";
  }
});
