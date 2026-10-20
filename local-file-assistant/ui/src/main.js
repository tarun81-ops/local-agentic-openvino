import '@fontsource/archivo/400.css';
import '@fontsource/archivo/500.css';
import '@fontsource/archivo/700.css';
import '@fontsource/archivo/900.css';
import '@fontsource/space-mono/400.css';
import '@fontsource/space-mono/700.css';
import './style.css';

import { api, waitForBackend } from './api.js';
import { el, notice } from './components/common.js';
import { mascot } from './components/mascot.js';
import { applyCached, loadAppearance } from './appearance.js';
import { renderSidebar } from './components/sidebar.js';
import { render as renderChat } from './pages/chat.js';
import { render as renderFind } from './pages/find.js';
import { render as renderOrganize } from './pages/organize.js';
import { render as renderSearch } from './pages/search.js';
import { render as renderSettings } from './pages/settings-shell.js';
import { render as renderSetup } from './pages/setup.js';

applyCached();

const PAGES = { chat: renderChat, find: renderFind, search: renderSearch, organize: renderOrganize, settings: renderSettings, setup: renderSetup };
// Index and Memory now live inside Settings as tabs; old links ("Go to Index", the tray) still land there.
const MOVED = { index: 'files', memory: 'memory' };

const sidebar = document.getElementById('sidebar');
const content = document.getElementById('content');
const state = { page: 'chat', chatId: null, openChat: null, prefill: null, collections: [], recent: [], status: { connected: false, model: '' }, cleanup: null, ready: false };

function drawSidebar() {
  renderSidebar(sidebar, {
    active: state.page,
    onSelect: (id) => show(id),
    onNewChat: () => show('chat', { chatId: null }),
    recent: state.recent,
    onOpenChat: (id) => show('chat', { chatId: id }),
    onDeleteChat: deleteChat,
    onClearChats: clearChats,
    openChat: state.page === 'chat' ? state.openChat : null,
    status: state.status,
    collections: state.collections,
    onOpenCollection: (c) => show('search', { prefill: { collectionId: c.id } }),
  });
}

async function refreshCollections() {
  try {
    state.collections = (await api.collections()).collections.filter((c) => c.pinned);
  } catch { /* keep the list we have */ }
  drawSidebar();
}

async function refreshRecent(openId) {
  if (openId != null) state.openChat = openId; // a new chat got its id with its first reply
  try {
    state.recent = (await api.conversations()).conversations;
  } catch { /* keep the list we have */ }
  drawSidebar();
}

// Deleting the chat on screen starts a new one, so the next question can't go to a deleted chat.
async function deleteChat(id) {
  try {
    await api.deleteConversation(id);
  } catch { /* the list below shows what is left */ }
  state.recent = state.recent.filter((c) => c.id !== id);
  if (state.page === 'chat' && state.openChat === id) show('chat', { chatId: null });
  refreshRecent();
}

async function clearChats() {
  if (!confirm('Delete all your recent chats? This can’t be undone.')) return;
  const ids = state.recent.map((c) => c.id);
  await Promise.allSettled(ids.map((id) => api.deleteConversation(id)));
  state.recent = [];
  if (state.page === 'chat' && ids.includes(state.openChat)) show('chat', { chatId: null });
  refreshRecent();
}

// One-time move of the old browser-stored chats into the backend's conversation store.
async function importOldChats() {
  try {
    const old = JSON.parse(localStorage.getItem('lfa.chats') || '[]');
    if (old.length) await api.importConversations(old);
    localStorage.removeItem('lfa.chats');
  } catch { /* try again next launch */ }
}

async function refreshStatus() {
  try {
    const s = await api.settings();
    state.status = { connected: s.server.connected, model: s.model };
  } catch {
    state.status = { connected: false, model: '' };
  }
  drawSidebar();
}

function show(page, { chatId, prefill } = {}) {
  if (MOVED[page]) {
    prefill = { ...(prefill || {}), tab: MOVED[page] };
    page = 'settings';
  }
  state.page = PAGES[page] ? page : 'chat';
  if (!state.ready) {
    // boot() shows the requested page once the backend is up; keep the tab or search it asked for.
    state.prefill = prefill ?? null;
    return;
  }
  state.cleanup?.();
  if (state.page === 'chat') state.chatId = state.openChat = chatId ?? null;
  state.prefill = prefill ?? null;
  drawSidebar();
  content.dataset.page = state.page;
  state.cleanup =
    PAGES[state.page](content, {
      chatId: state.chatId,
      prefill: state.prefill,
      onCollectionsChanged: refreshCollections,
      navigate: show,
      onChatsChanged: refreshRecent,
      onStatusChanged: refreshStatus,
    }) || null;
}

async function boot() {
  drawSidebar();
  const title = [...'Local File Assistant'].map((ch, i) => el('span', { style: `--i:${i}` }, ch));
  content.replaceChildren(
    el('div', { class: 'boot splash' },
      el('div', { class: 'splash-burst', 'aria-hidden': 'true' }),
      mascot({ size: 132, mood: 'listening', label: 'Lumi, your local file assistant' }),
      el('div', { class: 'splash-title', role: 'heading', 'aria-level': '1', 'aria-label': 'Local File Assistant' }, title),
      el('div', { class: 'splash-sub' }, 'Starting the local backend… the first start can take a minute'),
    ),
  );
  // Keep the splash up briefly (about 0.9s) so the intro can play, even when the backend is already running.
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const [up] = await Promise.all([waitForBackend(), new Promise((r) => setTimeout(r, reduce ? 0 : 900))]);
  if (!up) {
    content.replaceChildren(
      el('div', { class: 'boot' },
        mascot({ size: 96, mood: 'worried', label: 'Lumi looks worried' }),
        notice('The local backend didn’t start. Run scripts\\setup.ps1 once, then restart the app.', 'error'),
      ),
    );
    return;
  }
  loadAppearance();
  await importOldChats();
  state.ready = true;
  try {
    if (!(await api.setupStatus()).done) state.page = 'setup'; // first launch: the setup wizard
  } catch { /* setup stays reachable from Settings */ }
  show(state.page, { prefill: state.prefill });
  refreshRecent();
  refreshCollections();
  refreshStatus();
  setInterval(refreshStatus, 30000);
}

window.lfa.onNavigate((page) => show(page));
boot();
