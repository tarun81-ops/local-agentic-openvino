import { el } from '../components/common.js';
import { render as renderFiles } from './files.js';
import { render as renderMemory } from './memory.js';
import { render as renderPlugins } from './plugins.js';
import { render as renderPrivacy } from './privacy.js';
import { render as renderGeneral } from './settings.js';

// Settings: the general settings, Files & PC access (the PC access switch and drives, Tidy-up undo
// history) and Memory (what you asked the assistant to remember). Each tab is its own page module
// rendered into the pane.
export const TABS = [
  ['general', 'General', renderGeneral],
  ['files', 'Files & PC access', renderFiles],
  ['memory', 'Memory', renderMemory],
  ['plugins', 'Plug-in tools', renderPlugins],
  ['privacy', 'How it works & privacy', renderPrivacy],
];

let lastTab = 'general'; // reopening Settings returns to the tab you were on

export function render(container, opts = {}) {
  const pane = el('div', { class: 'settings-pane', role: 'tabpanel' });
  const strip = el('div', { class: 'tabs settings-tabs', role: 'tablist', 'aria-label': 'Settings sections' });
  container.replaceChildren(strip, pane);
  let cleanup = null;

  function select(id, focus = false) {
    const tab = TABS.find(([k]) => k === id) || TABS[0];
    lastTab = tab[0];
    cleanup?.();
    strip.replaceChildren(
      ...TABS.map(([k, label]) =>
        el('button', {
          type: 'button', class: 'tab', role: 'tab', id: `settings-tab-${k}`,
          'aria-selected': String(k === tab[0]), tabindex: k === tab[0] ? '0' : '-1',
          onclick: () => select(k),
        }, label)),
    );
    pane.setAttribute('aria-labelledby', `settings-tab-${tab[0]}`);
    cleanup = tab[2](pane, opts) || null;
    if (focus) strip.querySelector('[aria-selected="true"]')?.focus();
  }

  strip.addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
    const i = TABS.findIndex(([k]) => k === lastTab);
    const next = TABS[(i + (e.key === 'ArrowRight' ? 1 : TABS.length - 1)) % TABS.length][0];
    select(next, true);
  });

  select(opts.prefill?.tab || lastTab);
  return () => cleanup?.();
}
