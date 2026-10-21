// One renderer for chat turns, shared by the Chat page and the overlay's Ask tab.
import { api, chat } from '../api.js';
import { actionCard, localInput, whenText } from './action-card.js';
import { answerNote, answerText, matchedFiles, sourceCards } from './answer.js';
import { el, notice, dots } from './common.js';
import { mascot } from './mascot.js';

const thinkingRow = (text) => el('div', { class: 'matched matched-thinking' }, mascot({ size: 28, mood: 'thinking' }), text, dots());

const ROUTE_LABEL = { files: 'Answered from your files', chat: 'General answer — not checked against your files', memory: 'Memory', task: 'Reminder / calendar', action: 'Action on this PC — needs your approval' };

/** Saved messages -> turns ({q, answer, ...meta}) for reopening a conversation. */
export function turnsFromMessages(messages) {
  const turns = [];
  for (let i = 0; i + 1 < messages.length; i += 2) {
    const [u, a] = [messages[i], messages[i + 1]];
    turns.push({ q: u.content, answer: a.content, route: a.meta.route, citations: a.meta.citations || [], uncited: a.meta.uncited, truncated: a.meta.truncated, noResults: a.meta.no_results, memories: a.meta.memory_used, messageId: a.id, rating: a.rating });
  }
  return turns;
}

/** The confirm card for a reminder, task or event proposed in chat. Nothing is saved until CONFIRM. */
function proposalCard(p) {
  const isEvent = p.kind === 'event';
  const title = el('input', { type: 'text', class: 'field', value: p.title, 'aria-label': 'Title' });
  const when = el('input', { type: 'datetime-local', class: 'field', value: localInput(p.when), 'aria-label': isEvent ? 'Starts' : 'Remind at' });
  const repeat = el('select', { class: 'field', 'aria-label': 'Repeat' }, ['', 'daily', 'weekly', 'monthly'].map((v) => el('option', { value: v }, v ? `Repeat ${v}` : 'No repeat')));
  repeat.value = p.repeat || '';
  return actionCard({
    title: isEvent ? 'Add to calendar' : p.when != null ? 'Set reminder' : 'Add task',
    lines: p.confidence === 'date_only' && !p.all_day ? ['No time was given, so 9:00 was assumed. Check it.'] : [],
    fields: isEvent ? [title, when] : [title, when, repeat],
    onConfirm: async () => {
      const name = title.value.trim();
      if (!name) throw new Error('Give it a title.');
      const sec = when.value ? new Date(when.value).getTime() / 1000 : null;
      if (isEvent) {
        if (sec == null) throw new Error('Pick a start time.');
        await api.createEvent({ title: name, start_at: sec, all_day: p.all_day });
        return 'Added to your calendar';
      }
      await api.createTask({ title: name, due_at: sec, remind_at: sec, repeat: repeat.value || null });
      return sec != null ? `Reminder set for ${whenText(sec)}` : 'Task added';
    },
  });
}

const ACTION_TITLE = { open_path: 'Open file or folder', open_app: 'Open app', create_note: 'Create note', move_file: 'Move file' };
const ACTION_DONE = { open_path: 'Opened', open_app: 'Opened', create_note: 'Note created', move_file: 'Moved' };

/** The approval card for an action on this PC. The lines are exactly what will happen; nothing runs before APPROVE. */
function toolCard(p) {
  return actionCard({
    title: ACTION_TITLE[p.tool] || p.tool.toUpperCase(),
    lines: p.lines,
    confirmLabel: 'Approve',
    onConfirm: async () => {
      await api.approveAction(p.id);
      return ACTION_DONE[p.tool] || 'Done';
    },
    onCancel: () => api.rejectAction(p.id),
    onUndo: p.undoable ? async () => {
      await api.undoAction(p.id);
      return 'Undone';
    } : undefined,
  });
}

/** HELPFUL / NOT HELPFUL under an answer. Marks stay on this PC; not-helpful ones can be exported as test questions. */
function feedbackBar(turn) {
  if (!turn.messageId || !['files', 'chat'].includes(turn.route)) return '';
  let rating = turn.rating || 0;
  const note = el('span', { class: 'action-status', role: 'status' });
  const buttons = [[1, 'Helpful'], [-1, 'Not helpful']].map(([value, text]) => [value, el('button', { type: 'button', class: 'feedback-btn', 'aria-pressed': String(rating === value) }, text)]);
  for (const [value, button] of buttons) {
    button.addEventListener('click', async () => {
      const next = rating === value ? 0 : value;
      try {
        await api.rateAnswer(turn.messageId, next);
        rating = turn.rating = next;
        for (const [v, b] of buttons) b.setAttribute('aria-pressed', String(v === rating));
        note.textContent = next === -1 ? 'Noted. Export these as test questions in settings.' : '';
      } catch (e) {
        note.textContent = e.message;
      }
    });
  }
  return el('div', { class: 'feedback' }, ...buttons.map(([, b]) => b), note);
}

/** "Used N memories": shows which remembered facts shaped the answer, each deletable. */
function memoryNote(memories) {
  if (!memories?.length) return '';
  const list = el('ul', { class: 'mem-used-list' });
  for (const m of memories) {
    const li = el('li', {}, m.text, ' ');
    li.append(el('button', { type: 'button', class: 'link-btn', onclick: () => api.deleteMemory(m.id).then(() => li.remove(), () => {}) }, 'Forget'));
    list.append(li);
  }
  return el('details', { class: 'mem-used' }, el('summary', {}, `Used ${memories.length} memor${memories.length > 1 ? 'ies' : 'y'} (not from files)`), list);
}

export function fillAnswer(box, turn, { sourcesHeading = false } = {}) {
  if (turn.error) return box.replaceChildren(notice(turn.error, 'error'));
  if (turn.noResults) return box.replaceChildren(notice('Nothing in your indexed files matches that. Add a folder in Settings → Files & PC access, or try different words.'));
  const cards = sourceCards(turn.citations, turn.answer);
  box.replaceChildren(
    turn.route ? el('div', { class: 'route-tag' }, ROUTE_LABEL[turn.route]) : '',
    answerText(turn.answer, turn.citations, turn.uncited),
    answerNote(turn) || '',
    cards && sourcesHeading ? el('div', { class: 'sources-h' }, 'Sources') : '',
    cards || '',
    memoryNote(turn.memories),
    feedbackBar(turn),
  );
}

export function turnView(turn, opts) {
  const box = el('div', { class: 'assistant' });
  if (turn.answer != null) fillAnswer(box, turn, opts);
  return [el('div', { class: 'bubble-user' }, turn.q), box];
}

/**
 * Appends the question and a live answer to `log`, streams /chat into it, and resolves with the
 * finished turn: {q, box, answer, route, citations, uncited, truncated, noResults, error, conversation:{id,title}}.
 */
export async function ask({ log, message, conversationId, workspaceId, mode, root, context, style, language, signal, onScroll, opts }) {
  const turn = { q: message, answer: null, citations: [] };
  const [bubble, box] = turnView(turn);
  turn.box = box;
  bubble.classList.add('bubble-in');
  box.classList.add('bubble-in');
  const live = el('div', { class: 'answer-text streaming' });
  let status = thinkingRow(mode === 'chat' ? 'Thinking…' : 'Searching your files…');
  box.append(status, live);
  log.append(bubble, box);
  onScroll?.();

  let text = '';
  await chat(
    { message, conversationId, workspaceId, mode, root, context, style, language },
    {
      route: (d) => {
        turn.route = d.route;
        if (d.route === 'chat') status.replaceWith((status = thinkingRow('General answer')));
      },
      results: (d) => status.replaceWith((status = matchedFiles(d.results) || status)),
      pc_step: (d) => status.replaceWith((status = thinkingRow(`On this PC: ${d.detail}`))),
      warning: (d) => box.prepend(notice(d.detail)),
      token: (d) => {
        text += d.delta;
        live.textContent = text;
        onScroll?.();
      },
      memory_used: (d) => (turn.memories = d.memories),
      action: (d) => (turn.proposal = d.proposal),
      tool_proposal: (d) => (turn.tool = d),
      conversation: (d) => {
        turn.conversation = d;
        turn.messageId = d.message_id;
      },
      done: (d) => {
        Object.assign(turn, { answer: d.answer, citations: d.citations, uncited: d.uncited, truncated: d.truncated, noResults: d.no_results });
        fillAnswer(box, turn, opts);
        if (turn.proposal) box.append(proposalCard(turn.proposal));
        if (turn.tool) box.append(toolCard(turn.tool));
      },
      error: (d) => {
        turn.error = d.detail;
        turn.answer = '';
        fillAnswer(box, turn, opts);
      },
    },
    signal,
  );
  onScroll?.();
  return turn;
}
