// Run: node --test app/test/
const test = require("node:test");
const assert = require("node:assert");
const { isNewer, assetName, updateMethod, macBundle } = require("../updater");

test("versions compare numerically, and a release beats its pre-releases", () => {
  assert.ok(isNewer("0.3.10", "0.3.9"));
  assert.ok(isNewer("v1.0.0", "0.9.9"));
  assert.ok(!isNewer("0.3.1", "0.3.1"));
  assert.ok(!isNewer("0.3.0", "0.3.1"));
  assert.ok(isNewer("1.0.0", "1.0.0-beta.1"));
  assert.ok(!isNewer("1.0.0-beta.1", "1.0.0"));
  assert.ok(!isNewer("garbage", "0.1.0"));
});

test("asset names match what electron-builder publishes", () => {
  assert.strictEqual(assetName("darwin", "arm64", "mac-zip"), "Frame-Control-mac-arm64.zip");
  assert.strictEqual(assetName("win32", "x64", "nsis"), "Frame-Control-Setup-x64.exe");
  assert.strictEqual(assetName("linux", "x64", "appimage"), "Frame-Control-linux-x86_64.AppImage");
  assert.strictEqual(assetName("linux", "arm64", "appimage"), "Frame-Control-linux-arm64.AppImage");
});

const base = { isPackaged: true, env: {}, exists: () => false, writable: () => true };

test("macOS updates in place only from a writable, non-translocated location", () => {
  const exe = "/Applications/Frame Control.app/Contents/MacOS/Frame Control";
  assert.strictEqual(macBundle(exe), "/Applications/Frame Control.app");
  assert.deepStrictEqual(updateMethod({ ...base, platform: "darwin", execPath: exe }),
                         { method: "mac-zip", bundle: "/Applications/Frame Control.app" });
  const dmg = "/Volumes/Frame Control 0.3.1/Frame Control.app/Contents/MacOS/Frame Control";
  assert.strictEqual(updateMethod({ ...base, platform: "darwin", execPath: dmg }).method, "manual");
  const trans = "/private/var/folders/x/AppTranslocation/ABC/d/Frame Control.app/Contents/MacOS/Frame Control";
  assert.strictEqual(updateMethod({ ...base, platform: "darwin", execPath: trans }).method, "manual");
  assert.strictEqual(updateMethod({ ...base, platform: "darwin", execPath: exe, writable: () => false }).method, "manual");
});

test("Windows needs the installer's copy; Linux needs an AppImage", () => {
  const exe = "C:\\Users\\a\\AppData\\Local\\Programs\\Frame Control\\Frame Control.exe";
  assert.strictEqual(updateMethod({ ...base, platform: "win32", execPath: exe, exists: () => true }).method, "nsis");
  assert.strictEqual(updateMethod({ ...base, platform: "win32", execPath: exe }).method, "manual");
  assert.strictEqual(updateMethod({ ...base, platform: "linux", execPath: "/opt/x", env: { APPIMAGE: "/home/a/F.AppImage" } }).method,
                     "appimage");
  assert.strictEqual(updateMethod({ ...base, platform: "linux", execPath: "/opt/Frame Control/frame-control" }).method, "manual");
  assert.strictEqual(updateMethod({ ...base, isPackaged: false, platform: "darwin", execPath: "x" }).method, "manual");
});

test("update.json assets always download from this repository's release", () => {
  const r = require("../updater").fromManifest({ version: "0.4.0", notes: "n", assets: [
    { name: "Frame-Control-mac-arm64.zip", url: "https://evil.example/x.zip", digest: "sha256:" + "a".repeat(64) }] });
  assert.strictEqual(r.assets[0].url, "https://github.com/saphid/frame-control/releases/download/v0.4.0/Frame-Control-mac-arm64.zip");
  assert.throws(() => require("../updater").fromManifest({ version: "nope", assets: [] }));
});

test("prepare refuses a release that isn't newer (no downgrades)", async () => {
  const { prepare } = require("../updater");
  const release = { version: "0.3.1", assets: [] };
  await assert.rejects(prepare(release, { method: "appimage", appImage: "/nonexistent/x" }, null, "0.4.0"), /isn't newer/);
  await assert.rejects(prepare(release, { method: "appimage", appImage: "/nonexistent/x" }, null, "0.3.1"), /isn't newer/);
});
