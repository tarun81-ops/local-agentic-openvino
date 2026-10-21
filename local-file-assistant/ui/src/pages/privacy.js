import { api } from '../api.js';
import { badge, el, notice } from '../components/common.js';

// Settings > How it works & privacy. Plain words, and the live state of every switch that lets something leave this
// PC, read from the backend (engine, download host, data folder) so the page is the same in both builds and never
// drifts from what the app does.
export function render(container) {
  const body = el('div', { class: 'page-body sections' }, el('div', { class: 'quiet' }, 'Loading…'));
  container.replaceChildren(
    el('header', { class: 'page-head' }, el('div', {}, el('h1', { class: 'page-title' }, 'How it works & privacy'),
      el('div', { class: 'page-sub' }, 'What stays on this PC, and the few times the app uses the internet'))),
    body,
  );

  const section = (title, ...children) => el('section', {}, el('div', { class: 'section-h' }, el('h2', {}, title)), el('div', { class: 'box box-pad stack' }, ...children));
  const item = (state, text) => el('div', { class: 'row-gap privacy-item' }, state, el('span', {}, text));
  const on = (yes, whenOn = 'ON', whenOff = 'OFF') => badge(yes ? whenOn : whenOff, yes ? 'red-outline' : 'outline');

  Promise.all([
    api.settings(),
    api.setupStatus().catch(() => null),
    api.models().catch(() => null),
    window.lfa.updates().catch(() => null),
    api.pcStatus().catch(() => null),
  ]).then(([s, setup, models, updates, pc]) => {
    const engine = setup?.engine?.name || 'the model engine';
    const ext = models?.external;
    body.replaceChildren(
      section('Stays on this PC',
        el('div', {}, `Your questions, your files and the answers. The model runs on this PC (${engine}), the backend only listens on this PC (127.0.0.1) and needs a key that changes every launch. There is no account and no telemetry.`),
        el('div', {}, `Everything the app keeps is in ${s.data_dir}: chats, memories, settings, ${s.store_file_text ? 'the search index (it holds a copy of the text of indexed folders)' : 'and a list of file names and folders (names-only mode: no copy of the text inside your files)'}.`)),
      section('When the app uses the internet',
        item(badge('YOU CLICK', 'outline'), `Downloading a model (setup and Settings > Model): from ${setup?.model?.source || 'the model site'}.`),
        item(on(updates?.enabled !== false), 'Once a day: asking github.com whether a new version exists. Nothing about you or your files is sent. Switch: Settings > General > Privacy.'),
        item(on(Boolean(ext?.enabled)), ext?.enabled
          ? `Another server answers your chat: ${ext.url}. ${ext.loopback ? 'It is on this PC.' : 'It is NOT on this PC: your questions and the file text they use are sent there.'} Switch: Settings > General > Model.`
          : 'Another OpenAI-compatible server (off): only if you switch it on in Settings > General > Model.'),
        item(badge('YOU ADD', 'outline'), 'Plug-in tools: programs you add yourself in Settings > Plug-in tools. A tool that changes things or sends data always asks first.'),
        ext?.enabled && !ext.loopback ? notice('Your questions are currently sent to a server on another computer.', 'error') : ''),
      section('What it looks at',
        item(on(Boolean(pc?.enabled)), pc?.enabled ? `Files on ${pc.drives.join(', ')}: names and locations are listed; a file is opened only when you ask about it.` : 'PC access is off: the app sees no files except ones you drop into chat.'),
        el('div', {}, 'Never opened: Windows and program folders, AppData, developer folders, and anything that looks like a password, key or credentials file.'),
        el('div', {}, 'A file you drop into chat is read for that answer only and is not added to the index unless you click Keep in index.')),
      section('How answers are made',
        el('div', {}, 'The app finds the files first, then the model answers from passages of them. Every claim is checked against the file it cites, and the source is shown with its page.'),
        el('div', {}, 'Marks, totals and percentages are calculated in code, not by the model, and shown with the working.'),
        el('div', {}, 'For "my" documents the app checks whose name is on them before calling one yours.'),
        el('div', {}, 'Text inside files, windows, the clipboard and plug-in tool results is treated as data. It can never make the app do something.')),
      section('Changes to your files',
        el('div', {}, 'Moving, renaming, deleting and tidying are always a plan you see first. Nothing changes until you approve it, deletes go to the Recycle Bin, and most changes can be undone from Organize > History.')),
    );
  }).catch((e) => body.replaceChildren(notice(e.message, 'error')));
}
