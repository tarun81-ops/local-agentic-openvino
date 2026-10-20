import { el, svg, ICONS } from './common.js';
import { mascot } from './mascot.js';

// Everyday pages first, Settings under the rule. SEARCH is folded into FIND FILE (which also
// searches contents); its page stays reachable from pinned collections. Index and Memory are tabs
// inside Settings.
export const NAV_ITEMS = [
  { id: 'chat', n: '01', label: 'Chat' },
  { id: 'find', n: '02', label: 'Find file' },
  { id: 'organize', n: '03', label: 'Organize' },
  { id: 'settings', n: '04', label: 'Settings', setup: true },
];
const RULE_HEIGHT = 17; // the divider between the two groups (1px + 8px margins)
const NAV_ALIAS = { search: 'find', index: 'settings', memory: 'settings' }; // pages folded into another light it up

/**
 * @param {object} p
 * @param {string} p.active  current page id
 * @param {(id:string)=>void} p.onSelect
 * @param {()=>void} p.onNewChat
 * @param {Array<{id:string,title:string}>} p.recent  recent chats (shown on the Chat page)
 * @param {(id:string)=>void} p.onOpenChat
 * @param {(id:number)=>void} [p.onDeleteChat]  removes one chat from RECENT (and from the app)
 * @param {()=>void} [p.onClearChats]  removes every chat
 * @param {number|null} [p.openChat]  the chat on screen, marked in the list
 * @param {{model?:string, connected?:boolean}} p.status
 */
const ROW_HEIGHT = 40;

export function renderSidebar(root, { active, onSelect, onNewChat, recent, onOpenChat, onDeleteChat, onClearChats, openChat = null, status, collections = [], onOpenCollection }) {
  const current = NAV_ALIAS[active] || active;
  const activeIndex = Math.max(0, NAV_ITEMS.findIndex((item) => item.id === current));
  const firstSetup = NAV_ITEMS.findIndex((item) => item.setup);
  const indicatorTop = activeIndex * ROW_HEIGHT + (activeIndex >= firstSetup ? RULE_HEIGHT : 0);
  root.replaceChildren(
    el('div', { class: 'brand' }, mascot({ size: 34, flat: true }), el('div', { class: 'brand-name' }, 'Local File Assistant')),
    el('button', { type: 'button', class: `new-chat ${active === 'chat' ? 'new-chat-primary' : ''}`, onclick: onNewChat }, '+ New chat'),
    el(
      'nav',
      { class: 'nav', 'aria-label': 'Main' },
      el('div', { class: 'nav-indicator', style: `top:${indicatorTop}px` }),
      NAV_ITEMS.flatMap((item, i) => [
        i === firstSetup ? el('div', { class: 'nav-rule', 'aria-hidden': 'true' }) : '',
        el(
          'button',
          {
            type: 'button',
            class: `nav-item ${item.id === current ? 'active' : ''}`,
            'aria-current': item.id === current ? 'page' : null,
            onclick: () => onSelect(item.id),
          },
          el('span', { class: 'nav-n' }, item.n),
          el('span', { class: 'nav-label' }, item.label),
        ),
      ]),
    ),
    collections.length
      ? el(
          'div',
          { class: 'recent' },
          el('div', { class: 'recent-h' }, 'Collections'),
          collections.slice(0, 8).map((c) => el('button', { type: 'button', class: 'recent-item', title: c.query, onclick: () => onOpenCollection?.(c) }, c.name)),
        )
      : '',
    active === 'chat' && recent.length
      ? el(
          'div',
          { class: 'recent' },
          el(
            'div',
            { class: 'recent-head' },
            el('div', { class: 'recent-h' }, 'Recent'),
            onClearChats ? el('button', { type: 'button', class: 'recent-clear', title: 'Delete every chat', onclick: onClearChats }, 'Clear all') : '',
          ),
          recent.slice(0, 8).map((c) =>
            el(
              'div',
              { class: `recent-row ${c.id === openChat ? 'current' : ''}` },
              el('button', { type: 'button', class: 'recent-item', title: c.title, onclick: () => onOpenChat(c.id) }, c.title),
              onDeleteChat ? el('button', { type: 'button', class: 'recent-del', 'aria-label': `Delete chat: ${c.title}`, title: 'Delete this chat', onclick: () => onDeleteChat(c.id) }, svg(ICONS.close, 12)) : '',
            ),
          ),
        )
      : '',
    el(
      'div',
      { class: `status-box ${status.connected ? '' : 'status-off'}`, role: 'status', 'aria-live': 'polite' },
      el('div', { class: 'status-dot' }),
      el(
        'div',
        { class: 'status-text', title: status.connected ? `OpenVINO — ${status.model}` : 'OpenVINO Model Server is not running' },
        status.connected ? (status.model || '') : 'OpenVINO — not running',
      ),
      el('div', { class: 'tag-local' }, 'Local'),
    ),
  );
}
