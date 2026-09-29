// Downloads what the app bundles so users install nothing else: a standalone
// Python (python-build-standalone), adb (Android platform-tools) and a CA
// bundle. Each goes in build/deps/<os>-<arch>/{python,tools}, which package.json
// copies into the app's resources. Also KDE Connect for the Frame (the same arm64
// packages for every build), into ../frame/kdeconnect/packages as listed in
// ../frame/kdeconnect/packages.json. Everything is pinned by version and SHA-256.
//   node build/fetch-deps.js mac arm64 | win x64 | linux x64 arm64
const crypto = require("crypto");
const fs = require("fs");
const https = require("https");
const path = require("path");
const { execFileSync } = require("child_process");

const PY = "3.12.14+20260924";
const PY_URL = (triple) => "https://github.com/astral-sh/python-build-standalone/releases/download/"
  + `${PY.split("+")[1]}/cpython-${PY}-${triple}-install_only_stripped.tar.gz`;
const PYTHON = {
  "mac-arm64": ["aarch64-apple-darwin", "c2edb321cd32ec2b170df208db0446dccc4398db602ca27cf2079098fb1f7d9d"],
  "win-x64": ["x86_64-pc-windows-msvc", "c5bf8edfe858c1df9891be498b5bbc8761d383df5b9790658b088fea4870433a"],
  "linux-x64": ["x86_64-unknown-linux-gnu", "269b2c99e4db15b242bf01832f4fea1e8f1a664f273cff519393f296e9820b41"],
  "linux-arm64": ["aarch64-unknown-linux-gnu", "c8499b61252c433280f134df954464d19811527b31cb920c35fc6967c1222e35"],
};

// Google publishes no arm64 Linux platform-tools; there the app uses the system adb.
const PT = "37.0.1";
const PT_URL = (os) => `https://dl.google.com/android/repository/platform-tools_r${PT}-${os}.zip`;
const TOOLS = {
  mac: ["darwin", "ee39ad5967e95c2a07f04dbcbde96b1a0c916ba376096db5d2f498b7727a5d1d", ["adb"]],
  win: ["win", "45f4d63113e895ebde0c90f194099a4676b6ac653bd28d54314a9e022bbc1a99",
        ["adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll", "libwinpthread-1.dll"]],
  linux: ["linux", "d230f13842f60f782a8645f9c813f8f845bf36089ea7289f28c48f17979313f1", ["adb"]],
};

// Mozilla's CA list, as curl publishes it: Python on Windows only trusts roots
// already in the Windows store (see frame_host.trust_bundled_cas).
const CA = "2026-09-25";
const CA_SHA256 = "a41b5d356aea97a529fe27e0f7316d2f9d946d75927476cf9cf1b90637d00505";

// Parts of Python the server never imports (GUI, tests, packaging, headers).
const PRUNE = [
  "include", "share", "Scripts", "libs", "tcl", "lib/pkgconfig", "lib/itcl4", "lib/tcl8", "lib/tcl8.6",
  "lib/tk8.6", "lib/thread2.8", "bin/idle3", "bin/idle3.12", "bin/pip", "bin/pip3", "bin/pip3.12",
  "bin/pydoc3", "bin/pydoc3.12", "bin/2to3", "bin/2to3-3.12", "bin/python3-config", "bin/python3.12-config",
  ...["test", "idlelib", "tkinter", "turtledemo", "ensurepip", "lib2to3", "site-packages/pip", "pydoc_data", "venv"]
    .flatMap((d) => [`lib/python3.12/${d}`, `Lib/${d}`]),
];

function get(url, redirects = 5) {
  return new Promise((resolve, reject) => {
    https.get(url, { timeout: 60000 }, (res) => {
      if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
        res.resume();
        if (!redirects) return reject(new Error(`${url}: too many redirects`));
        let next;
        try { next = new URL(res.headers.location, url).href; }
        catch { return reject(new Error(`${url}: bad redirect ${res.headers.location}`)); }
        return resolve(get(next, redirects - 1));
      }
      if (res.statusCode !== 200) return reject(new Error(`${url}: HTTP ${res.statusCode}`));
      const chunks = [];
      res.on("data", (c) => chunks.push(c));
      res.on("end", () => resolve(Buffer.concat(chunks)));
    }).on("timeout", function () { this.destroy(new Error(`${url}: timed out`)); }).on("error", reject);
  });
}

async function download(url, sha256, file) {
  const data = await get(url);
  const sum = crypto.createHash("sha256").update(data).digest("hex");
  if (sum !== sha256) throw new Error(`checksum mismatch for ${url}: ${sum}`);
  fs.writeFileSync(file, data);
}

// Windows' own bsdtar: Git's GNU tar, often first on PATH, reads C:\ as a remote host.
const TAR = process.platform === "win32" ? path.join(process.env.SystemRoot || "C:\\Windows", "System32", "tar.exe") : "tar";

function extract(file, dir) {
  fs.mkdirSync(dir, { recursive: true });
  // bsdtar (macOS, Windows 10+) reads zip files; GNU tar doesn't, so fall back to unzip.
  try { execFileSync(TAR, ["-xf", file, "-C", dir]); }
  catch (e) {
    if (!file.endsWith(".zip")) throw e;
    execFileSync("unzip", ["-q", "-o", file, "-d", dir]);
  }
  fs.rmSync(file);
}

async function fetch(os, arch) {
  const key = `${os}-${arch}`;
  if (!PYTHON[key]) throw new Error(`no bundle for ${key}`);
  const out = path.join(__dirname, "deps", key);
  const stamp = path.join(out, ".version");
  const version = `python ${PY}, platform-tools ${PT}, CA ${CA}`;
  if (fs.existsSync(stamp) && fs.readFileSync(stamp, "utf8") === version) {
    console.log(`${key}: already fetched (${version})`);
    return;
  }
  fs.rmSync(out, { recursive: true, force: true });
  fs.mkdirSync(out, { recursive: true });

  const [triple, pySha] = PYTHON[key];
  const tgz = path.join(out, "python.tar.gz");
  await download(PY_URL(triple), pySha, tgz);
  extract(tgz, out);  // unpacks to python/
  for (const p of PRUNE) fs.rmSync(path.join(out, "python", p), { recursive: true, force: true });
  const stdlib = path.join(out, "python", "lib", "python3.12");  // macOS and Linux: drop the static libpython
  if (fs.existsSync(stdlib)) {
    for (const d of fs.readdirSync(stdlib)) {
      if (d.startsWith("config-3.12")) fs.rmSync(path.join(stdlib, d), { recursive: true, force: true });
    }
  }

  const tools = path.join(out, "tools");
  fs.mkdirSync(tools);
  if (!(os === "linux" && arch === "arm64")) {
    const [name, ptSha, keep] = TOOLS[os];
    const zip = path.join(out, "pt.zip");
    const tmp = path.join(out, "pt");
    await download(PT_URL(name), ptSha, zip);
    extract(zip, tmp);
    for (const f of [...keep, "NOTICE.txt", "source.properties"]) {
      fs.copyFileSync(path.join(tmp, "platform-tools", f), path.join(tools, f));
    }
    if (os !== "win") fs.chmodSync(path.join(tools, "adb"), 0o755);
    fs.rmSync(tmp, { recursive: true, force: true });
  }
  await download(`https://curl.se/ca/cacert-${CA}.pem`, CA_SHA256, path.join(tools, "cacert.pem"));
  fs.writeFileSync(stamp, version);
  console.log(`${key}: ${version} -> ${out}`);
}

// As frame/kdeconnect/fetch.py does for the iPhone app's bundle.
async function fetchKdeConnect() {
  const dir = path.join(__dirname, "..", "..", "frame", "kdeconnect");
  const manifest = JSON.parse(fs.readFileSync(path.join(dir, "packages.json"), "utf8"));
  const out = path.join(dir, "packages");
  fs.mkdirSync(out, { recursive: true });
  const sha = (file) => crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex");
  for (const p of manifest.packages) {
    const file = path.join(out, p.file);
    if (fs.existsSync(file) && sha(file) === p.sha256) continue;
    await download(manifest.release + p.file, p.sha256, file);
  }
  const wanted = new Set(manifest.packages.map((p) => p.file));
  for (const f of fs.readdirSync(out)) if (!wanted.has(f)) fs.rmSync(path.join(out, f), { recursive: true, force: true });
  console.log(`KDE Connect for the Frame: ${manifest.packages.length} packages -> ${out}`);
}

(async () => {
  const [os, ...archs] = process.argv.slice(2);
  if (!os || !archs.length) throw new Error("usage: node build/fetch-deps.js <mac|win|linux> <arch>...");
  for (const arch of archs) await fetch(os, arch);
  await fetchKdeConnect();
})().catch((e) => { console.error(e.message); process.exit(1); });
