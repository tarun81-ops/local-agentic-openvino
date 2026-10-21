import { api, setupPull } from '../api.js';
import { badge, el, notice } from '../components/common.js';
import { render as renderFiles } from './files.js';

// First-run setup: shown on first launch (until POST /setup/done) and from Settings → Model & backend.
// What each step checks and offers comes from GET /setup/status, so this page is the same in the Ollama and the
// OpenVINO build: the engine's name, its state, the help link and the download all come from the backend.
const STEPS = ['Welcome', 'Model engine', 'Model', 'Files & PC access', 'Try it'];
const EXAMPLES = [
  'Find my class 12 marksheet',
  'What percentage did I get in class 12? Show the working',
  'Which PDFs did I download this week?',
];

export function render(container, { navigate, onStatusChanged }) {
  let step = 0;
  let status = null;
  let timer = null;
  let pulling = null; // AbortController while the progress stream is open
  let filesCleanup = null; // the embedded Files & PC access pane polls while a scan runs
  let disposed = false;

  const body = el('div', { class: 'box box-pad stack setup-step', tabindex: '-1' }, el('div', { class: 'quiet' }, 'Checking this PC…'));
  const nav = el('div', { class: 'row-gap setup-nav' });
  const strip = el('ol', { class: 'setup-steps', 'aria-label': 'Setup steps' });
  container.replaceChildren(
    el('header', { class: 'page-head' }, el('div', {}, el('h1', { class: 'page-title' }, 'Set up'), el('div', { class: 'page-sub' }, 'A few steps, then you can ask about your files'))),
    el('div', { class: 'page-body sections' }, strip, body, nav),
  );

  const button = (label, onclick, tone = 'btn-outline') => el('button', { type: 'button', class: `btn ${tone} btn-sm`, onclick }, label);

  async function refresh() {
    clearTimeout(timer);
    try {
      status = await api.setupStatus();
    } catch (e) {
      status = null;
      body.replaceChildren(notice(e.message, 'error'), button('Check again', refresh));
      return;
    }
    if (!disposed) draw();
  }

  async function finish(question) {
    try {
      await api.setupDone();
    } catch { /* the wizard shows again next launch; nothing is lost */ }
    onStatusChanged?.();
    navigate('chat', question ? { prefill: { ask: question } } : {});
  }

  function go(n) {
    step = Math.max(0, Math.min(STEPS.length - 1, n));
    draw();
    body.focus();
  }

  function draw() {
    clearTimeout(timer);
    filesCleanup?.();
    filesCleanup = null;
    strip.replaceChildren(...STEPS.map((s, i) => el('li', { class: i === step ? 'on' : i < step ? 'past' : '', 'aria-current': i === step ? 'step' : null }, `${String(i + 1).padStart(2, '0')} ${s}`)));
    const back = step > 0 ? button('Back', () => go(step - 1)) : '';
    const next = step < STEPS.length - 1 ? button('Next', () => go(step + 1), 'btn-primary') : button('Finish', () => finish(), 'btn-primary');
    nav.replaceChildren(back, next, button('Skip setup', () => finish()));
    [welcome, engineStep, modelStep, filesStep, tryStep][step]();
  }

  function welcome() {
    body.replaceChildren(
      el('h2', {}, 'Your files, answered on this PC'),
      el('div', {}, 'Ask for any file by describing it, ask what is inside it, and get answers with the page they came from. Marks and totals are calculated in code, and the app checks a document is yours before it calls it yours.'),
      el('div', { class: 'privacy' },
        el('div', { class: 'privacy-h' }, 'What stays private'),
        el('div', {}, 'Your questions and your files never leave this PC. There is no account and no telemetry. The app uses the internet only when you click Download in the next steps (it says where from first), and once a day to check for a new version of the app, which you can turn off in Settings → Privacy.')),
      el('div', { class: 'quiet' }, 'Nothing on your PC is moved, renamed or deleted unless you approve it.'),
    );
  }

  function engineStep() {
    const e = status.engine;
    const tone = e.state === 'ready' ? 'blue' : e.ok ? 'outline' : 'red';
    body.replaceChildren(
      el('h2', {}, e.name),
      el('div', { class: 'row-gap', role: 'status' }, badge(e.state.toUpperCase(), tone), e.state === 'ready' ? el('span', {}, 'Running on this PC.') : ''),
      e.detail ? notice(e.detail, e.ok ? 'neutral' : 'error') : '',
      el('div', { class: 'row-gap' },
        e.help ? button(e.help.label, () => window.lfa.openLink(e.help.url), 'btn-primary') : '',
        button('Check again', refresh)),
      e.help ? el('div', { class: 'quiet' }, 'Opens the official download page in your browser. Install it yourself, then come back and click Check again.') : '',
    );
    if (['starting', 'warming up'].includes(e.state)) timer = setTimeout(refresh, 2000);
  }

  function modelStep() {
    const m = status.model;
    const rec = m.recommended;
    if (!m.needed.length) {
      body.replaceChildren(
        el('h2', {}, 'Model'),
        el('div', { class: 'row-gap' }, badge('READY', 'blue'), el('span', {}, `Installed: ${m.installed.join(', ')}`)),
        el('div', { class: 'quiet' }, `Recommended ${rec.why}: ${rec.label}.`),
      );
      return;
    }

    const bar = el('div', { class: 'meter', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-label': 'Download progress' }, el('div'));
    const line = el('div', { class: 'quiet', role: 'status' });
    const out = el('div', { class: 'stack' });
    const total = m.needed.reduce((a, n) => a + n.size_mb, 0);
    const start = button(`Download (${(total / 1000).toFixed(1)} GB)`, () => download(), 'btn-primary');
    const stop = button('Cancel', () => api.setupCancel().catch(() => {}));
    stop.hidden = true;

    function show(s) {
      const pct = s.percent ?? 0;
      bar.firstChild.style.width = `${pct}%`;
      bar.setAttribute('aria-valuenow', String(pct));
      const which = s.model ? `${s.model}${s.steps > 1 ? ` (${s.step} of ${s.steps})` : ''} — ` : '';
      const size = s.total_mb ? ` · ${(s.mb || 0).toLocaleString()} of ${s.total_mb.toLocaleString()} MB` : '';
      line.textContent = `${which}${pct}%${size}`;
    }

    function end(s, ok) {
      pulling = null;
      stop.hidden = true;
      start.disabled = false;
      out.replaceChildren(...(ok ? [] : [bar, line]), notice(s.detail || 'The download stopped.', ok ? 'neutral' : 'error'));
      if (ok) refresh();
    }

    async function download() {
      start.disabled = true;
      stop.hidden = false;
      out.replaceChildren(bar, line);
      pulling = new AbortController();
      await setupPull({
        progress: show,
        done: (s) => end(s, true),
        failed: (s) => end(s, false),
        cancelled: (s) => end(s, false),
        error: (s) => end(s, false),
      }, pulling.signal);
    }

    body.replaceChildren(
      el('h2', {}, 'Model'),
      el('div', {}, `Recommended ${rec.why}: `, el('strong', {}, rec.label), '.'),
      el('div', { class: 'quiet' }, `Downloads from ${m.source}. What runs:`),
      el('ul', { class: 'setup-needed' }, ...m.needed.map((n) =>
        el('li', {}, el('div', {}, `${n.label} — ${n.size_mb.toLocaleString()} MB`), el('code', { class: 'cmd' }, n.command)))),
      el('div', { class: 'row-gap' }, start, stop),
      out,
    );
    if (m.pull.status === 'running') download(); // still downloading from an earlier visit: follow it
  }

  function filesStep() {
    const pane = el('div');
    body.replaceChildren(pane);
    filesCleanup = renderFiles(pane) || null; // the same switch and wording as Settings → Files & PC access
  }

  function tryStep() {
    body.replaceChildren(
      el('h2', {}, 'Try it'),
      el('div', { class: 'quiet' }, 'Click a question to ask it in Chat.'),
      el('div', { class: 'stack setup-examples' }, ...EXAMPLES.map((q) => button(q, () => finish(q)))),
      status.missing.length ? notice(`Still missing: ${status.missing.join(', ')}. Answers need the model engine and the model; go Back to finish those steps.`, 'error') : '',
    );
  }

  refresh();
  return () => {
    disposed = true;
    clearTimeout(timer);
    filesCleanup?.();
    pulling?.abort();
  };
}
