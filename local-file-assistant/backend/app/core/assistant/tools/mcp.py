"""Plug-in tools from local MCP servers the user adds in Settings (Model Context Protocol, stdio transport).

A small stdlib client: the server is a local program started with pipes; messages are newline-delimited JSON-RPC
(initialize, tools/list, tools/call). Each server tool is registered in the tool registry as "mcp.<server>.<tool>":
- read-only, except "write" (an approval card with the exact arguments, nothing runs before APPROVE) when the user
  marks it, when the server itself declares it is not read-only / destructive, or when the server says nothing and the
  tool's name is a verb that changes things (delete_note, sendEmail, run...): the user can mark those read-only;
- planned only from the user's own message, once, before any tool runs, at most MAX_STEPS calls (plan());
- its result is untrusted data: it only goes into the answer prompt as quoted text and is never planned on, so text
  inside a result cannot trigger another call."""
from __future__ import annotations

import json
import logging
import numbers
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from app.config import settings
from app.core import prefs
from app.core.assistant.tools import registry

log = logging.getLogger(__name__)

PROTOCOL = "2025-06-18"
MAX_STEPS = 3  # tool calls per question: the 2B model plans badly beyond that
MAX_RESULT = 4000  # characters of a tool result kept for the answer (prompts stay bounded)
TIMEOUT_S = 30
MAX_PAGES = 5  # tools/list pages followed (nextCursor); a server with more tools than that is cut off
LOG_MAX = 1_000_000  # bytes of a server's stderr log before it starts over
_PREF = "mcp_servers"
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_TYPES = {"string": str, "number": numbers.Real, "integer": int, "boolean": bool, "object": dict, "array": list}


class McpError(RuntimeError):
    pass


class Client:
    """One running MCP server. ponytail: one request at a time per server (a lock); concurrent calls if a slow tool
    ever blocks the chat."""

    def __init__(self, command: list[str], name: str = ""):
        self.err = _stderr_log(name) if name else subprocess.DEVNULL
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.err,
                                     text=True, encoding="utf-8", errors="replace", bufsize=1,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.inbox: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        self.next_id = 0
        threading.Thread(target=self._read, daemon=True, name="mcp-read").start()

    def _read(self):
        for line in self.proc.stdout:
            try:
                self.inbox.put(json.loads(line))
            except ValueError:
                pass  # a server that logs to stdout: not a message
        self.inbox.put(None)  # the server exited

    def _send(self, msg: dict) -> None:
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
        except OSError as exc:
            raise McpError(f"the server stopped ({exc})") from exc

    def request(self, method: str, params: dict | None = None, timeout: float = TIMEOUT_S) -> dict:
        with self.lock:
            self.next_id += 1
            rid = self.next_id
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
            while True:
                try:
                    msg = self.inbox.get(timeout=timeout)
                except queue.Empty:
                    raise McpError(f"no answer to {method} within {timeout:.0f} s") from None
                if msg is None:
                    raise McpError("the server stopped")
                if msg.get("id") == rid and ("result" in msg or "error" in msg):
                    if "error" in msg:
                        raise McpError(str(msg["error"].get("message", msg["error"]))[:300])
                    return msg["result"]
                # notifications and requests from the server are ignored (no sampling, no roots)

    def start(self) -> list[dict]:
        self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                    "clientInfo": {"name": "local-file-assistant", "version": "0.2"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools, cursor = [], None
        for _ in range(MAX_PAGES):
            page = self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools += page.get("tools", [])
            cursor = page.get("nextCursor")
            if not cursor:
                break
        return tools

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        if self.err is not subprocess.DEVNULL:
            self.err.close()


def _stderr_log(name: str):
    """logs\\mcp-<server>.log: what the server prints on stderr, so a failing one can be diagnosed. ponytail: the size is
    checked when the server starts (over LOG_MAX it starts over), not while it runs."""
    path = settings.data_dir / "logs" / f"mcp-{name}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    big = path.is_file() and path.stat().st_size > LOG_MAX
    return open(path, "w" if big else "a", encoding="utf-8", errors="replace")


_clients: dict[str, Client] = {}
_state: dict[str, dict] = {}  # server name -> {"tools": [...], "error": str}
_mutex = threading.Lock()  # _clients, _state and the registry's mcp.* entries; held briefly, never while waiting on a server
_refresh_lock = threading.Lock()  # one refresh at a time; only Settings changes wait on it, never chat
_started = False  # the enabled servers were started (or are being) since the app began


# ---------- configuration (settings.json) ----------

def servers() -> list[dict]:
    return [{"enabled": True, "args": [], "writes": [], "reads": [], **s} for s in (prefs.get(_PREF) or [])]


def _save(items: list[dict]) -> None:
    prefs.set(_PREF, items)


def add_server(name: str, command: str, args: list[str]) -> dict:
    name = (name or "").strip().lower()
    if not _NAME.match(name):
        raise ValueError("Name it with letters, digits, - or _ (up to 32), e.g. notes-server.")
    if any(s["name"] == name for s in servers()):
        raise ValueError(f"There is already a server called {name}.")
    exe = (command or "").strip()
    if not (Path(exe).is_file() or shutil.which(exe)):
        raise ValueError(f"{exe or 'The command'} wasn't found on this PC. Give the full path to the program.")
    item = {"name": name, "command": exe, "args": [str(a) for a in args or []][:20], "enabled": True, "writes": [], "reads": []}
    _save(servers() + [item])
    _changed()
    return item


def update_server(name: str, enabled: bool | None = None, writes: list[str] | None = None, reads: list[str] | None = None) -> None:
    """writes: tools the user wants to be asked about; reads: tools the user says are safe to run at once (these win
    over a guess from the tool's name, never over what the server itself declares)."""
    items = servers()
    found = next((s for s in items if s["name"] == name), None)
    if found is None:
        raise KeyError(name)
    if enabled is not None:
        found["enabled"] = bool(enabled)
    if writes is not None:
        found["writes"] = sorted({str(w) for w in writes})
    if reads is not None:
        found["reads"] = sorted({str(r) for r in reads})
    found["reads"] = sorted(set(found["reads"]) - set(found["writes"]))  # asking wins when both are set
    _save(items)
    _changed()


def remove_server(name: str) -> None:
    _save([s for s in servers() if s["name"] != name])
    _changed()


def _changed() -> None:
    """After a Settings change: the tools are re-registered now (a tool just marked "ask first" must never run
    unasked, a removed server's tools must go), and starting or stopping servers happens in the background, so the
    request returns at once; the page polls GET /mcp, which shows a new server as "starting" until it answers."""
    with _mutex:
        _reregister({s["name"]: s for s in servers() if s["enabled"]})
    threading.Thread(target=refresh, daemon=True, name="mcp-refresh").start()


# ---------- running servers and registering their tools ----------

def _declares_write(tool: dict) -> bool:
    """The server itself says the tool changes things: readOnlyHint false or destructiveHint true, stated explicitly."""
    a = tool.get("annotations") or {}
    return a.get("readOnlyHint") is False or a.get("destructiveHint") is True


# ponytail: a word list on the tool's name, no model involved; add words as real servers show what it misses.
_CHANGES = frozenset("delete remove send write update create run exec execute drop kill move rename edit insert upload "
                     "publish push install reset clear post put patch save add set append modify commit email".split())


def _sounds_like_change(name: str) -> bool:
    """delete_note, sendEmail, run-command: a word in the name that is a verb for changing things (not "address", "asset")."""
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name).lower()
    return bool(_CHANGES & set(re.findall(r"[a-z]+", words)))


def _is_write(server: dict, tool: dict) -> bool:
    """Asks before running when the user marked it or the server says it changes things. A tool the server says nothing
    about is judged by its name (an unannotated delete_note must not run on arguments a 2B model wrote) unless the user
    marked it read-only; a tool the server declares readOnlyHint: true is read-only."""
    name = tool["name"]
    if name in server["writes"] or _declares_write(tool):
        return True
    if name in server["reads"] or (tool.get("annotations") or {}).get("readOnlyHint") is True:
        return False
    return _sounds_like_change(name)


def _shown(value) -> str:
    """An argument as the approval card shows it: everything up to 300 characters, then how much more will be sent."""
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= 300 else f"{text[:300]}… ({len(text) - 300} more characters are sent)"


def tool_name(server: str, tool: str) -> str:
    return f"mcp.{server}.{tool}"


def label(name: str) -> str:
    _, server, tool = name.split(".", 2)
    return f"{tool} ({server})"


def call(server: str, tool: str, args: dict) -> str:
    """Runs one tool; its text content, capped. Raises McpError (shown to the user in plain words)."""
    client = _clients.get(server)
    if client is None or client.proc.poll() is not None:
        raise McpError(f"the plug-in server {server} isn't running")
    t, ok = time.monotonic(), False
    try:
        result = client.request("tools/call", {"name": tool, "arguments": args})
        text = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text").strip()
        if result.get("isError"):
            raise McpError(text[:300] or "the tool reported an error")
        ok = True
        return text[:MAX_RESULT] or "(the tool returned no text)"
    finally:  # argument names only: their values may be file text or private data
        log.info("plug-in tool call: server=%s tool=%s args=%s %.2fs %s", server, tool, ",".join(sorted(args)) or "-",
                 time.monotonic() - t, "ok" if ok else "failed")


def _register(server: str, tool: dict, write: bool) -> None:
    schema = tool.get("inputSchema") or {}
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    kind = lambda spec: _TYPES.get((spec or {}).get("type"), object)  # noqa: E731 - unknown types are not checked
    name, tname = tool_name(server, tool["name"]), tool["name"]

    def describe(args: dict) -> list[str]:
        lines = [f"Runs the tool {tname} of the plug-in server “{server}” with:"]
        lines += [f"{k}: {_shown(v)}" for k, v in args.items()] or ["(no arguments)"]
        return lines + (["This tool can change things or send data."] if write else [])

    registry.register(registry.Tool(
        name=name, risk="external" if write else "read",
        required={k: kind(props.get(k)) for k in required}, optional={k: kind(v) for k, v in props.items() if k not in required},
        validate=lambda args: dict(args), describe=describe, run=lambda args: {"text": call(server, tname, args)}))


def refresh() -> dict[str, dict]:
    """Starts the enabled servers that aren't running, lists their tools and re-registers them; stops disabled or
    removed ones. A server that hangs takes up to 2 x TIMEOUT_S to give up, so _mutex (which chat reads the registry
    under) is only held for the quick bookkeeping, never while waiting on a server."""
    global _started
    with _refresh_lock:
        _started = True
        wanted = {s["name"]: s for s in servers() if s["enabled"]}
        with _mutex:
            for name in [n for n in _clients if n not in wanted]:
                _clients.pop(name).close()
            for name in [n for n in _state if n not in wanted]:
                _state.pop(name)
        for name, s in wanted.items():
            if name in _clients and _clients[name].proc.poll() is None:
                continue
            client = None
            try:
                client = Client([s["command"], *s["args"]], name)
                state = {"tools": client.start(), "error": ""}
            except Exception as exc:
                log.warning("MCP server %s failed: %s", name, exc)
                if client:
                    client.close()
                client, state = None, {"tools": [], "error": f"It didn't start: {exc}"}
            with _mutex:
                _clients.pop(name, None)  # a stopped one
                if client:
                    _clients[name] = client
                _state[name] = state
        with _mutex:
            _reregister({s["name"]: s for s in servers() if s["enabled"]})  # read again: Settings may have changed meanwhile
            return {k: dict(v) for k, v in _state.items()}


def _reregister(wanted: dict[str, dict]) -> None:
    """The registry's mcp.* entries rebuilt from what the servers listed. Caller holds _mutex."""
    for name in [n for n in registry.REGISTRY if n.startswith("mcp.")]:
        registry.REGISTRY.pop(name)
    for name, s in wanted.items():
        for t in _state.get(name, {}).get("tools", []):
            _register(name, t, _is_write(s, t))


def start_background() -> None:
    """Starts the enabled servers in a thread, once per app run (later changes go through refresh() from Settings).
    Chat, GET /mcp and app start call this and never wait: a broken server needs up to 2 x TIMEOUT_S to give up."""
    global _started
    with _mutex:
        if _started or not servers():
            return
        _started = True
    threading.Thread(target=refresh, daemon=True, name="mcp-start").start()


def status() -> list[dict]:
    """What is known now. Never starts a server or waits for one; "starting" = on, but no answer from it yet."""
    start_background()
    with _mutex:
        st = {k: dict(v) for k, v in _state.items()}
    out = []
    for s in servers():
        info = st.get(s["name"])
        out.append({**s, "starting": s["enabled"] and info is None, "error": info["error"] if info else "", "tools": [
            {"name": t["name"], "description": (t.get("description") or "")[:300],
             "write": _is_write(s, t), "server_says_write": _declares_write(t)}
            for t in (info["tools"] if info else [])]})
    return out


def stop_all() -> None:
    global _started
    with _mutex:
        for c in _clients.values():
            c.close()
        _clients.clear()
        _started = False


# ---------- planning: from the user's own message only ----------

PLAN_PROMPT = """You can use these tools. Decide which of them, if any, the request needs.
Request (from the user): {message}

Tools:
{tools}

Reply with JSON only: {{"calls": [{{"tool": "<name>", "args": {{...}}}}]}}. At most {n} calls; [] if no tool fits."""

_WORD = re.compile(r"[a-z]{4,}")


def plan(message: str, notes: list[str] | None = None) -> list[dict]:
    """Up to MAX_STEPS {"tool", "args"} for this message. The planner sees only the user's message and the tool list,
    never a tool result or file text, so nothing a tool returns can ask for another call. A message that shares no
    word with any tool skips the model call entirely (chat stays as fast as before). Planning always runs on this PC
    (llm_json local=True); when that engine is down the plan is skipped and `notes` gets a line for the step log."""
    start_background()  # first question since the app started: servers still starting offer no tools yet; chat doesn't wait
    with _mutex:
        tools = {n: t for n, t in registry.REGISTRY.items() if n.startswith("mcp.")}
    if not tools:
        return []
    words = set(_WORD.findall(message.lower()))
    entries = []
    for n in tools:
        _, server, tname = n.split(".", 2)
        desc = next((t.get("description") or "" for t in _state.get(server, {}).get("tools", []) if t["name"] == tname), "")
        if words & set(_WORD.findall(f"{tname.replace('_', ' ')} {desc}".lower())):
            args = ", ".join(f"{k}: {getattr(v, '__name__', 'any')}" for k, v in {**tools[n].required, **tools[n].optional}.items())
            entries.append(f"- {n}: {desc[:160]} (args: {args or 'none'})")
    if not entries:
        return []
    from app.core.llm.client import llm_json

    try:
        data = llm_json(PLAN_PROMPT.format(message=message[:1000], tools="\n".join(entries[:20]), n=MAX_STEPS))
    except Exception as exc:
        log.info("tool planning skipped: %s", exc)
        if notes is not None:
            notes.append("Plug-in tools skipped: the model on this PC isn't answering")
        return []
    calls = data.get("calls") if isinstance(data.get("calls"), list) else []
    return [{"tool": c["tool"], "args": c.get("args") if isinstance(c.get("args"), dict) else {}}
            for c in calls if isinstance(c, dict) and c.get("tool") in tools][:MAX_STEPS]
