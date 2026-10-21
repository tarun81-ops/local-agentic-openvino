"""Plug-in tools from local MCP servers the user adds in Settings (Model Context Protocol, stdio transport).

A small stdlib client: the server is a local program started with pipes; messages are newline-delimited JSON-RPC
(initialize, tools/list, tools/call). Each server tool is registered in the tool registry as "mcp.<server>.<tool>":
- read-only by default; "write" (an approval card with the exact arguments, nothing runs before APPROVE) when the user
  marks it, or when the server itself declares it is not read-only / destructive;
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
from pathlib import Path

from app.core import prefs
from app.core.assistant.tools import registry

log = logging.getLogger(__name__)

PROTOCOL = "2025-06-18"
MAX_STEPS = 3  # tool calls per question: the 2B model plans badly beyond that
MAX_RESULT = 4000  # characters of a tool result kept for the answer (prompts stay bounded)
TIMEOUT_S = 30
_PREF = "mcp_servers"
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_TYPES = {"string": str, "number": numbers.Real, "integer": int, "boolean": bool, "object": dict, "array": list}


class McpError(RuntimeError):
    pass


class Client:
    """One running MCP server. ponytail: one request at a time per server (a lock); concurrent calls if a slow tool
    ever blocks the chat."""

    def __init__(self, command: list[str]):
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
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
        return self.request("tools/list").get("tools", [])

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()


_clients: dict[str, Client] = {}
_state: dict[str, dict] = {}  # server name -> {"tools": [...], "error": str}
_mutex = threading.Lock()


# ---------- configuration (settings.json) ----------

def servers() -> list[dict]:
    return [{"enabled": True, "args": [], "writes": [], **s} for s in (prefs.get(_PREF) or [])]


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
    item = {"name": name, "command": exe, "args": [str(a) for a in args or []][:20], "enabled": True, "writes": []}
    _save(servers() + [item])
    refresh()
    return item


def update_server(name: str, enabled: bool | None = None, writes: list[str] | None = None) -> None:
    items = servers()
    found = next((s for s in items if s["name"] == name), None)
    if found is None:
        raise KeyError(name)
    if enabled is not None:
        found["enabled"] = bool(enabled)
    if writes is not None:
        found["writes"] = sorted({str(w) for w in writes})
    _save(items)
    refresh()


def remove_server(name: str) -> None:
    _save([s for s in servers() if s["name"] != name])
    refresh()


# ---------- running servers and registering their tools ----------

def _declares_write(tool: dict) -> bool:
    """The server itself says the tool changes things: readOnlyHint false or destructiveHint true, stated explicitly.
    Unannotated tools count as read-only (the user marks them otherwise in Settings)."""
    a = tool.get("annotations") or {}
    return a.get("readOnlyHint") is False or a.get("destructiveHint") is True


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
    result = client.request("tools/call", {"name": tool, "arguments": args})
    text = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text").strip()
    if result.get("isError"):
        raise McpError(text[:300] or "the tool reported an error")
    return text[:MAX_RESULT] or "(the tool returned no text)"


def _register(server: str, tool: dict, write: bool) -> None:
    schema = tool.get("inputSchema") or {}
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    kind = lambda spec: _TYPES.get((spec or {}).get("type"), object)  # noqa: E731 - unknown types are not checked
    name, tname = tool_name(server, tool["name"]), tool["name"]

    def describe(args: dict) -> list[str]:
        lines = [f"Runs the tool {tname} of the plug-in server “{server}” with:"]
        lines += [f"{k}: {json.dumps(v, ensure_ascii=False)[:300]}" for k, v in args.items()] or ["(no arguments)"]
        return lines + (["This tool can change things or send data."] if write else [])

    registry.register(registry.Tool(
        name=name, risk="external" if write else "read",
        required={k: kind(props.get(k)) for k in required}, optional={k: kind(v) for k, v in props.items() if k not in required},
        validate=lambda args: dict(args), describe=describe, run=lambda args: {"text": call(server, tname, args)}))


def refresh() -> dict[str, dict]:
    """Starts the enabled servers (once), lists their tools and re-registers them; stops disabled or removed ones."""
    with _mutex:
        for name in list(registry.REGISTRY):
            if name.startswith("mcp."):
                registry.REGISTRY.pop(name)
        wanted = {s["name"]: s for s in servers() if s["enabled"]}
        for name in list(_clients):
            if name not in wanted:
                _clients.pop(name).close()
                _state.pop(name, None)
        for name, s in wanted.items():
            try:
                if name not in _clients or _clients[name].proc.poll() is not None:
                    _clients[name] = Client([s["command"], *s["args"]])
                    _state[name] = {"tools": _clients[name].start(), "error": ""}
                for t in _state[name]["tools"]:
                    _register(name, t, t["name"] in s["writes"] or _declares_write(t))
            except Exception as exc:
                log.warning("MCP server %s failed: %s", name, exc)
                if name in _clients:
                    _clients.pop(name).close()
                _state[name] = {"tools": [], "error": f"It didn't start: {exc}"}
        return {k: dict(v) for k, v in _state.items()}


def status() -> list[dict]:
    st = refresh()
    out = []
    for s in servers():
        info = st.get(s["name"], {"tools": [], "error": ""})
        out.append({**s, "error": info["error"], "tools": [
            {"name": t["name"], "description": (t.get("description") or "")[:300],
             "write": t["name"] in s["writes"] or _declares_write(t), "server_says_write": _declares_write(t)}
            for t in info["tools"]]})
    return out


def stop_all() -> None:
    with _mutex:
        for c in _clients.values():
            c.close()
        _clients.clear()


# ---------- planning: from the user's own message only ----------

PLAN_PROMPT = """You can use these tools. Decide which of them, if any, the request needs.
Request (from the user): {message}

Tools:
{tools}

Reply with JSON only: {{"calls": [{{"tool": "<name>", "args": {{...}}}}]}}. At most {n} calls; [] if no tool fits."""

_WORD = re.compile(r"[a-z]{4,}")


def plan(message: str) -> list[dict]:
    """Up to MAX_STEPS {"tool", "args"} for this message. The planner sees only the user's message and the tool list,
    never a tool result or file text, so nothing a tool returns can ask for another call. A message that shares no
    word with any tool skips the model call entirely (chat stays as fast as before)."""
    if servers() and not _state:
        refresh()  # first question since the app started: start the plug-in servers now
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
        return []
    calls = data.get("calls") if isinstance(data.get("calls"), list) else []
    return [{"tool": c["tool"], "args": c.get("args") if isinstance(c.get("args"), dict) else {}}
            for c in calls if isinstance(c, dict) and c.get("tool") in tools][:MAX_STEPS]
