// Chat page. The composer works like Claude desktop's: a centred box on a fresh chat that docks to
// the bottom once the conversation starts. Perplexity-style touches: a focus switch (AUTO / FILES /
// CHAT), starter cards, and RELATED follow-ups under each answer. "/" opens commands, "@" picks a folder.
import { api } from '../api.js';
import { ask, turnView, turnsFromMessages } from '../components/chat-log.js';
import { el, fileName, notice, svg, ICONS } from '../components/common.js';
import { mascot } from '../components/mascot.js';
import { micButton, readAloud, speak, stopSpeaking } from '../components/mic.js';

const STYLES = [['', 'Default'], ['concise', 'Concise'], ['detailed', 'Detailed'], ['study', 'Study'], ['simple', 'Simple']];
const LANGS = [['', 'Default'], ['english', 'English'], ['hinglish', 'Hinglish']];
const MODES = [
  ['auto', 'Auto', 'Files when they help, otherwise a general answer'],
  ['files', 'Files', 'Only answer from your indexed files, with citations'],
  ['chat', 'Chat', 'General answer, your files are not searched'],
];

const ICON = {
  folder: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h7l2 2h9v11H3z"/></svg>',
  tune: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="4" y1="7" x2="20" y2="7"/><line x1="4" y1="17" x2="20" y2="17"/><rect x="7" y="4.5" width="4" height="5" fill="currentColor"/><rect x="13" y="14.5" width="4" height="5" fill="currentColor"/></svg>',
  stop: '<svg viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" fill="currentColor"/></svg>',
  copy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="8" y="8" width="12" height="12"/><path d="M16 8V4H4v12h4"/></svg>',
  retry: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 12a8 8 0 1 0 2.5-5.8"/><polyline points="4 3 4 8 9 8"/></svg>',
  plus: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>',
  model: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="5" y="5" width="14" height="14"/><rect x="9" y="9" width="6" height="6" fill="currentColor"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/></svg>',
};


// "/" commands. Each either changes a setting or starts a message.
const COMMANDS = [
  ['/files', 'Answer only from your files', { mode: 'files' }],
  ['/chat', 'General answer, no file search', { mode: 'chat' }],
  ['/auto', 'Let the assistant decide', { mode: 'auto' }],
  ['/concise', 'Short answers', { style: 'concise' }],
  ['/detailed', 'Long, thorough answers', { style: 'detailed' }],
  ['/study', 'Explain like study notes', { style: 'study' }],
  ['/simple', 'Plain, simple words', { style: 'simple' }],
  ['/hinglish', 'Answer in Hinglish', { lang: 'hinglish' }],
  ['/english', 'Answer in English', { lang: 'english' }],
  ['/remind', 'Set a reminder', { text: 'Remind me to ' }],
  ['/summarize', 'Summarize a file', { text: 'Summarize ' }],
  ['/find', 'Find a file by describing it', { text: '/find ' }],
  ['/tidy', 'Clean up Downloads', { go: 'organize' }],
  ['/new', 'Start a new chat', { newChat: true }],
];

const MODE_KEY = 'lfa.chatMode';
const store = {
  get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* not remembered */ } },
};

function greeting() {
  const h = new Date().getHours();
  return h < 5 ? 'Working late' : h < 12 ? 'Good morning' : h < 17 ? 'Good afternoon' : 'Good evening';
}

/** A small menu anchored above a button. Closes on Escape, outside click, or picking an item. */
function popover(anchor, build) {
  const pop = el('div', { class: 'pop', role: 'menu' });
  let open = false;
  const close = () => {
    if (!open) return;
    open = false;
    pop.remove();
    anchor.setAttribute('aria-expanded', 'false');
    document.removeEventListener('pointerdown', outside, true);
  };
  const outside = (e) => { if (!pop.contains(e.target) && !anchor.contains(e.target)) close(); };
  const show = () => {
    pop.replaceChildren(...build(close));
    anchor.parentElement.append(pop);
    open = true;
    anchor.setAttribute('aria-expanded', 'true');
    document.addEventListener('pointerdown', outside, true);
    (pop.querySelector('[aria-checked="true"]') || pop.querySelector('button'))?.focus();
  };
  anchor.setAttribute('aria-haspopup', 'menu');
  anchor.setAttribute('aria-expanded', 'false');
  anchor.addEventListener('click', () => (open ? close() : show()));
  pop.addEventListener('keydown', (e) => {
    const items = [...pop.querySelectorAll('button')];
    const i = items.indexOf(document.activeElement);
    if (e.key === 'Escape') { e.stopPropagation(); close(); anchor.focus(); }
    if (e.key === 'ArrowDown') { e.preventDefault(); items[(i + 1) % items.length]?.focus(); }
    if (e.key === 'ArrowUp') { e.preventDefault(); items[(i - 1 + items.length) % items.length]?.focus(); }
  });
  return { close, isOpen: () => open };
}

const popHead = (text) => el('div', { class: 'pop-h' }, text);
const popItem = (label, { checked, sub, onPick, close }) =>
  el(
    'button',
    { type: 'button', class: 'pop-item', role: checked == null ? 'menuitem' : 'menuitemradio', 'aria-checked': checked == null ? null : String(checked), onclick: () => { onPick(); close(); } },
    el('span', { class: 'pop-label' }, label),
    sub ? el('span', { class: 'pop-sub' }, sub) : '',
    checked ? svg(ICONS.check, 11) : '',
  );

/**
 * @param {HTMLElement} container
 * @param {{chatId?:number, onChatsChanged:()=>void, navigate?:(page:string, o?:object)=>void}} opts
 */
export function render(container, { chatId, onChatsChanged, navigate, prefill, workspaceId }) {
  let convId = chatId ?? null; // set by the first reply of a new chat
  let busy = null; // AbortController while an answer streams
  let turns = 0;
  let lastQuestion = '';
  let roots = [];
  let models = [];
  const pick = { mode: MODES.some(([v]) => v === store.get(MODE_KEY)) ? store.get(MODE_KEY) : 'auto', root: '', style: '', lang: '', model: '' };

  // ---------- the composer ----------
  const input = el('textarea', { id: 'ask-input', rows: 1, placeholder: 'Ask about your files, or anything else…', autocomplete: 'off', 'aria-label': 'Message', spellcheck: 'true' });
  const send = el('button', { type: 'button', class: 'send', 'aria-label': 'Send', title: 'Send (Enter)' }, svg(ICONS.send, 15));
  const mic = micButton({
    onText: (text) => {
      input.value = text;
      submit();
    },
    onError: (message) => (turns ? log.append(notice(message, 'error')) : flash(message)),
  });
  mic.button.classList.add('mic-flat');

  const plusBtn = el('button', { type: 'button', class: 'ask-icon', 'aria-label': 'Options', title: 'Options' }, svg(ICON.plus, 14));
  const chips = el('div', { class: 'ask-tags' }); // active non-default picks, each removable
  const suggest = el('div', { class: 'suggest', role: 'listbox', hidden: true }); // "/" and "@" menu

  const box = el(
    'div',
    { class: 'ask-box' },
    suggest,
    chips,
    input,
    el(
      'div',
      { class: 'ask-bar' },
      el('div', { class: 'ask-left' }, el('span', { class: 'pop-anchor' }, plusBtn)),
      el('div', { class: 'ask-right' }, mic.button, send),
    ),
  );
  const composer = el('div', { class: 'composer-v2' }, box);

  // ---------- layout: hero (fresh chat) or log + dock ----------
  const log = el('div', { class: 'chat-log', id: 'chat-log', 'aria-live': 'polite' });
  const heroSlot = el('div', { class: 'hero-slot' });
  const heroMsg = el('div', { class: 'hero-flash' });
  const hero = el(
    'div',
    { class: 'chat-hero' },
    el('div', { class: 'hero-brand' }, mascot({ size: 52, label: 'Lumi' }), el('div', {}, el('h1', { class: 'hero-title' }, `${greeting()}.`), el('div', { class: 'hero-sub' }, 'Everything stays on this PC.'))),
    heroSlot,
    heroMsg,
  );
  const dock = el('div', { class: 'composer-wrap composer-dock' });
  const titleEl = el('h1', { class: 'page-title' }, 'New chat');
  const meta = el('div', { class: 'page-meta' });
  const head = el('header', { class: 'page-head' }, titleEl, meta);
  container.replaceChildren(head, hero, log, dock);

  const flash = (message) => heroMsg.replaceChildren(message ? notice(message, 'error') : '');

  function setDocked(docked) {
    container.classList.toggle('chat-docked', docked);
    hero.hidden = docked;
    head.hidden = !docked; // a fresh chat is just the greeting and the box
    log.hidden = !docked;
    dock.hidden = !docked;
    (docked ? dock : heroSlot).append(composer);
  }

  // ---------- picks (mode, folder, style, language, model) ----------
  function setPick(patch) {
    Object.assign(pick, patch);
    if (patch.mode) store.set(MODE_KEY, patch.mode);
    if ('model' in patch && patch.model) {
      api.setModel(patch.model).catch((e) => (turns ? log.append(notice(e.message, 'error')) : flash(e.message)));
    }
    drawPicks();
  }

  const label = (list, v) => list.find(([k]) => k === v)?.[1] || 'Default';
  const rootName = (p) => (fileName(p) || p);

  function drawPicks() {
    // Only what differs from the defaults is shown, as small removable tags.
    const tags = [];
    if (pick.mode !== 'auto') tags.push([pick.mode === 'files' ? 'Files only' : 'Chat only', { mode: 'auto' }]);
    if (pick.root && pick.mode !== 'chat') tags.push(['In ' + rootName(pick.root), { root: '' }]);
    if (pick.style) tags.push([label(STYLES, pick.style), { style: '' }]);
    if (pick.lang) tags.push([label(LANGS, pick.lang), { lang: '' }]);
    chips.replaceChildren(...tags.map(([t, reset]) => el('span', { class: 'ask-tag' }, t, el('button', { type: 'button', class: 'ask-tag-x', 'aria-label': `Remove ${t}`, onclick: () => { setPick(reset); input.focus(); } }, svg(ICONS.close, 10)))));
    chips.hidden = !tags.length;
  }

  // Everything optional lives behind the one + button.
  popover(plusBtn, (close) => {
    const aloud = el('input', { type: 'checkbox', class: 'toggle', role: 'switch', 'aria-label': 'Read answers aloud' });
    aloud.checked = readAloud.get();
    aloud.addEventListener('change', () => {
      readAloud.set(aloud.checked);
      if (!aloud.checked) stopSpeaking();
    });
    const row = (title, list, key) =>
      el('div', { class: 'opt-row' }, el('div', { class: 'opt-k' }, title),
        el('div', { class: 'seg seg-sm', role: 'radiogroup', 'aria-label': title }, ...list.map(([v, t]) =>
          el('button', { type: 'button', class: 'seg-btn', role: 'radio', 'aria-checked': String(pick[key] === v), onclick: () => { setPick({ [key]: v }); close(); } }, t))));
    return [
      row('Answer from', MODES.map(([v, t]) => [v, t]), 'mode'),
      row('Style', STYLES, 'style'),
      row('Language', LANGS, 'lang'),
      pick.mode === 'chat' ? '' : popHead('Search in'),
      ...(pick.mode === 'chat' ? [] : [
        popItem('All indexed files', { checked: !pick.root, onPick: () => setPick({ root: '' }), close }),
        ...roots.map((r) => popItem(rootName(r.path), { checked: pick.root === r.path, sub: r.path, onPick: () => setPick({ root: r.path }), close })),
      ]),
      models.length > 1 ? popHead('Model') : '',
      ...(models.length > 1 ? models.map((m) => popItem(m, { checked: pick.model === m, onPick: () => setPick({ model: m }), close })) : []),
      el('label', { class: 'pop-toggle' }, el('span', {}, 'Read answers aloud'), aloud),
      el('div', { class: 'pop-note' }, 'Tip: type / for commands, @ to pick a folder.'),
    ];
  });

  api.roots().then((d) => { roots = d.roots; }).catch(() => {});
  api.settings().then((s) => {
    models = s.server?.models || [];
    pick.model = s.model || '';
    drawPicks();
  }).catch(() => {});
  drawPicks();

  // ---------- "/" commands and "@" folders ----------
  let items = [];
  let active = 0;
  function hideSuggest() {
    suggest.hidden = true;
    items = [];
  }
  /** The "@word" being typed right before the caret, if any. */
  function atToken() {
    const upto = input.value.slice(0, input.selectionStart);
    const m = /(^|\s)@([^\s@]*)$/.exec(upto);
    return m ? { start: upto.length - m[2].length - 1, q: m[2].toLowerCase() } : null;
  }
  function updateSuggest() {
    const v = input.value;
    const at = atToken();
    if (/^\/\S*$/.test(v)) {
      const q = v.toLowerCase();
      items = COMMANDS.filter(([c]) => c.startsWith(q)).map(([c, sub, act]) => ({ label: c, sub, run: () => runCommand(act) }));
    } else if (at) {
      const list = [{ path: '', name: 'All indexed files' }, ...roots.map((r) => ({ path: r.path, name: rootName(r.path) }))];
      items = list
        .filter((r) => !at.q || r.name.toLowerCase().includes(at.q) || r.path.toLowerCase().includes(at.q))
        .map((r) => ({ label: '@' + r.name, sub: r.path || 'Every folder you added', run: () => {
          input.value = (input.value.slice(0, at.start) + input.value.slice(input.selectionStart)).replace(/\s{2,}/g, ' ');
          setPick({ root: r.path, mode: pick.mode === 'chat' ? 'auto' : pick.mode });
        } }));
    } else items = [];
    if (!items.length) return hideSuggest();
    active = Math.min(active, items.length - 1);
    suggest.replaceChildren(
      el('div', { class: 'pop-h' }, v.startsWith('/') ? 'Commands' : 'Search in folder'),
      ...items.map((it, i) =>
        el('button', { type: 'button', class: 'pop-item', role: 'option', 'aria-selected': String(i === active), tabindex: '-1', onmousedown: (e) => e.preventDefault(), onclick: () => choose(i) },
          el('span', { class: 'pop-label' }, it.label), el('span', { class: 'pop-sub' }, it.sub)),
      ),
    );
    suggest.hidden = false;
  }
  function choose(i) {
    const it = items[i];
    hideSuggest();
    it?.run();
    grow();
    input.focus();
  }
  function runCommand(act) {
    input.value = act.text || '';
    if (act.newChat) {
      if (navigate) navigate('chat', { chatId: null });
      return;
    }
    if (act.go) {
      if (navigate) navigate(act.go);
      return;
    }
    const { text, ...rest } = act;
    if (Object.keys(rest).length) setPick(rest);
    input.setSelectionRange(input.value.length, input.value.length);
  }

  // ---------- typing ----------
  function grow() {
    input.style.height = 'auto';
    input.style.height = `${Math.min(input.scrollHeight, 220)}px`;
    send.disabled = !busy && !input.value.trim();
  }
  input.addEventListener('input', () => {
    active = 0;
    grow();
    updateSuggest();
  });
  input.addEventListener('click', updateSuggest);
  input.addEventListener('keydown', (e) => {
    if (!suggest.hidden && items.length) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        active = (active + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
        return updateSuggest();
      }
      if (e.key === 'Enter' || e.key === 'Tab') {
        e.preventDefault();
        return choose(active);
      }
      if (e.key === 'Escape') return hideSuggest();
    }
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      submit();
    }
    if (e.key === 'Escape' && busy) busy.abort();
    // Up arrow in an empty box brings back the last question, like a terminal.
    if (e.key === 'ArrowUp' && !input.value && lastQuestion) {
      e.preventDefault();
      input.value = lastQuestion;
      grow();
    }
  });
  input.addEventListener('blur', () => setTimeout(hideSuggest, 120));

  // Ctrl+K or "/" anywhere on the page jumps to the box.
  const onKey = (e) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName);
    if ((e.ctrlKey && e.key.toLowerCase() === 'k') || (e.key === '/' && !typing)) {
      e.preventDefault();
      input.focus();
    }
    if (e.key === 'Escape' && busy) busy.abort();
  };
  document.addEventListener('keydown', onKey);

  // ---------- answers ----------
  function setBusy(on) {
    send.classList.toggle('send-stop', on);
    send.replaceChildren(svg(on ? ICON.stop : ICONS.send, on ? 13 : 15));
    send.setAttribute('aria-label', on ? 'Stop' : 'Send');
    send.title = on ? 'Stop (Esc)' : 'Send (Enter)';
    box.classList.toggle('ask-busy', on);
    grow();
  }

  /** Perplexity-style follow-ups, built from what the answer cited. */
  function related(turn) {
    const files = [...new Set((turn.citations || []).map((c) => fileName(c.file)))].slice(0, 2);
    const qs = [];
    for (const f of files) qs.push(`Summarize ${f}`, `What are the key dates and numbers in ${f}?`);
    if (turn.route === 'chat') qs.push('Explain that more simply', 'Give me a real-world example');
    if (turn.route === 'files' && !files.length) qs.push('Which of my files mention this?');
    return [...new Set(qs)].slice(0, 3);
  }

  function answerTools(turn) {
    const answerBox = turn.box;
    if (!answerBox || turn.answer == null || turn.error || turn.noResults || !String(turn.answer).trim()) return;
    const status = el('span', { class: 'action-status', role: 'status' });
    const copy = el('button', { type: 'button', class: 'tool-btn', title: 'Copy answer' }, svg(ICON.copy, 13), 'Copy');
    copy.addEventListener('click', () => navigator.clipboard.writeText(turn.answer).then(() => { status.textContent = 'Copied'; setTimeout(() => (status.textContent = ''), 1400); }, () => (status.textContent = 'Could not copy')));
    const retry = el('button', { type: 'button', class: 'tool-btn', title: 'Ask again', onclick: () => submit(turn.q) }, svg(ICON.retry, 13), 'Retry');
    const tools = el('div', { class: 'answer-tools' }, copy, retry, status);
    const fb = answerBox.querySelector('.feedback'); // share the HELPFUL / NOT HELPFUL row when there is one
    if (fb) fb.append(tools);
    const rel = related(turn);
    answerBox.append(
      fb ? '' : tools,
      rel.length
        ? el('div', { class: 'related' }, el('div', { class: 'sources-h' }, 'Related'), ...rel.map((q) => el('button', { type: 'button', class: 'related-row', onclick: () => submit(q) }, el('span', {}, q), svg(ICON.plus, 12))))
        : '',
    );
  }

  async function submit(text) {
    const message = (typeof text === 'string' ? text : input.value).trim();
    if (busy) return busy.abort();
    if (!message) return;
    // "/find <words>" opens Find file with the words filled in; it answers without the chat model.
    const find = /^\/find\b\s*(.*)$/i.exec(message);
    if (find && navigate) return navigate('find', { prefill: { q: find[1] } });
    hideSuggest();
    if (typeof text !== 'string') input.value = '';
    lastQuestion = message;
    if (!turns) {
      log.replaceChildren();
      setDocked(true);
    }
    log.querySelectorAll('.related').forEach((r) => r.remove()); // only the newest answer offers follow-ups
    busy = new AbortController();
    const signal = busy.signal;
    setBusy(true);
    const turn = await ask({ log, message, conversationId: convId, workspaceId, mode: pick.mode, root: pick.mode === 'chat' ? '' : pick.root, style: pick.style, language: pick.lang, signal, onScroll: () => (log.scrollTop = log.scrollHeight) });
    busy = null;
    setBusy(false);
    turns++;
    if (signal.aborted) {
      turn.box.querySelector('.streaming')?.classList.remove('streaming');
      turn.box.querySelector('.matched-thinking')?.remove();
      turn.box.append(el('div', { class: 'route-tag' }, 'Stopped'));
    } else answerTools(turn);
    if (readAloud.get() && turn.answer && !turn.error && !turn.noResults) speak(turn.answer).catch((e) => log.append(notice(e.message, 'error')));
    if (turn.conversation) {
      convId = turn.conversation.id;
      if (turn.conversation.title) titleEl.textContent = turn.conversation.title;
      onChatsChanged?.(convId);
    }
    log.scrollTop = log.scrollHeight;
    input.focus();
  }
  send.addEventListener('click', () => submit());

  // ---------- fresh chat ----------
  setDocked(false);
  if (convId != null) {
    api.conversation(convId).then((c) => {
      const old = turnsFromMessages(c.messages);
      if (!old.length || turns) return;
      turns = old.length;
      setDocked(true);
      if (c.title) titleEl.textContent = c.title;
      log.replaceChildren();
      for (const t of old) {
        const [bubble, answerBox] = turnView(t);
        log.append(bubble, answerBox);
        t.box = answerBox;
        answerTools(t);
      }
      log.querySelectorAll('.related').forEach((r, i, all) => i < all.length - 1 && r.remove());
      lastQuestion = old[old.length - 1].q;
      log.scrollTop = log.scrollHeight;
    }).catch(() => {});
  }
  grow();
  input.focus();
  if (prefill?.ask) submit(prefill.ask); // an example question clicked in setup
  return () => {
    busy?.abort();
    stopSpeaking();
    document.removeEventListener('keydown', onKey);
  };
}
