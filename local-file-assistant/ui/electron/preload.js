const { contextBridge, ipcRenderer } = require('electron');

// The only bridge between the pages and Electron. Pages get the backend's port and token
// from here and can ask for a folder picker; they can't reach Node or the filesystem.
contextBridge.exposeInMainWorld('lfa', {
  config: () => ipcRenderer.invoke('app:config'),
  shortcut: () => ipcRenderer.invoke('app:shortcut'),
  pickFolder: () => ipcRenderer.invoke('dialog:pick-folder'),
  // Updates (electron/updates.js): {enabled, supported, ready}, switch on/off, restart into a downloaded update.
  updates: () => ipcRenderer.invoke('updates:get'),
  setUpdates: (enabled) => ipcRenderer.invoke('updates:set', Boolean(enabled)),
  installUpdate: () => ipcRenderer.invoke('updates:install'),
  onUpdateReady: (cb) => ipcRenderer.on('update:ready', (_e, version) => cb(version)),
  openLink: (url) => ipcRenderer.invoke('app:open-link', url), // only the download pages listed in main.js
  getLaunchAtStartup: () => ipcRenderer.invoke('app:get-login'),
  setLaunchAtStartup: (enabled) => ipcRenderer.invoke('app:set-login', enabled),
  openMain: (page) => ipcRenderer.send('main:open', page),
  hideOverlay: () => ipcRenderer.send('overlay:hide'),
  resizeOverlay: (height) => ipcRenderer.send('overlay:resize', height),
  onNavigate: (cb) => ipcRenderer.on('navigate', (_e, page) => cb(page)),
  // cb gets what was in front when summoned: {app, title, selection}, or null
  onOverlayShown: (cb) => ipcRenderer.on('overlay:shown', (_e, context) => cb(context)),
  // ATTACH mode: tell main which files may be dragged out, then start a native drag of one
  allowDrag: (paths, replace = true) => ipcRenderer.send('overlay:drag-allow', { paths, replace }),
  startDrag: (file) => ipcRenderer.send('overlay:start-drag', file),
  pinOverlay: (pinned) => ipcRenderer.send('overlay:pin', Boolean(pinned)),
  insertText: (text) => ipcRenderer.invoke('overlay:insert', text),
  setShortcut: (accelerator, which = 'toggle') => ipcRenderer.invoke('shortcuts:set', { which, accelerator }),
  shortcuts: () => ipcRenderer.invoke('app:shortcuts'),
  onVoiceToggle: (cb) => ipcRenderer.on('overlay:voice-toggle', () => cb()),
});
