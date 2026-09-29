// The fake Steam client's JavaScript context ("SharedJSContext"), for fakesteam's
// DevTools server. fakesteam sends one JSON request per line on stdin:
//   {"id": N, "expression": "...", "awaitPromise": true, "steam": {...state...}}
// and gets one line back: {"id": N, "result": <CDP Runtime.evaluate result>, "steam": {...}}.
// The expression runs for real in a V8 context holding the objects below, so
// whatever JavaScript Frame Control sends (async functions, optional chaining,
// Map) behaves as it would in Steam's CEF; only the objects are stand-ins.
//
// Shapes copy what was seen through the Frame's DevTools port on 2026-09-25
// (docs/steam-games.md and docs/apks.md, BUILD_ID 20260922.6101926):
// appStore.allApps, a Map in downloadsStore.m_DownloadOverview keyed by client
// id with "0" for this machine, SteamClient.Installs.GetInstallManagerInfo /
// ContinueInstall, SteamClient.User.GetIPCountry, and SteamClient.Apps.AddShortcut
// + SetShortcutName / SetShortcutStartDir for non-Steam shortcuts.
'use strict';
const vm = require('vm');
const readline = require('readline');

const SHORTCUT_TYPE = 1073741824;  // app_type of a non-Steam shortcut, as steam_shortcuts.py filters

function newShortcutId(steam) {
  // Real shortcut ids are 32-bit with the top bit set (T3 Code's was 3130509679).
  steam.next_shortcut = (steam.next_shortcut || 0) + 1;
  return (0x80000000 + ((steam.next_shortcut * 2654435761) >>> 1)) >>> 0;
}

function build(steam) {
  const findShortcut = id => steam.shortcuts.find(s => s.appid === Number(id));
  const gameOverview = a => ({
    appid: a.appid, display_name: a.display_name, sort_as: a.display_name, app_type: a.app_type ?? 1,
    steam_hw_compat_category_packed: a.packed || 0, vr_supported: !!a.vr, vr_only: !!a.vr_only,
    size_on_disk: String(a.installed ? a.size : 0), minutes_playtime_forever: a.minutes || 0,
    rt_last_time_played: a.last_played || 0,
    local_per_client_data: {
      installed: !!a.installed, display_status: a.display_status ?? (a.installed ? 1 : 0),
      status_percentage: a.status_percentage ?? 0,
    },
  });
  const shortcutOverview = s => ({
    appid: s.appid, display_name: s.name, sort_as: s.name, app_type: SHORTCUT_TYPE,
    local_per_client_data: { installed: true, display_status: 1, status_percentage: 0 },
  });
  const allApps = () => [...steam.apps.map(gameOverview), ...steam.shortcuts.map(shortcutOverview)];

  const im = () => steam.install_manager;
  const queue = appid => {
    // State 14: Steam queued the download (Balatro on the Frame, docs/steam-games.md).
    const app = steam.apps.find(a => a.appid === appid);
    Object.assign(im(), { eInstallState: 14, currentAppID: appid });
    if (app) {
      steam.download = { update_appid: appid, update_state: 'Downloading', paused: false,
                         update_is_install: true, overall_percent_complete: 0,
                         overall_estimated_time_remaining_sec: 7, update_network_bytes_per_second: 9500000 };
    }
  };

  return {
    appStore: {
      get allApps() { return allApps(); },
      GetAppOverviewByAppID(id) { return allApps().find(a => a.appid === Number(id)) || null; },
    },
    downloadsStore: {
      get m_DownloadOverview() { return steam.download ? new Map([['0', { ...steam.download }]]) : new Map(); },
    },
    SteamClient: {
      Apps: {
        async AddShortcut(name, exe, launchOptions, cmdLine) {
          const appid = newShortcutId(steam);
          const base = String(exe).split('/').pop();
          steam.shortcuts.push({ appid, name: base, exe: String(exe), start_dir: '', icon: '',
                                 launch_options: String(launchOptions || ''), devkit_gameid: null });
          return appid;
        },
        SetShortcutName(id, name) { const s = findShortcut(id); if (s) s.name = String(name); },
        SetShortcutStartDir(id, dir) { const s = findShortcut(id); if (s) s.start_dir = String(dir); },
        SetShortcutIcon(id, icon) { const s = findShortcut(id); if (s) s.icon = String(icon); },
        SetShortcutExe(id, exe) { const s = findShortcut(id); if (s) s.exe = String(exe); },
        RemoveShortcut(id) {
          steam.shortcuts = steam.shortcuts.filter(s => s.appid !== Number(id));
          delete steam.compat_tools[String(id)];
        },
      },
      Installs: {
        async GetInstallManagerInfo() {
          const i = im();
          return { eInstallState: i.eInstallState, currentAppID: i.currentAppID,
                   nDiskSpaceRequired: i.nDiskSpaceRequired, nDiskSpaceAvailable: i.nDiskSpaceAvailable,
                   eAppError: i.eAppError ?? 0, errorDetail: i.errorDetail ?? '' };
        },
        // Broforce stopped at state 7 and ContinueInstall() queued it (docs/steam-games.md).
        ContinueInstall() { if (im().eInstallState === 7) queue(im().currentAppID); },
        CancelInstall() { Object.assign(im(), { eInstallState: 16 }); },
        // Calling OpenInstallWizard directly did nothing on the Frame: the state stayed 0.
        OpenInstallWizard() {},
      },
      User: {
        async GetIPCountry() { return steam.country; },
      },
    },
  };
}

// What CDP's Runtime.evaluate returns with returnByValue.
function remote(value) {
  if (value === undefined) return { type: 'undefined' };
  if (value === null) return { type: 'object', subtype: 'null', value: null };
  const type = typeof value;
  if (type === 'object') return { type: 'object', value: JSON.parse(JSON.stringify(value)) };
  if (type === 'function') return { type: 'function', description: String(value) };
  return { type, value, description: String(value) };
}

function exception(err, inPromise) {
  // Chrome puts the stack in description; its first line is enough here.
  const description = err && err.name ? `${err.name}: ${err.message}` : String(err);
  return {
    result: { type: 'object', subtype: 'error', className: (err && err.name) || 'Error', description },
    exceptionDetails: {
      exceptionId: 1, text: inPromise ? 'Uncaught (in promise)' : 'Uncaught', lineNumber: 0, columnNumber: 0,
      exception: { type: 'object', subtype: 'error', className: (err && err.name) || 'Error', description },
    },
  };
}

async function evaluate(req) {
  const steam = req.steam;
  const ctx = vm.createContext(build(steam));
  let value;
  try {
    value = vm.runInContext(req.expression, ctx, { timeout: 5000 });
  } catch (err) {
    return exception(err, false);
  }
  if (req.awaitPromise && value && typeof value.then === 'function') {
    try {
      value = await value;
    } catch (err) {
      return exception(err, true);
    }
  }
  try {
    return { result: remote(value) };
  } catch (err) {
    return exception(err, false);
  }
}

// One request at a time: fakesteam holds the state lock around each.
const rl = readline.createInterface({ input: process.stdin });
let chain = Promise.resolve();
rl.on('line', line => {
  chain = chain.then(async () => {
    const req = JSON.parse(line);
    const result = await evaluate(req);
    process.stdout.write(JSON.stringify({ id: req.id, result, steam: req.steam }) + '\n');
  });
});
