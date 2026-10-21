import { api } from '../api.js';
import { badge, el, notice } from '../components/common.js';

// Settings > Plug-in tools: local MCP servers (programs on this PC) whose tools the assistant may use. A tool marked
// "Asks before running" (by you, by the server, or by a name like delete_note or send_email when the server says
// nothing) shows an approval card with the exact arguments and runs only on APPROVE; untick it to run at once.
// What a tool returns is treated as data.
export function render(container) {
  const flash = el('div');
  const list = el('div', { class: 'stack' }, el('div', { class: 'quiet' }, 'Loading…'));
  const name = el('input', { type: 'text', id: 'mcp-name', class: 'field field-mono', placeholder: 'notes', maxlength: '32' });
  const command = el('input', { type: 'text', id: 'mcp-command', class: 'field field-mono', placeholder: 'C:\\Tools\\notes-server.exe  or  npx' });
  const args = el('textarea', { id: 'mcp-args', class: 'field field-mono', rows: 2, placeholder: 'one argument per line' });
  const add = el('button', { type: 'button', class: 'btn btn-primary btn-sm' }, 'Add server');

  container.replaceChildren(
    el('header', { class: 'page-head' }, el('div', {}, el('h1', { class: 'page-title' }, 'Plug-in tools'),
      el('div', { class: 'page-sub' }, 'Tools from MCP servers running on this PC'))),
    el('div', { class: 'page-body sections' },
      flash,
      el('div', { class: 'quiet' }, 'A server is a program on this PC that you trust. The assistant uses at most 3 of its tools per question, planned only from what you typed. What a tool returns is read as data, never as instructions.'),
      list,
      el('section', {}, el('div', { class: 'section-h' }, el('h2', {}, 'Add a server')),
        el('div', { class: 'box box-pad stack' },
          el('label', { for: 'mcp-name' }, 'Name'), name,
          el('label', { for: 'mcp-command' }, 'Program'), command,
          el('label', { for: 'mcp-args' }, 'Arguments'), args,
          el('div', { class: 'row-gap' }, add)))),
  );

  const say = (msg, tone) => flash.replaceChildren(msg ? notice(msg, tone) : '');

  /** Runs one change and redraws from the reply; true when it worked. */
  async function run(fn, ok) {
    try {
      draw((await fn()).servers);
      say(ok);
      return true;
    } catch (e) {
      say(e.message, 'error');
      return false;
    }
  }

  function server(s) {
    const on = el('input', { type: 'checkbox', class: 'toggle', role: 'switch', 'aria-label': `Use ${s.name}` });
    on.checked = s.enabled;
    on.addEventListener('change', () => run(() => api.patchMcpServer(s.name, { enabled: on.checked }), on.checked ? `${s.name} is on.` : `${s.name} is off.`));
    const tools = s.tools.map((t) => {
      const ask = el('input', { type: 'checkbox', class: 'check', 'aria-label': `${t.name} asks before running` });
      ask.checked = t.write;
      ask.disabled = t.server_says_write; // the server says it changes things: always asks
      ask.addEventListener('change', () => {
        // every tool the user can see gets an explicit answer, so a name-based guess never overrides what they chose
        const asks = (x) => (x.name === t.name ? ask.checked : x.write);
        const own = s.tools.filter((x) => !x.server_says_write);
        run(() => api.patchMcpServer(s.name, { writes: own.filter(asks).map((x) => x.name), reads: own.filter((x) => !asks(x)).map((x) => x.name) }), '');
      });
      return el('li', { class: 'mcp-tool' },
        el('div', { class: 'row-gap' }, el('span', { class: 'mono' }, t.name), t.write ? badge('Asks first', 'red-outline') : badge('Read-only', 'outline')),
        t.description ? el('div', { class: 'quiet' }, t.description) : '',
        el('label', { class: 'row-gap quiet' }, ask, t.server_says_write ? ' Changes things (the server says so), always asks' : ' Asks before running'));
    });
    return el('div', { class: 'box box-pad stack' },
      el('div', { class: 'row-gap' }, el('strong', {}, s.name), el('span', { class: 'mono quiet' }, [s.command, ...s.args].join(' ')),
        el('label', { class: 'row-gap' }, on, ' On'),
        el('button', { type: 'button', class: 'btn btn-outline btn-sm', onclick: () => {
          if (confirm(`Remove the plug-in server ${s.name}? Its tools stop being used. The program itself is not deleted.`)) run(() => api.removeMcpServer(s.name), `${s.name} removed.`);
        } }, 'Remove')),
      s.error ? notice(`${s.error} Check the program and its arguments, then switch it off and on again.`, 'error') : '',
      s.starting ? el('div', { class: 'quiet' }, 'Starting…') : '',
      s.enabled && !s.starting && !s.error && !tools.length ? el('div', { class: 'quiet' }, 'This server offers no tools.') : '',
      tools.length ? el('ul', { class: 'mcp-tools' }, ...tools) : '');
  }

  function draw(servers) {
    list.replaceChildren(...(servers.length ? servers.map(server) : [el('div', { class: 'quiet' }, 'No plug-in servers yet.')]));
    // the backend starts servers in the background (a broken one can take a minute to give up): look again until they answer
    if (servers.some((s) => s.starting)) setTimeout(() => container.isConnected && api.mcpServers().then((r) => draw(r.servers)).catch(() => {}), 2000);
  }

  add.addEventListener('click', async () => {
    const added = name.value.trim();
    const ok = await run(() => api.addMcpServer({ name: added, command: command.value, args: args.value.split('\n').map((a) => a.trim()).filter(Boolean) }),
      `Added ${added}. It is starting; its tools appear above when it answers. Ones that sound like they change things ask first; untick any that are safe.`);
    if (ok) name.value = command.value = args.value = '';
  });

  run(() => api.mcpServers(), '');
}
