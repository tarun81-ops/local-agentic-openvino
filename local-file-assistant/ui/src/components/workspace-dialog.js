import { api } from '../api.js';
import { el, notice } from './common.js';

// "New workspace": a name, the folders chat may answer from (or the whole PC) and an answer style. A native <dialog>,
// so focus, Escape and the backdrop behave like the rest of Windows. Folders come from the system folder picker.
const STYLES = [['', 'My default'], ['concise', 'Concise'], ['detailed', 'Detailed'], ['study', 'Study'], ['simple', 'Simple']];

export function workspaceDialog({ onCreated }) {
  const roots = [];
  const name = el('input', { type: 'text', id: 'ws-name', maxlength: '60', placeholder: 'e.g. College', autocomplete: 'off' });
  const whole = el('input', { type: 'checkbox', id: 'ws-whole', class: 'check' });
  const list = el('ul', { class: 'ws-folders' });
  const add = el('button', { type: 'button', class: 'btn btn-outline btn-sm' }, 'Add folder');
  const style = el('select', { id: 'ws-style' }, ...STYLES.map(([v, label]) => el('option', { value: v }, label)));
  const flash = el('div');
  const create = el('button', { type: 'submit', class: 'btn btn-primary btn-sm' }, 'Create');
  const dialog = el('dialog', { class: 'ws-dialog', 'aria-labelledby': 'ws-title' },
    el('form', { method: 'dialog', class: 'stack' },
      el('h2', { id: 'ws-title' }, 'New workspace'),
      el('label', { for: 'ws-name' }, 'Name'), name,
      el('div', { class: 'quiet' }, 'Chat in this workspace answers only from these folders.'),
      list,
      el('div', { class: 'row-gap' }, add, el('label', { class: 'chip' }, whole, ' Whole PC')),
      el('label', { for: 'ws-style' }, 'Answer style'), style,
      flash,
      el('div', { class: 'row-gap' }, create, el('button', { type: 'button', class: 'btn btn-outline btn-sm', onclick: () => done() }, 'Cancel'))));

  function draw() {
    list.replaceChildren(...(whole.checked ? [el('li', { class: 'quiet' }, 'The whole PC (every drive PC access covers)')]
      : roots.length ? roots.map((r, i) => el('li', {}, el('span', { class: 'mono' }, r), ' ',
        el('button', { type: 'button', class: 'btn btn-outline btn-sm', 'aria-label': `Remove ${r}`, onclick: () => { roots.splice(i, 1); draw(); } }, 'Remove')))
        : [el('li', { class: 'quiet' }, 'No folders yet. Add one, or tick Whole PC.')]));
    add.disabled = whole.checked;
  }
  add.addEventListener('click', async () => {
    const folder = await window.lfa.pickFolder();
    if (folder && !roots.includes(folder)) roots.push(folder);
    draw();
  });
  whole.addEventListener('change', draw);
  dialog.querySelector('form').addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!whole.checked && !roots.length) return flash.replaceChildren(notice('Add a folder, or tick Whole PC.', 'error'));
    create.disabled = true;
    try {
      const ws = await api.createWorkspace({ name: name.value, roots: whole.checked ? [] : roots, answer_style: style.value });
      done();
      onCreated?.(ws);
    } catch (err) {
      flash.replaceChildren(notice(err.message, 'error'));
    } finally {
      create.disabled = false;
    }
  });
  const done = () => {
    dialog.close();
    dialog.remove();
  };
  dialog.addEventListener('close', () => dialog.remove()); // Escape
  draw();
  document.body.append(dialog);
  dialog.showModal();
  name.focus();
}
