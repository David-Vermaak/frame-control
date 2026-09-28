// Lets the page read this computer's clipboard through Electron, so sending it
// to the Frame needs no pbpaste, PowerShell, xclip or wl-clipboard. Also tells
// the page where a dropped file or folder lives, so a folder can be sideloaded
// as a title without zipping it (the local server reads it from there).
// It can open Set Up Connection when the headset can't be reached, and keeps the
// Frame menu's list of headsets up to date.
// It also receives frame-control://install links (docs/web-install.md): only
// what the link asked for, never an install; the page asks the user first.
const { contextBridge, ipcRenderer, webUtils } = require("electron");

contextBridge.exposeInMainWorld("frameApp", {
  readClipboard: () => ipcRenderer.invoke("clipboard:read"),
  setUpConnection: () => ipcRenderer.invoke("connection:setup"),
  // The Frame menu's headset switcher: the page tells it the headsets, and hears picks.
  devicesChanged: (list) => ipcRenderer.send("devices:changed", list),
  onUseDevice: (cb) => {
    ipcRenderer.removeAllListeners("use-device");
    ipcRenderer.on("use-device", (_e, id) => cb(String(id)));
  },
  pathForFile: (file) => { try { return webUtils.getPathForFile(file) || ""; } catch { return ""; } },
  onInstallLink: (cb) => {
    ipcRenderer.removeAllListeners("install-link");
    ipcRenderer.on("install-link", (_e, req) => cb({ kind: req.kind, target: req.target }));
    ipcRenderer.send("install-link:ready");
  },
});
