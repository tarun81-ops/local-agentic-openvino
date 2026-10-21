import { api, setupPull } from '../api.js';
import { badge, el, notice } from './common.js';

// Settings > Model: the models on this PC (size, expected RAM, IN USE / RECOMMENDED), the ones the app can download,
// the device the chat model runs on (OpenVINO build only: shown when the backend reports devices) and the optional
// other OpenAI-compatible server. Nothing switches by itself: a downloaded model is used only after USE.
const gb = (mb) => `${(mb / 1000).toFixed(1)} GB`;

export function modelsPanel({ onChanged }) {
  const box = el('div', { class: 'stack models-panel' }, el('div', { class: 'quiet' }, 'Loading models…'));
  const flash = el('div');
  let pulling = null;

  const say = (msg, tone) => flash.replaceChildren(msg ? notice(msg, tone) : '');
  const btn = (label, onclick, tone = 'btn-outline') => el('button', { type: 'button', class: `btn ${tone} btn-sm`, onclick }, label);

  async function act(fn, ok) {
    try {
      const r = await fn();
      say(typeof ok === 'function' ? ok(r) : ok);
      onChanged?.();
    } catch (e) {
      say(e.message, 'error');
    }
    load();
  }

  function row(m, data) {
    const tags = [
      m.active ? badge('IN USE', 'blue') : '',
      m.recommended ? badge('RECOMMENDED FOR YOUR PC', 'outline') : '',
      m.kind === 'embed' ? badge('SEARCH', 'outline') : '',
      !m.installed && m.ram_mb > data.memory.total_mb * 0.5 ? badge('NEEDS MORE RAM', 'red-outline') : '',
    ];
    const line = el('div', { class: 'quiet', role: 'status' });
    const actions = el('div', { class: 'row-gap' });
    if (m.installed && m.kind === 'chat' && !m.active) {
      actions.append(
        btn('Use', () => act(() => api.setModel(m.name), `Now answering with ${m.label}.`), 'btn-primary'),
        btn('Remove', () => {
          if (confirm(`Remove ${m.label}? It frees ${gb(m.size_mb)}; you can download it again later.`)) act(() => api.removeModel(m.name), `${m.label} removed.`);
        }),
      );
    } else if (!m.installed) {
      const cancel = btn('Cancel', () => api.setupCancel().catch(() => {}));
      cancel.hidden = true;
      const download = btn(`Download ${gb(m.size_mb)}`, async () => {
        if (pulling) return say('Another download is running. Wait for it or cancel it first.', 'error');
        download.disabled = true;
        cancel.hidden = false;
        pulling = new AbortController();
        const end = (s, ok) => {
          pulling = null;
          say(s.detail, ok ? 'neutral' : 'error');
          load();
        };
        await setupPull({
          progress: (s) => (line.textContent = `${s.percent ?? 0}% · ${(s.mb || 0).toLocaleString()} of ${(s.total_mb || m.size_mb).toLocaleString()} MB`),
          done: (s) => end({ ...s, detail: `${m.label} is downloaded. Click USE to answer with it.` }, true),
          failed: (s) => end(s, false),
          cancelled: (s) => end(s, false),
          error: (s) => end(s, false),
        }, pulling.signal, { repo: m.repo });
      });
      actions.append(download, cancel);
    }
    return el('li', { class: 'model-row' },
      el('div', { class: 'row-gap' }, el('span', { class: 'mono' }, m.label), ...tags),
      el('div', { class: 'quiet' }, `${gb(m.size_mb)} on disk · needs about ${gb(m.ram_mb)} of RAM`),
      actions, line);
  }

  function deviceRow(d) {
    const names = { CPU: 'CPU', GPU: 'iGPU', NPU: 'NPU' };
    const pick = el('select', { id: 'model-device', class: 'field' },
      el('option', { value: 'auto' }, `Auto (now ${names[d.resolved] || d.resolved})`),
      ...d.available.map((x) => el('option', { value: x }, names[x] || x)));
    pick.value = d.choice === 'auto' ? 'auto' : d.choice;
    const apply = btn('Apply', () => act(() => api.setDevice(pick.value), (r) => r.detail));
    return el('div', { class: 'stack' },
      el('div', { class: 'row-gap' }, el('label', { for: 'model-device' }, 'Runs on'), pick, apply),
      el('div', { class: 'quiet' }, 'Changing it restarts the model server; the first answer after that is slower while the model compiles.'));
  }

  function externalBlock(x) {
    const on = el('input', { type: 'checkbox', id: 'ext-on', class: 'toggle', role: 'switch' });
    on.checked = x.enabled;
    const url = el('input', { type: 'text', id: 'ext-url', class: 'field field-mono', placeholder: 'http://127.0.0.1:1234/v1', value: x.url || '' });
    const name = el('input', { type: 'text', id: 'ext-model', class: 'field field-mono', placeholder: 'model name', value: x.model || '' });
    const warn = el('div');
    const save = async (confirmRemote = false) => {
      try {
        const r = await api.setExternal({ enabled: on.checked, url: url.value, model: name.value, confirm_remote: confirmRemote });
        say(r.enabled ? `Chat now answers with ${r.model} at ${r.url}.` : 'Back to the model on this PC.');
        onChanged?.();
        load();
      } catch (e) {
        if (!/not this PC/.test(e.message)) return say(e.message, 'error');
        // The second confirmation: the risk said in words, then an explicit button. Nothing is saved until it is clicked.
        warn.replaceChildren(notice(e.message, 'error'), btn('Send my questions there anyway', () => save(true), 'btn-primary'));
      }
    };
    return el('details', { class: 'ext-server', open: x.enabled || null },
      el('summary', {}, 'Use another OpenAI-compatible server (for example LM Studio)'),
      el('div', { class: 'stack' },
        el('div', { class: 'quiet' }, 'Off by default. Only answers go there: questions, flashcards and the text of the files they use. File search, image captions, the briefing and choosing files stay on this PC. A server on another computer receives your questions, flashcards and the text of the files they use.'),
        x.enabled && !x.loopback ? notice(`Questions, flashcards and the text of the files they use are being sent to ${x.url}.`, 'error') : '',
        el('div', { class: 'kv kv-plain' }, el('label', { for: 'ext-on' }, 'Use it'), on),
        el('label', { for: 'ext-url' }, 'Address'), url,
        el('label', { for: 'ext-model' }, 'Model'), name,
        el('div', { class: 'row-gap' }, btn('Save', () => save(false))),
        warn));
  }

  async function load() {
    let data;
    try {
      data = await api.models();
    } catch (e) {
      box.replaceChildren(notice(e.message, 'error'));
      return;
    }
    if (pulling) return; // a download is drawing its progress into the current rows
    box.replaceChildren(
      flash,
      el('ul', { class: 'model-list' }, ...data.models.map((m) => row(m, data))),
      data.device ? deviceRow(data.device) : '',
      externalBlock(data.external),
    );
  }

  load();
  return { node: box, dispose: () => pulling?.abort() };
}
