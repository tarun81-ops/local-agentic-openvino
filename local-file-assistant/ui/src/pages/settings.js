import { api } from '../api.js';
import { applyAppearance } from '../appearance.js';
import { el, notice } from '../components/common.js';

function kv(label, ...value) {
  return el('div', { class: 'kv' }, el('div', { class: 'kv-k' }, label), el('div', { class: 'kv-v' }, ...value));
}

function sectionHead(n, title) {
  return el('div', { class: 'section-h' }, el('h2', {}, el('span', { class: 'n' }, `${n} —`), ` ${title}`));
}

export function render(container, { onStatusChanged, navigate }) {
  const model = el('div', { class: 'box box-pad' }, el('div', { class: 'quiet' }, 'Loading…'));
  const flash = el('div');
  const toggle = el('input', { type: 'checkbox', id: 'launch-at-startup', class: 'toggle', role: 'switch' });
  const shortcut = el('span', { class: 'keys' });
  const shortcutInput = el('input', { type: 'text', class: 'shortcut-input', placeholder: 'e.g. Ctrl+Alt+Space', 'aria-label': 'New shortcut' });
  const shortcutSet = el('button', { type: 'button', class: 'btn btn-ink btn-sm' }, 'Set');
  const meBox = el('div', { class: 'box box-pad stack' }, el('div', { class: 'quiet' }, 'Loading…'));
  const perfBox = el('div', { class: 'box box-pad stack' }, el('div', { class: 'quiet' }, 'Loading…'));
  const lookBox = el('div', { class: 'box box-pad stack' }, el('div', { class: 'quiet' }, 'Loading…'));
  const inboxBox = el('div', { class: 'box box-pad stack' });
  const proBox = el('div', { class: 'box box-pad stack' }, el('div', { class: 'quiet' }, 'Loading…'));
  const voiceBox = el('div', { class: 'box box-pad stack' }, el('div', { class: 'quiet' }, 'Loading…'));
  const learnBox = el('div', { class: 'box box-pad stack' }, el('div', { class: 'quiet' }, 'Loading…'));

  container.replaceChildren(
    el('header', { class: 'page-head' }, el('h1', { class: 'page-title' }, 'Settings')),
    el(
      'div',
      { class: 'page-body sections' },
      flash,
      el('section', {}, sectionHead('00', 'About me & answers'), meBox),
      el('section', {}, sectionHead('01', 'Model & backend'), model,
        el('div', { class: 'row-gap' }, el('button', { type: 'button', class: 'btn btn-outline btn-sm', onclick: () => navigate?.('setup') }, 'Run setup again'))),
      el(
        'div',
        { class: 'two-col' },
        el(
          'section',
          {},
          sectionHead('02', 'Global shortcut'),
          el(
            'div',
            { class: 'box box-pad stack' },
            el('div', { class: 'kv kv-plain' }, el('div', {}, 'Summon overlay'), el('div', { class: 'row-gap' }, shortcut, shortcutInput, shortcutSet)),
            el('div', { class: 'kv kv-plain' }, el('label', { for: 'launch-at-startup' }, 'Launch at startup'), toggle),
          ),
        ),
        el(
          'section',
          {},
          sectionHead('03', 'Privacy'),
          el(
            'div',
            { class: 'privacy' },
            el('div', { class: 'privacy-h' }, 'Private by design'),
            el('div', {}, 'Nothing you index or ask ever leaves this device. No cloud calls, no telemetry.'),
          ),
        ),
      ),
      el('section', {}, sectionHead('04', 'Proactive suggestions'), proBox, inboxBox),
      el('section', {}, sectionHead('05', 'Voice'), voiceBox),
      el('section', {}, sectionHead('06', 'Learning from use'), learnBox),
      el('section', {}, sectionHead('07', 'Appearance'), lookBox),
      el('section', {}, sectionHead('08', 'Performance'), perfBox),
    ),
  );

  const showKeys = (keys) => shortcut.replaceChildren(...keys.split('+').flatMap((k, i) => [i ? el('span', { class: 'plus' }, '+') : '', el('kbd', {}, k.length > 1 ? k[0].toUpperCase() + k.slice(1).toLowerCase() : k.toUpperCase())]));
  window.lfa.shortcut().then(showKeys);
  shortcutSet.addEventListener('click', async () => {
    const r = await window.lfa.setShortcut(shortcutInput.value);
    if (r.ok) {
      showKeys(r.shortcut);
      shortcutInput.value = '';
      flash.replaceChildren();
    } else flash.replaceChildren(notice(r.error, 'error'));
  });
  window.lfa.getLaunchAtStartup().then(({ enabled, supported }) => {
    toggle.checked = enabled;
    toggle.disabled = !supported;
    if (!supported) toggle.title = 'Available in the installed app';
  });
  toggle.addEventListener('change', () => window.lfa.setLaunchAtStartup(toggle.checked));

  // Morning briefing and nudges: each can be switched off, with quiet hours and a daily limit.
  async function loadProactive() {
    try {
      const c = await api.proactiveSettings();
      const field = (id, label, key, attrs, value) => {
        const input = el('input', { id, ...attrs });
        if (attrs.type === 'checkbox') input.checked = value;
        else input.value = value;
        input.dataset.key = key;
        return el('div', { class: 'kv kv-plain' }, el('label', { for: id }, label), input);
      };
      const sw = { type: 'checkbox', class: 'toggle', role: 'switch' };
      const time = { type: 'time', class: 'shortcut-input' };
      const save = el('button', { type: 'button', class: 'btn btn-ink btn-sm' }, 'Save');
      const preview = el('button', { type: 'button', class: 'btn btn-outline btn-sm' }, 'Preview today’s briefing');
      const out = el('div');
      const rows = [
        field('p-briefing', 'Morning briefing', 'briefing', sw, c.briefing),
        field('p-btime', 'Briefing time', 'briefing_time', time, c.briefing_time),
        field('p-overdue', 'Evening nudge for overdue tasks', 'overdue', sw, c.overdue),
        field('p-qs', 'Quiet hours from', 'quiet_start', time, c.quiet_start),
        field('p-qe', 'Quiet hours until', 'quiet_end', time, c.quiet_end),
        field('p-cap', 'Most suggestions per day', 'daily_cap', { type: 'number', min: 0, max: 10, class: 'shortcut-input' }, c.daily_cap),
        field('p-model', 'Let the local model reword the briefing (uses more ram)', 'use_model', sw, c.use_model),
      ];
      proBox.replaceChildren(...rows, el('div', { class: 'row-gap' }, save, preview), out);
      save.addEventListener('click', async () => {
        const body = {};
        for (const input of proBox.querySelectorAll('input[data-key]')) {
          body[input.dataset.key] = input.type === 'checkbox' ? input.checked : input.type === 'number' ? Number(input.value) : input.value;
        }
        try {
          await api.saveProactive(body);
          out.replaceChildren(notice('Saved.'));
        } catch (e) {
          out.replaceChildren(notice(e.message, 'error'));
        }
      });
      preview.addEventListener('click', async () => {
        try {
          const p = await api.proactivePreview();
          out.replaceChildren(notice(p.empty ? 'Nothing to report today: no tasks, events or changed files.' : p.text));
        } catch (e) {
          out.replaceChildren(notice(e.message, 'error'));
        }
      });
    } catch (e) {
      proBox.replaceChildren(notice(e.message, 'error'));
    }
  }
  // Voice: the speech models are downloaded once (a few hundred MB); everything then runs on this PC.
  async function loadVoice(errors = {}) {
    try {
      const [s, keys] = await Promise.all([api.voiceStatus(), window.lfa.shortcuts()]);
      const ready = (ok) => (ok ? 'Ready' : 'Not downloaded');
      const input = el('input', { type: 'text', class: 'shortcut-input', placeholder: 'e.g. Ctrl+Shift+Alt+V', 'aria-label': 'New voice shortcut' });
      const set = el('button', { type: 'button', class: 'btn btn-ink btn-sm' }, 'Set');
      const keyCaps = (text) => el('span', { class: 'keys' }, ...text.split('+').flatMap((k, i) => [i ? el('span', { class: 'plus' }, '+') : '', el('kbd', {}, k.length > 1 ? k[0].toUpperCase() + k.slice(1).toLowerCase() : k.toUpperCase())]));
      const download = el('button', { type: 'button', class: 'btn btn-ink btn-sm' }, 'Download voice models');
      download.disabled = s.stt.available && s.tts.available;
      const out = el('div', {}, ...Object.entries(errors).map(([k, v]) => notice(`${k === 'stt' ? 'Speech recognition' : 'Voice'}: ${v}`, 'error')));
      voiceBox.replaceChildren(
        el('div', { class: 'kv kv-plain' }, el('div', {}, `Speech recognition (${s.stt.model})`), el('div', {}, ready(s.stt.available))),
        el('div', { class: 'kv kv-plain' }, el('div', {}, `Spoken answers (${s.tts.voice})`), el('div', {}, ready(s.tts.available))),
        el('div', { class: 'kv kv-plain' }, el('div', {}, 'Talk to the assistant'), el('div', { class: 'row-gap' }, keyCaps(keys.voice), input, set)),
        el('div', { class: 'row-gap' }, download),
        out,
      );
      set.addEventListener('click', async () => {
        const r = await window.lfa.setShortcut(input.value, 'voice');
        if (r.ok) loadVoice();
        else out.replaceChildren(notice(r.error, 'error'));
      });
      download.addEventListener('click', async () => {
        download.disabled = true;
        out.replaceChildren(notice('Downloading… this can take a few minutes. Keep the app open.'));
        try {
          loadVoice((await api.downloadVoice()).errors);
        } catch (e) {
          download.disabled = false;
          out.replaceChildren(notice(e.message, 'error'));
        }
      });
    } catch (e) {
      voiceBox.replaceChildren(notice(e.message, 'error'));
    }
  }
  // Learning from use: files you open for a search rise slightly in similar searches, and answers you
  // mark not helpful can be exported as test questions. Everything stays on this PC and can be wiped.
  async function loadLearning() {
    try {
      const s = await api.learningStats();
      const exportBtn = el('button', { type: 'button', class: 'btn btn-ink btn-sm' }, 'Export not-helpful questions');
      const wipeBtn = el('button', { type: 'button', class: 'btn btn-outline btn-sm' }, 'Forget what I open and rate');
      exportBtn.disabled = s.not_helpful === 0;
      const out = el('div');
      learnBox.replaceChildren(
        el('div', { class: 'kv kv-plain' }, el('div', {}, 'File opens remembered'), el('div', {}, String(s.opens))),
        el('div', { class: 'kv kv-plain' }, el('div', {}, 'Answers marked helpful / not helpful'), el('div', {}, `${s.helpful} / ${s.not_helpful}`)),
        el('div', { class: 'row-gap' }, exportBtn, wipeBtn),
        out,
      );
      exportBtn.addEventListener('click', async () => {
        try {
          const a = el('a', { href: URL.createObjectURL(new Blob([await api.exportLearning()], { type: 'application/x-ndjson' })), download: 'not-helpful-questions.jsonl' });
          a.click();
          URL.revokeObjectURL(a.href);
          out.replaceChildren(notice('Saved. Append the lines to backend/eval/questions.jsonl and fill in expect and file for the ones you want measured.'));
        } catch (e) {
          out.replaceChildren(notice(e.message, 'error'));
        }
      });
      wipeBtn.addEventListener('click', async () => {
        if (!confirm('Forget which files you opened and which answers you rated? Search ranking returns to its defaults.')) return;
        try {
          await api.wipeLearning();
          loadLearning();
        } catch (e) {
          out.replaceChildren(notice(e.message, 'error'));
        }
      });
    } catch (e) {
      learnBox.replaceChildren(notice(e.message, 'error'));
    }
  }
  // Personalization: what the assistant knows about you and how it answers. Everything is optional and stays on this PC.
  async function loadMe() {
    try {
      const c = await api.personalize();
      const profile = el('textarea', { id: 'pz-profile', class: 'field', rows: 4, maxlength: 800, placeholder: 'e.g. First-year ETE student in Raipur. Working on a robotics project. I like short answers with examples.' });
      profile.value = c.profile;
      const count = el('div', { class: 'quiet' }, `${c.profile.length} / 800`);
      profile.addEventListener('input', () => (count.textContent = `${profile.value.length} / 800`));
      const select = (id, label, key, options, value) => {
        const input = el('select', { id, class: 'field field-mono' }, ...options.map(([v, t]) => el('option', { value: v, selected: v === value }, t)));
        input.dataset.key = key;
        return el('div', { class: 'kv kv-plain' }, el('label', { for: id }, label), input);
      };
      const sw = (id, label, key, value) => {
        const input = el('input', { id, type: 'checkbox', class: 'toggle', role: 'switch' });
        input.checked = value;
        input.dataset.key = key;
        return el('div', { class: 'kv kv-plain' }, el('label', { for: id }, label), input);
      };
      const save = el('button', { type: 'button', class: 'btn btn-ink btn-sm' }, 'Save');
      const out = el('div');
      meBox.replaceChildren(
        el('label', { for: 'pz-profile' }, 'About me (shared with the local model on every question; never leaves this PC)'),
        profile,
        count,
        select('pz-style', 'Answer style', 'answer_style', [['concise', 'Concise'], ['detailed', 'Detailed'], ['study', 'Study'], ['simple', 'Simple']], c.answer_style),
        select('pz-lang', 'Language', 'language', [['auto', 'Match what I write'], ['english', 'English'], ['hinglish', 'Hinglish']], c.language),
        sw('pz-cite', 'Always cite page numbers', 'cite_pages', c.cite_pages),
        select('pz-strict', 'Citation checking', 'verifier_strict', [['normal', 'Normal'], ['strict', 'Strict (flags more claims)']], c.verifier_strict),
        el('div', { class: 'row-gap' }, save),
        out,
      );
      save.addEventListener('click', async () => {
        const body = { profile: profile.value };
        for (const input of meBox.querySelectorAll('[data-key]')) body[input.dataset.key] = input.type === 'checkbox' ? input.checked : input.value;
        try {
          await api.savePersonalize(body);
          out.replaceChildren(notice('Saved.'));
        } catch (e) {
          out.replaceChildren(notice(e.message, 'error'));
        }
      });
    } catch (e) {
      meBox.replaceChildren(notice(e.message, 'error'));
    }
  }
  // Appearance: every change applies at once and is saved. The overlay picks up its options next time it opens.
  async function loadLook() {
    try {
      let current = (await api.personalize()).appearance;
      const out = el('div');
      const change = (patch) => {
        current = applyAppearance({ ...current, ...patch });
        api.savePersonalize({ appearance: patch }).then(() => out.replaceChildren(), (e) => out.replaceChildren(notice(e.message, 'error')));
      };
      const select = (id, label, key, options) => {
        const input = el('select', { id, class: 'field field-mono' }, ...options.map(([v, t]) => el('option', { value: v, selected: v === current[key] }, t)));
        input.addEventListener('change', () => change({ [key]: input.value }));
        return el('div', { class: 'kv kv-plain' }, el('label', { for: id }, label), input);
      };
      const size = el('input', { id: 'lk-size', type: 'range', min: 0.9, max: 1.3, step: 0.05, value: current.font_scale, 'aria-label': 'Text size' });
      const sizeNote = el('span', { class: 'quiet' }, `${Math.round(current.font_scale * 100)}%`);
      size.addEventListener('input', () => {
        sizeNote.textContent = `${Math.round(size.value * 100)}%`;
        change({ font_scale: Number(size.value) });
      });
      const compact = el('input', { id: 'lk-compact', type: 'checkbox', class: 'toggle', role: 'switch' });
      compact.checked = current.overlay_compact;
      compact.addEventListener('change', () => change({ overlay_compact: compact.checked }));
      lookBox.replaceChildren(
        select('lk-theme', 'Theme', 'theme', [['system', 'Follow Windows'], ['light', 'Light'], ['dark', 'Dark']]),
        select('lk-accent', 'Accent colour', 'accent', [['red', 'Red'], ['blue', 'Blue'], ['green', 'Green'], ['violet', 'Violet']]),
        el('div', { class: 'kv kv-plain' }, el('label', { for: 'lk-size' }, 'Text size'), el('div', { class: 'row-gap' }, size, sizeNote)),
        el('div', { class: 'kv kv-plain' }, el('label', { for: 'lk-compact' }, 'Compact overlay (smaller, fewer results)'), compact),
        select('lk-pos', 'Overlay position', 'overlay_position', [['top-right', 'Top right'], ['center', 'Centre'], ['cursor', 'Near the cursor']]),
        el(
          'div',
          { class: 'look-preview', 'aria-label': 'Preview' },
          el('span', { class: 'btn btn-primary btn-sm' }, 'Primary'),
          el('span', { class: 'btn btn-outline btn-sm' }, 'Outline'),
          el('span', { class: 'link-btn' }, 'Link'),
          el('span', { class: 'badge badge-red' }, 'Accent'),
          el('span', { class: 'badge badge-blue' }, 'Local'),
        ),
        out,
      );
    } catch (e) {
      lookBox.replaceChildren(notice(e.message, 'error'));
    }
  }
  // Performance: a profile trades speed for battery and RAM. Changes apply to the next request, no restart.
  const PROFILE_NAMES = { battery: 'Battery saver', balanced: 'Balanced', plugged: 'Plugged in', auto: 'Auto (battery or plugged in)' };
  async function loadPerf() {
    try {
      const [cfg, st] = await Promise.all([api.personalize(), api.performance()]);
      const out = el('div');
      const save = (patch) => api.savePersonalize(patch).then(() => { out.replaceChildren(notice('Saved.')); loadPerf(); }, (e) => out.replaceChildren(notice(e.message, 'error')));
      const profile = el('select', { id: 'pf-profile', class: 'field field-mono' }, ...Object.entries(PROFILE_NAMES).map(([v, t]) => el('option', { value: v, selected: v === cfg.perf_profile }, t)));
      profile.addEventListener('change', () => save({ perf_profile: profile.value }));
      const number = (id, label, key, value, attrs) => {
        const input = el('input', { id, type: 'number', class: 'shortcut-input', placeholder: 'follow profile', ...attrs });
        input.value = value ?? '';
        input.addEventListener('change', () => save({ [key]: input.value === '' ? null : Number(input.value) }));
        return el('div', { class: 'kv kv-plain' }, el('label', { for: id }, label), input);
      };
      const gb = (mb) => (mb == null ? 'Unknown' : `${(mb / 1024).toFixed(1)} GB`);
      const unload = st.values.llm_idle_unload_s;
      const power = st.power === 'battery' ? 'On battery' : st.power === 'plugged' ? 'Plugged in' : 'No battery reported';
      perfBox.replaceChildren(
        el('div', { class: 'kv kv-plain' }, el('label', { for: 'pf-profile' }, 'Profile'), profile),
        el('div', { class: 'quiet' }, `In force now: ${PROFILE_NAMES[st.active]} · ${power} · free RAM ${gb(st.free_mb)}`),
        el('div', { class: 'quiet' }, `Voice models unload after ${unload > 0 ? `${Math.round(unload / 60)} min` : 'never'} · chat remembers ${st.values.history_turns} turns · image captions ${st.values.caption_images ? 'on' : 'off'}`),
        st.note ? notice(st.note) : '',
        number('pf-unload', 'Unload voice models after (minutes, 0 = never)', 'perf_unload_minutes', cfg.perf_unload_minutes, { min: 0, max: 240 }),
        number('pf-ram', 'Stop indexing when free RAM is below (MB)', 'perf_min_free_ram_mb', cfg.perf_min_free_ram_mb, { min: 0, max: 8192 }),
        el('div', { class: 'mem-group' }, 'What each profile changes'),
        ...Object.entries(st.words).map(([k, text]) => el('div', { class: 'quiet' }, `${PROFILE_NAMES[k]} — ${text}`)),
        out,
      );
    } catch (e) {
      perfBox.replaceChildren(notice(e.message, 'error'));
    }
  }
  // New-file nudge: opt-in. With no folders listed it never looks at anything; it only counts new files by name and never moves one.
  async function loadInbox() {
    try {
      const { inbox_folders: folders } = await api.personalize();
      const out = el('div');
      const save = (next) => api.savePersonalize({ inbox_folders: next }).then(loadInbox, (e) => out.replaceChildren(notice(e.message, 'error')));
      const add = el('button', { type: 'button', class: 'btn btn-outline btn-sm' }, '+ Add a folder');
      add.addEventListener('click', async () => {
        const picked = await window.lfa.pickFolder();
        if (picked) save([...folders, picked]);
      });
      inboxBox.replaceChildren(
        el('div', { class: 'mem-group' }, 'New-file nudge (off until you add a folder)'),
        el('div', { class: 'quiet' }, 'When new files appear in these folders (say Downloads), I can tell you once a day and offer to open Organize. I only read file names. Nothing is moved unless you approve a plan.'),
        ...folders.map((f) => el('div', { class: 'kv kv-plain' }, el('div', { class: 'folder-path' }, f), el('button', { type: 'button', class: 'link-btn', 'aria-label': `Stop watching ${f}`, onclick: () => save(folders.filter((x) => x !== f)) }, 'Remove'))),
        el('div', { class: 'row-gap' }, add),
        out,
      );
    } catch (e) {
      inboxBox.replaceChildren(notice(e.message, 'error'));
    }
  }
  loadInbox();
  loadPerf();
  loadLook();
  loadMe();
  loadLearning();
  loadVoice();
  loadProactive();

  async function load() {
    try {
      const s = await api.settings();
      const picker = el('select', { class: 'field field-mono', id: 'model-select', 'aria-label': 'Model' });
      const models = s.server.models.includes(s.model) ? s.server.models : [s.model, ...s.server.models];
      picker.append(...models.map((m) => el('option', { value: m, selected: m === s.model }, m)));
      const save = el('button', { type: 'button', class: 'link-btn', disabled: true }, 'Change');
      picker.addEventListener('change', () => (save.disabled = picker.value === s.model));
      save.addEventListener('click', async () => {
        try {
          await api.setModel(picker.value);
          flash.replaceChildren(notice(`Now answering with ${picker.value}.`));
          onStatusChanged?.();
          load();
        } catch (e) {
          flash.replaceChildren(notice(e.message, 'error'));
        }
      });
      model.replaceChildren(
        kv('Base URL', el('span', { class: 'mono' }, s.base_url)),
        kv('Model', picker, save),
        kv('Embedding model', el('span', { class: 'mono' }, s.embedding_model)),
        kv(
          'Connection',
          s.server.connected
            ? el('span', { class: 'conn' }, el('span', { class: 'status-dot' }), 'Connected')
            : el('span', { class: 'conn conn-off' }, 'Not running — start OpenVINO Model Server (scripts\\ovms-start.ps1), then reopen this page'),
        ),
        kv('File types', el('span', { class: 'mono' }, s.file_types.join('  '))),
        kv('Data folder', el('span', { class: 'mono' }, s.data_dir)),
        s.memory?.free_mb != null
          ? kv(
              'Free memory',
              el(
                'span',
                { class: `mono ${s.memory.low ? 'conn-off' : ''}` },
                `${(s.memory.free_mb / 1024).toFixed(1)} GB of ${(s.memory.total_mb / 1024).toFixed(1)} GB`,
                s.memory.low ? ' — LOW: indexing skips embeddings until more is free' : '',
              ),
            )
          : '',
      );
    } catch (e) {
      model.replaceChildren(notice(e.message, 'error'));
    }
  }
  load();
}
