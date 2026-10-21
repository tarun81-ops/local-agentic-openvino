// One client for every page and the overlay. The port and token come from Electron's main
// process (a free port and a fresh token each launch), never from the page itself.

let configPromise = null;

function config() {
  if (!configPromise) configPromise = window.lfa.config();
  return configPromise;
}

async function base() {
  const { port } = await config();
  return `http://127.0.0.1:${port}`;
}

export class ApiError extends Error {
  constructor(status, detail) {
    const text = typeof detail === 'string' ? detail : detail?.message || `Request failed (${status})`;
    super(text);
    this.status = status;
    this.detail = detail;
  }
}

export async function request(path, { method = 'GET', body, query } = {}) {
  const { token } = await config();
  const url = new URL((await base()) + path);
  for (const [k, v] of Object.entries(query || {})) if (v != null && v !== '') url.searchParams.set(k, v);
  let response;
  try {
    response = await fetch(url, {
      method,
      headers: { Authorization: `Bearer ${token}`, ...(body ? { 'Content-Type': 'application/json' } : {}) },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(0, "Can't reach the local backend. It may still be starting.");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(response.status, data.detail);
  return data;
}

/** Resolves once the backend answers /health (it takes a few seconds to start). */
export async function waitForBackend({ timeoutMs = 120000, onWaiting } = {}) { // the first start after an install can take a minute
  const started = Date.now();
  for (;;) {
    try {
      const r = await fetch((await base()) + '/health');
      if (r.ok) return true;
    } catch { /* not up yet */ }
    if (Date.now() - started > timeoutMs) return false;
    onWaiting?.();
    await new Promise((res) => setTimeout(res, 400));
  }
}

/** POST /chat and dispatch its server-sent events: route, results, token, conversation, done, error. */
export function chat({ message, conversationId, workspaceId, mode, root, context, style, language }, handlers, signal) {
  return stream('/chat', { message, conversation_id: conversationId ?? null, workspace_id: workspaceId ?? null, mode: mode || 'auto', root: root || null, context: context || null, style: style || null, language: language || null }, handlers, signal);
}

/** POST /setup/model/pull: progress, then done / failed / cancelled. Aborting only stops listening; api.setupCancel() stops the download. */
export function setupPull(handlers, signal) {
  return stream('/setup/model/pull', {}, handlers, signal);
}

/** POSTs body and calls handlers[event](data) for each server-sent event; handlers.error on a network or HTTP error. */
async function stream(path, body, handlers, signal) {
  const { token } = await config();
  let response;
  try {
    response = await fetch((await base()) + path, {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal,
    });
  } catch (err) {
    if (err.name === 'AbortError') return;
    handlers.error?.({ detail: "Can't reach the local backend. It may still be starting." });
    return;
  }
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    handlers.error?.({ detail: data.detail || `Request failed (${response.status})` });
    return;
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let cut;
      while ((cut = buffer.indexOf('\n\n')) >= 0) {
        const block = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 2);
        const event = /^event: (.*)$/m.exec(block)?.[1];
        const data = /^data: (.*)$/m.exec(block)?.[1];
        if (event && data) handlers[event]?.(JSON.parse(data));
      }
    }
  } catch (err) {
    if (err.name !== 'AbortError') handlers.error?.({ detail: 'The answer stream was interrupted.' });
  }
}

export const api = {
  search: (q, root) => request('/search', { query: { q, root } }),
  settings: () => request('/settings'),
  // First-run setup (backend/app/api/routes_setup.py); the download itself is setupPull() above.
  setupStatus: () => request('/setup/status'),
  setupDone: () => request('/setup/done', { method: 'POST' }),
  setupCancel: () => request('/setup/model/pull/cancel', { method: 'POST' }),
  setModel: (model) => request('/settings/model', { method: 'PUT', body: { model } }),
  roots: () => request('/files/roots'),
  files: (root) => request('/files', { query: { root } }),
  commonFolders: () => request('/files/common'),
  addCommonFolders: (keys) => request('/files/common', { method: 'POST', body: { keys } }),
  setAutoAccess: (enabled) => request('/files/common/auto', { method: 'PUT', body: { enabled } }),
  addRoot: (folder) => request('/files/roots', { method: 'POST', body: { folder } }),
  removeRoot: (folder) => request('/files/roots/remove', { method: 'POST', body: { folder } }),
  rescan: (folder) => request('/files/index', { method: 'POST', body: { folder } }),
  saveFolderProfile: (folder, profile) => request('/files/roots/profile', { method: 'PUT', body: { folder, profile } }),
  collections: () => request('/collections'),
  createCollection: (body) => request('/collections', { method: 'POST', body }),
  updateCollection: (id, patch) => request(`/collections/${id}`, { method: 'PATCH', body: patch }),
  deleteCollection: (id) => request(`/collections/${id}`, { method: 'DELETE' }),
  recentFiles: () => request('/files/recent'),
  indexStatus: () => request('/files/index/status'),
  cancelIndex: () => request('/files/index/cancel', { method: 'POST' }),
  // Whole-PC access: file-name catalog of the drives the user switched on (backend/app/api/routes_pc.py).
  pcStatus: () => request('/pc'),
  pcSettings: (body) => request('/pc/settings', { method: 'PUT', body }),
  pcScan: () => request('/pc/scan', { method: 'POST' }),
  pcStop: () => request('/pc/stop', { method: 'POST' }),
  pcForget: () => request('/pc/forget', { method: 'POST' }),
  // query: the search that led here (optional); the backend learns which files you open for which searches
  open: (path, query) => request('/files/open', { method: 'POST', body: { path, query: query || '' } }),
  find: (q, limit) => request('/find', { query: { q, limit } }),
  findOpen: (path, query) => request('/find/open', { method: 'POST', body: { path, query: query || '' } }),
  findReveal: (path) => request('/find/reveal', { method: 'POST', body: { path } }),
  findSimilar: (path, limit) => request('/find/similar', { query: { path, limit } }),
  tidyFolders: () => request('/organize/tidy/folders'),
  tidy: (folder, largeMb, oldDays, checks) => request('/organize/tidy', { method: 'POST', body: { folder: folder || null, large_mb: largeMb || 100, old_days: oldDays || 90, ...(checks ? { checks } : {}) } }),
  tidyStart: (folder, largeMb, oldDays, checks) => request('/organize/tidy/start', { method: 'POST', body: { folder: folder || null, large_mb: largeMb || 100, old_days: oldDays || 90, ...(checks ? { checks } : {}) } }),
  tidyJob: (id) => request(`/organize/tidy/jobs/${id}`),
  tidyCancel: (id) => request(`/organize/tidy/jobs/${id}/cancel`, { method: 'POST' }),
  places: () => request('/organize/places'),
  browse: (path, hidden) => request('/organize/browse', { query: { path, hidden: hidden ? 'true' : '' } }),
  orgOpen: (path) => request('/organize/open', { method: 'POST', body: { path } }),
  orgReveal: (path) => request('/organize/reveal', { method: 'POST', body: { path } }),
  apply: (actions, { skipMissing = false } = {}) => request('/organize/apply', { method: 'POST', body: { actions, ...(skipMissing ? { skip_missing: true } : {}) } }),
  undo: (batch) => request('/organize/undo', { method: 'POST', body: { batch: batch || null } }),
  history: () => request('/organize/history'),
  bin: () => request('/organize/bin'),
  binPurge: (ids) => request('/organize/bin/purge', { method: 'POST', body: { ids } }),
  binRestore: (ids) => request('/organize/bin/restore', { method: 'POST', body: { ids } }),
  binEmpty: () => request('/organize/bin/empty', { method: 'POST' }),
  memories: (q, status) => request('/memory', { query: { q, status } }),
  personalize: () => request('/personalize'),
  performance: () => request('/personalize/performance'),
  savePersonalize: (patch) => request('/personalize', { method: 'PUT', body: patch }),
  addMemory: (text) => request('/memory', { method: 'POST', body: { text } }),
  updateMemory: (id, patch) => request(`/memory/${id}`, { method: 'PATCH', body: patch }),
  deleteMemory: (id) => request(`/memory/${id}`, { method: 'DELETE' }),
  wipeMemory: () => request('/memory/wipe', { method: 'POST' }),
  exportMemory: () => request('/memory/export'),
  parseTask: (text, kind) => request('/tasks/parse', { method: 'POST', body: { text, kind } }),
  createTask: (body) => request('/tasks', { method: 'POST', body }),
  createEvent: (body) => request('/events', { method: 'POST', body }),
  approveAction: (id) => request(`/actions/${id}/approve`, { method: 'POST' }),
  rejectAction: (id) => request(`/actions/${id}/reject`, { method: 'POST' }),
  undoAction: (id) => request(`/actions/${id}/undo`, { method: 'POST' }),
  formsSuggest: ({ label, pageTitle, accept }) => request('/forms/suggest', { method: 'POST', body: { label, page_title: pageTitle || '', accept: accept || null } }),
  proactiveSettings: () => request('/proactive/settings'),
  saveProactive: (body) => request('/proactive/settings', { method: 'PUT', body }),
  proactivePreview: () => request('/proactive/preview'),
  voiceStatus: () => request('/voice/status'),
  downloadVoice: () => request('/voice/download', { method: 'POST' }),
  stt: async (pcm) => {
    const { token } = await config();
    const r = await fetch((await base()) + '/voice/stt', { method: 'POST', headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/octet-stream' }, body: pcm });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new ApiError(r.status, data.detail);
    return data;
  },
  tts: async (text) => {
    const { token } = await config();
    const r = await fetch((await base()) + '/voice/tts', { method: 'POST', headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' }, body: JSON.stringify({ text }) });
    if (!r.ok) throw new ApiError(r.status, (await r.json().catch(() => ({}))).detail);
    return r.blob();
  },
  rateAnswer: (msgId, rating) => request('/learning/feedback', { method: 'POST', body: { msg_id: msgId, rating } }),
  learningStats: () => request('/learning/stats'),
  wipeLearning: () => request('/learning/wipe', { method: 'POST' }),
  exportLearning: async () => {
    const { token } = await config();
    const r = await fetch((await base()) + '/learning/export', { headers: { Authorization: `Bearer ${token}` } });
    if (!r.ok) throw new ApiError(r.status, 'Export failed');
    return r.text();
  },
  studyGenerate: (path, kind, count) => request('/study/generate', { method: 'POST', body: { path, kind, count } }),
  studyCsv: async (items) => {
    const { token } = await config();
    const r = await fetch((await base()) + '/study/export', { method: 'POST', headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' }, body: JSON.stringify({ items }) });
    if (!r.ok) throw new ApiError(r.status, 'Export failed');
    return r.text();
  },
  conversations: (workspace) => request('/conversations', { query: { workspace } }), // no workspace = "All files"
  // Workspaces (backend/app/api/routes_workspaces.py): {name, roots ([] = whole PC), answer_style}
  workspaces: () => request('/workspaces'),
  createWorkspace: (body) => request('/workspaces', { method: 'POST', body }),
  deleteWorkspace: (id) => request(`/workspaces/${id}`, { method: 'DELETE' }),
  conversation: (id) => request(`/conversations/${id}`),
  deleteConversation: (id) => request(`/conversations/${id}`, { method: 'DELETE' }),
  importConversations: (chats) => request('/conversations/import', { method: 'POST', body: { chats } }),
};
