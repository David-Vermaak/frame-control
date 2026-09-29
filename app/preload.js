// Lets the page read this computer's clipboard through Electron, so sending it
// to the Frame needs no pbpaste, PowerShell, xclip or wl-clipboard. Also tells
// the page where a dropped file or folder lives, so a folder can be sideloaded
// as a title without zipping it (the local server reads it from there).
// It can open Set Up Connection when the headset can't be reached.
// It also receives frame-control://install links (docs/web-install.md): only
// what the link asked for, never an install; the page asks the user first.
// And it passes update state both ways: see app/updater.js.
const { contextBridge, ipcRenderer, webUtils } = require("electron");

contextBridge.exposeInMainWorld("frameApp", {
  readClipboard: () => ipcRenderer.invoke("clipboard:read"),
  setUpConnection: () => ipcRenderer.invoke("connection:setup"),
  pathForFile: (file) => { try { return webUtils.getPathForFile(file) || ""; } catch { return ""; } },
  // Updates (app/updater.js): the page shows a banner and an Update button.
  update: {
    get: () => ipcRenderer.invoke("update:get"),
    check: () => ipcRenderer.invoke("update:check"),
    install: () => ipcRenderer.invoke("update:install"),
    onState: (cb) => {
      ipcRenderer.removeAllListeners("update:state");
      ipcRenderer.on("update:state", (_e, s) => cb(s));
    },
  },
  // Help → Report a Problem… opens the page's report dialog (ui/frame_report.py).
  onReportProblem: (cb) => {
    ipcRenderer.removeAllListeners("report:open");
    ipcRenderer.on("report:open", () => cb());
  },
  onInstallLink: (cb) => {
    ipcRenderer.removeAllListeners("install-link");
    ipcRenderer.on("install-link", (_e, req) => cb({ kind: req.kind, target: req.target }));
    ipcRenderer.send("install-link:ready");
  },
});
