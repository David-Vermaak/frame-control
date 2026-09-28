// Shared by every page. Change the settings here, not in the HTML.
const SITE = {
  repo: "saphid/frame-control",
  // Ko-fi page name, the part after ko-fi.com/. Donate buttons stay hidden while it's empty.
  kofi: "alexsouthwell",
};

const RELEASE = `https://github.com/${SITE.repo}/releases/latest/download/`;

// Donate buttons.
for (const el of document.querySelectorAll("[data-kofi]")) {
  if (SITE.kofi) el.href = `https://ko-fi.com/${SITE.kofi}`;
  else el.hidden = true;
}
for (const el of document.querySelectorAll("[data-needs-kofi]")) el.hidden = !SITE.kofi;
for (const el of document.querySelectorAll("[data-no-kofi]")) el.hidden = !!SITE.kofi;

// Best guess at the visitor's platform, for the hero button and the download cards.
function detectPlatform() {
  const ua = navigator.userAgent;
  const hint = navigator.userAgentData?.platform || navigator.platform || "";
  if (/iPhone|iPad|iPod/.test(ua) || (/Mac/.test(hint) && navigator.maxTouchPoints > 1)) return "ios";
  if (/Android/.test(ua)) return null;
  if (/Mac/.test(hint) || /Macintosh/.test(ua)) return "mac";
  if (/Win/.test(hint) || /Windows/.test(ua)) return "windows";
  if (/Linux/.test(hint) || /Linux/.test(ua)) return /aarch64|arm64/i.test(ua + hint) ? "linux-arm" : "linux";
  return null;
}

const PLATFORMS = {
  mac: { name: "macOS", file: "Frame-Control-mac-arm64.dmg" },
  windows: { name: "Windows", file: "Frame-Control-Setup-x64.exe" },
  linux: { name: "Linux", file: "Frame-Control-linux-x86_64.AppImage" },
  "linux-arm": { name: "Linux (arm64)", file: "Frame-Control-linux-arm64.AppImage" },
};

const platform = detectPlatform();
const hero = document.getElementById("hero-download");
if (hero && PLATFORMS[platform]) {
  hero.href = RELEASE + PLATFORMS[platform].file;
  hero.querySelector("[data-label]").textContent = `Download for ${PLATFORMS[platform].name}`;
}
const card = platform && document.querySelector(`.dl[data-os="${platform.replace("-arm", "")}"]`);
if (card) {
  card.classList.add("mine");
  card.querySelector(".tag").hidden = false;
}

// Latest version number, so the page never goes stale. Fails quietly.
const versionEls = document.querySelectorAll("[data-version]");
if (versionEls.length) {
  fetch(`https://api.github.com/repos/${SITE.repo}/releases/latest`, { headers: { accept: "application/vnd.github+json" } })
    .then((r) => (r.ok ? r.json() : Promise.reject()))
    .then((release) => {
      for (const el of versionEls) {
        el.textContent = release.tag_name;
        el.closest("[hidden]")?.removeAttribute("hidden");
      }
    })
    .catch(() => {});
}

document.querySelectorAll("[data-year]").forEach((el) => (el.textContent = new Date().getFullYear()));
