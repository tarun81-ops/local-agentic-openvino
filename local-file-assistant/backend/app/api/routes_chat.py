import json
import re
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.core import doc_kinds, doc_photos, owner, pc_access, personalize, ram
from app.core.parsers import pdf_parser
from datetime import datetime

from app.core.assistant import actions, chat_recall, conversation, file_agent, memory_store, prompts, tasks, workspaces
from app.core.assistant.tools import intents
from app.core.assistant.tools.registry import ToolError
from app.core import calc, facts, find_file
from app.core.assistant.router import memory_command, organize_intent, needs_rewrite, route, task_intent
from app.core.llm.answerer import answer_stream, chat_stream, rewrite_query
from app.core.llm.verifier import STRICT_THRESHOLD, THRESHOLD, excerpt, verify
from app.core.search.hybrid import hybrid_search
from app.db import sqlite_fts
from app.config import settings
from pathlib import Path

log = logging.getLogger(__name__)
MAX_ATTACH = 3  # files per message: the 2B model reads a few passages, not a pile of documents
router = APIRouter(prefix="/chat", tags=["chat"])


class ChatRequest(BaseModel):
    message: str
    conversation_id: int | None = None
    mode: str = "auto"  # "auto" | "files" | "files_only" | "chat"
    root: str | None = None
    workspace_id: int | None = None  # a new chat's workspace; an existing chat keeps its own
    attach: list[str] = []  # files dropped on the chat: read for this answer only, never indexed
    model: str | None = None
    style: str | None = None  # one-question override of the saved answer style
    language: str | None = None  # same, for the reply language
    context: dict | None = None  # {app, title, selection} captured from the window in front


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _public(chunk: dict) -> dict:
    return {k: chunk[k] for k in ("path", "name", "loc_kind", "loc_no", "snippet", "match")}


def _pc_lookup(query: str, message: str, kind: str, chunks: list[dict], who: dict | None = None, within: tuple = ()):
    """Whole-PC access: when it is on and the question is about a document, find files by name across the
    enabled drives and read the best ones live. Yields progress events and returns (chunks, kind). Never
    raises: a failure here leaves the normal search results as they were.
    who: {"name": the user's name or "", "exclude": paths, "others": filled with other people's files} for
    questions about the user's own documents (see core/owner.py)."""
    if not pc_access.enabled() or not (kind == "files" or file_agent.agent_worthy(message)):
        return chunks, kind
    gen = (file_agent.gather(query, content_hits=chunks, owner_name=who["name"], exclude=tuple(who["exclude"]), within=within) if who is not None
           else file_agent.gather(query, content_hits=chunks, within=within))
    try:
        while True:
            try:
                yield _sse("pc_step", next(gen))
            except StopIteration as stop:
                found = stop.value or {"chunks": []}
                break
    except Exception:
        log.exception("PC file agent failed")
        return chunks, kind
    if who is not None:
        who["locked"].extend(p for p in found.get("locked", []) if p not in who["locked"])
    if who is not None:
        who["others"].extend(found.get("others", []))
        if any(c.get("owner") == "yes" for c in found["chunks"]):  # the user's own document was found: only that
            return found["chunks"], "files"
        skip = {o["path"] for o in who["others"]} | set(who["exclude"])
        chunks = [c for c in chunks if c["path"] not in skip]
        if who["others"]:  # documents of the right kind were found, just not in the user's name: still a file answer
            kind = "files"
    have = {(c["path"], c["loc_kind"], int(c["loc_no"])) for c in chunks}
    extra = [c for c in found["chunks"] if (c["path"], c["loc_kind"], int(c["loc_no"])) not in have]
    return (chunks + extra, "files") if extra else (chunks, kind)


# ---------- whose document (core/owner.py) ----------

# "you fetched another person's marksheet", "this is not mine", "kisi aur ka hai"
_WRONG_PERSON = re.compile(
    r"\b(?:another|other|someone|somebody|different)\s*(?:person|one|guy|student|else)?'?s?\b.*\b(?:marksheet|file|document|result|pdf|certificate|one)"
    r"|\bnot\s+(?:mine|my|me|of mine)\b|\bisn'?t\s+mine\b|\bwrong\s+(?:person|student|name|marksheet|file|document)"
    r"|\b(?:kisi|kisi aur|dusre|doosre)\b.*\b(?:ka|ki|ke)\b|\bmera\s+nahi\b",
    re.I,
)
# A new question, not a clue: a question mark, a question word at the start ("which is in jpg" is not one), or Hindi ones.
_ASKS = re.compile(r"\?|^\s*(?:what|which|whats|what's|how|when|where|who|why|tell|show|give|find|search|open|can you|could you|list)\b"
                   r"|\b(?:kya|kitna|kitne|kaun|kab|kahan|batao|bata)\b", re.I)
_NAMES_TYPE = re.compile(r"\b(?:jpe?g|png|photo|image|picture|pic|scan(?:ned)?|pdf|docx?|word)\b", re.I)


def _last_file_question(conv: dict | None) -> tuple[str, list[str]]:
    """The previous question that was answered from files, and the files that answer used."""
    if not conv:
        return "", []
    msgs = conv["messages"]
    for i in range(len(msgs) - 1, 0, -1):
        m = msgs[i]
        if m.get("role") == "assistant" and (m.get("meta") or {}).get("route") == "files" and msgs[i - 1].get("role") == "user":
            q = msgs[i - 1]["content"]
            meta = m.get("meta") or {}
            cited = [c["path"] for c in meta.get("citations", []) if c.get("path")]
            used = list(dict.fromkeys(cited)) or list(meta.get("files", []))[:1]  # the file the answer came from, not every file read
            return q, used
    return "", []


def _follow_up(message: str, conv: dict | None, named: str) -> dict | None:
    """A correction or a clue for the previous file question, not a new question:
      "you fetched another person's marksheet, my name is tarun verma"  -> ask it again, in that name, without those files
      "i have my class 12 marksheet which is in jpg format"            -> ask it again, looking at photos
    Returns {"search": query, "ask": question to answer, "exclude": paths} or None."""
    prev, used = _last_file_question(conv)
    if not prev:
        return None
    wrong = bool(_WRONG_PERSON.search(message))
    if not wrong and facts.refers_back(message):  # "recheck this pdf": stays on the same file (the pinned path below)
        return None
    statement = not _ASKS.search(message)
    if not (wrong or (named and statement) or (statement and _NAMES_TYPE.search(message))):
        return None
    if _ASKS.search(message) and not wrong:
        return None
    clue = " ".join(w for w in re.findall(r"[\w.]+", message) if _NAMES_TYPE.fullmatch(w) or re.fullmatch(r"(?:class|std)|\d{1,2}(?:th)?|marksheet|certificate|result", w, re.I))
    return {"search": f"{prev} {clue}".strip(), "ask": prev, "exclude": used if wrong else []}


def _owner_tag(chunks: list[dict], name: str) -> None:
    """Whose document each passage is from, for passages the file agent did not read (the folder index)."""
    by_path: dict[str, list[dict]] = {}
    for c in chunks:
        if "owner" not in c:
            by_path.setdefault(c["path"], []).append(c)
    for group in by_path.values():
        verdict, people = owner.check("\n".join(c.get("text", "") for c in group), name)
        for c in group:
            c["owner"], c["people"] = verdict, people


def _not_yours_reply(name: str, others: list[dict]) -> str:
    seen = []
    for o in others:
        line = f"- {o['name']}: in the name of {', '.join(o['people']) or 'someone else'}"
        if line not in seen:
            seen.append(line)
    text = (f"I couldn't find this in a document in your name ({name}). These matched, but they are other people's:\n"
            + "\n".join(seen[:5]))
    return text + "\n\n" + _photo_hint()


def _photo_hint() -> str:
    try:
        if not doc_photos.enabled():
            return "If yours is a photo (a .jpg from your phone), turn on whole-PC access so I can read document photos."
        p = doc_photos.progress()
    except Exception:
        return ""
    if p["photos"] and p["read"] < p["photos"]:
        return (f"If yours is a photo (a .jpg from your phone), I'm still reading photos for text in the background: "
                f"{p['read']} of {p['photos']} done. Ask again in a while, or tell me the photo's file name.")
    return "If yours is a photo, tell me its file name or the folder it is in."


_NOT_FOUND = re.compile(
    r"\b(?:couldn'?t|could not|can'?t|cannot|unable to|did not|didn'?t|does not|doesn'?t|do not|don'?t)\s+(?:find|locate|see|contain|provide|mention|have)"
    r"|\bnot\s+(?:found|available|mentioned|provided|present|given)\b|\bno\s+(?:information|mention|details|marksheet|document|file)s?\b",
    re.I,
)


FILES_ONLY_REFUSAL = ("I couldn't find this in your files, so I'm not answering it from general knowledge. "
                      "Try other words, or pick AUTO for a general answer.")


def files_only(answer: str, result: dict) -> tuple[str, dict]:
    """FILES ONLY mode: an answer is kept only when at least one of its citations is verified against the files, or
    when it already says the files don't have it. Anything else may come from the model's general knowledge (a small
    model ignores "use only the excerpts" when it knows the answer), so it is replaced before it is shown.
    ponytail: one verified citation keeps the whole answer; its uncited sentences are still marked by the UI."""
    if says_not_found(answer) or any(c.get("verified") for c in result.get("citations", [])):
        return answer, result
    return FILES_ONLY_REFUSAL, {**result, "citations": [], "uncited": [], "all_verified": False}


def says_not_found(answer: str) -> bool:
    """The answer says the files don't have it: a source next to that reads as evidence for a non-answer."""
    return bool(_NOT_FOUND.search(answer or "")) and len(answer) < 400


def _title_chunks(path: str, message: str, explicit: bool = False) -> list[dict]:
    """Passages of one file found by its name: from the index when it is indexed, else read live (PC access).
    explicit: a file the user dropped on this chat, readable even outside PC access (pc_access.can_read)."""
    from app.core.search.hybrid import snippet

    terms = [w for w in find_file.fuzzy.title_words(message)]
    conn = sqlite_fts.connect(settings.db_path)
    try:
        rows, _ = sqlite_fts.file_chunks(conn, path, budget=6000)
    finally:
        conn.close()
    if rows:
        page = facts.wanted_page(message)
        if page is not None:  # "page 12": that page, not an even spread of the file
            nums = sorted({int(r["loc_no"]) for r in rows})
            target = nums[-1] if page == "last" else nums[0] if page == "first" else page
            rows = [r for r in rows if int(r["loc_no"]) == target] or rows
        return [{"path": path, "name": Path(path).name, "loc_kind": r["loc_kind"], "loc_no": r["loc_no"], "chunk_no": i,
                 "text": r["text"], "snippet": snippet(r["text"], terms), "match": "name"} for i, r in enumerate(rows)]
    got, _problem = file_agent.read_chunks(path, message, terms, explicit=explicit)
    for c in got:
        c["match"] = "name"
    return got


def _title_lookup(message: str, kind: str, chunks: list[dict], within: tuple = ()):
    """Nothing in the files matched the words, but the message may be a file's title typed by sound
    ("gunaho ke devata" for "Gunahon Ka Devta.pdf"). Yields progress; returns (chunks, kind). Never raises."""
    try:
        hits = [h for h in find_file.title_hits(message) if not within or pc_access.under(h["path"], list(within))]
        if not hits:
            return chunks, kind
        found: list[dict] = []
        for h in hits:
            got = _title_chunks(h["path"], message)
            if got:
                found.extend(got)
                yield _sse("pc_step", {"step": "name", "detail": f"Matched by file name: {h['name']}"})
                break  # one file: the title named one book or document
        return (found, "files") if found else (chunks, kind)
    except Exception:
        log.exception("title lookup failed")
        return chunks, kind


def _marks_reply(message: str, chunks: list[dict]) -> str | None:
    """Percentage / total questions about a marksheet: the sums are done in code (app.core.calc), not by the model.
    None when no marksheet among the results could be read, so the normal answer path runs."""
    by_file: dict[str, list[dict]] = {}
    for c in chunks:
        by_file.setdefault(c["path"], []).append(c)
    want = re.search(r"(?:class|std|standard)\s*(\d{1,2})|(\d{1,2})\s*(?:th|st|nd|rd)", message, re.I)
    num = (want.group(1) or want.group(2)) if want else None
    paths = [p for p in by_file if not num or num in Path(p).name] or list(by_file)
    out = []
    for path in paths:
        group = sorted(by_file[path], key=lambda c: (int(c["loc_no"]), c.get("chunk_no", 0)))
        text = " ".join(c["text"] for c in group)
        part = calc.answer_for(Path(path).name, int(group[0]["loc_no"]), text)
        if part:
            out.append(part)
        if len(out) == 2:
            break
    return "\n\n".join(out) if out else None



def _summary_reply(message: str, chunks: list[dict]) -> str | None:
    """"summarise my class 10 result": the sheet's facts read out in code (calc.summary_for). None if no marksheet."""
    by_file: dict[str, list[dict]] = {}
    for c in chunks:
        by_file.setdefault(c["path"], []).append(c)
    for path, group in by_file.items():
        group = sorted(group, key=lambda c: (int(c["loc_no"]), c.get("chunk_no", 0)))
        reply = calc.summary_for(Path(path).name, int(group[0]["loc_no"]), "\n".join(c["text"] for c in group))
        if reply:
            return reply
    return None


# ---------- numbers the model wrote that no file contains ----------

_NUMBER = re.compile(r"(?<![\w/.-])(\d[\d/.\-]{2,}\d|\d{3,})(?![\w/-])")


def unfound_numbers(answer: str, chunks: list[dict], question: str = "") -> list[str]:
    """Numbers of 3+ digits (roll numbers, amounts, dates) in the answer that appear in none of the passages read.
    Citations and the question itself are left out. A small model copies a roll number from the wrong passage or
    makes one up; this is the check a person would do."""
    from app.core.llm.verifier import CITATION_RE

    body = CITATION_RE.sub(" ", answer or "")
    haystack = " ".join(c.get("text", "") for c in chunks)
    squeezed = re.sub(r"[\s,]", "", haystack)
    missing = []
    for num in dict.fromkeys(_NUMBER.findall(body)):
        num = num.rstrip(".")
        if num in question or num in haystack or re.sub(r"[,\s]", "", num) in squeezed:
            continue
        if re.fullmatch(r"\d{1,3}(?:\.\d+)?", num):  # small numbers and percentages may be worked out
            continue
        missing.append(num)
    return missing


# ---------- password-protected files ----------

_PASSWORD = re.compile(r"\b(?:password|passcode|pwd|pass\s*word)\b\s*(?:is|:|=|hai|h)?\s*[\"'\u201c]?([^\s\"'\u201d]{3,64})", re.I)


def _locked_reply(paths: list[str]) -> str:
    names = ", ".join(Path(p).name for p in paths[:3])
    hint = ""
    if any("aadhaar" in Path(p).name.lower() or "aadhar" in Path(p).name.lower() for p in paths):
        hint = (" (For an e-Aadhaar PDF it is the first 4 letters of your name in CAPITALS followed by your birth year, "
                "for example ABCD1990.)")
    return (f"{names} {'is' if len(paths) == 1 else 'are'} password-protected, so I can't read {'it' if len(paths) == 1 else 'them'} yet.{hint} "
            f"Type \u201cthe password is \u2026\u201d and I'll open it. I use it only until the app closes, never save it, "
            f"and never show it to the AI model.")


def _last_locked(conv: dict | None) -> tuple[list[str], str]:
    """Locked files the previous answer asked a password for, and the question that answer was for."""
    if not conv:
        return [], ""
    msgs = conv["messages"]
    for i in range(len(msgs) - 1, 0, -1):
        m = msgs[i]
        if m.get("role") == "assistant":
            locked = (m.get("meta") or {}).get("locked") or []
            q = msgs[i - 1]["content"] if msgs[i - 1].get("role") == "user" else ""
            return [p for p in locked if Path(p).is_file()], q
    return [], ""


def _paths(chunks: list[dict]) -> list[str]:
    """The files these passages come from, best match first, each once (saved with the answer for follow-ups)."""
    return list(dict.fromkeys(c["path"] for c in chunks if c.get("path")))[:3]


def _model_error(exc: Exception) -> str:
    """What to tell the person when the model call failed. The model server not running is the usual cause:
    start it in the background and say so, instead of a bare "Connection error"."""
    from app.core.llm import ovms_runner

    if "connect" not in str(exc).lower() and "refused" not in str(exc).lower():
        return f"The local model didn't answer: {exc}"
    before = ovms_runner.status()["status"]
    ovms_runner.ensure_running_async()
    if before == "missing" or not settings.ovms_autostart:
        detail = ovms_runner.status()["detail"] or "start it with scripts\\ovms-start.ps1"
        return f"The model server (OpenVINO) isn't running: {detail}."
    return "The model server (OpenVINO) wasn't running, so I'm starting it now. Ask again in about a minute."


def _direct_reply(message: str, chunks: list[dict]) -> str | None:
    """Questions answered in code instead of by the model: marks arithmetic, IFSC codes. None = use the model."""
    if calc.wanted(message):
        reply = _marks_reply(message, chunks)
        if reply:
            return reply
    if calc.asks_summary(message):
        reply = _summary_reply(message, chunks)
        if reply:
            return reply
    if facts.asks_ifsc(message):
        return facts.ifsc_reply(message, chunks)
    return None


def _generate(deltas):
    """Drives an answer generator: yields ("token", delta) pairs, then ("end", truncated)."""
    while True:
        try:
            delta = next(deltas)
        except StopIteration as stop:  # the stream's return value: cut off at the token cap?
            yield "end", bool(stop.value)
            return
        yield "token", delta


def _recall(message: str) -> list[dict]:
    try:
        return memory_store.recall(message)
    except Exception:  # memory is a bonus: a failure must never block an answer
        log.exception("memory recall failed")
        return []


def _recall_chats(message: str, conv: dict | None) -> list[dict]:
    """Relevant exchanges from earlier conversations (never the current one). Same rule as memory:
    a failure here must never block an answer."""
    try:
        return chat_recall.recall(message, exclude_conv=conv["id"] if conv else None)
    except Exception:
        log.exception("chat recall failed")
        return []


def _folder_style(root: str | None, chunks: list[dict]) -> str:
    """The answer style of the folder being asked about (the request's root, else the top result's
    folder), or "" when it has none. Never lets a lookup problem block an answer."""
    try:
        conn = sqlite_fts.connect(settings.db_path)
        try:
            found = root or (sqlite_fts.root_for(conn, Path(chunks[0]["path"])) if chunks else None)
        finally:
            conn.close()
        return personalize.folder_profile(found).get("answer_style", "") if found else ""
    except Exception:
        log.exception("folder style lookup failed")
        return ""


def _memory_reply(kind: str, text: str) -> str:
    if kind == "remember":
        return f'Remembered: "{text}".' if memory_store.add(text) else "I already remember that."
    gone = memory_store.forget(text)
    return f'Forgot: "{gone}".' if gone else "I don't have a memory matching that."


def _proposal_text(p: dict) -> str:
    what = "event" if p["kind"] == "event" else "reminder" if p["when"] else "task"
    if p["when"] is None:
        return f"I can add that {what}: \u201c{p['title']}\u201d, with no time yet. Confirm or set a time below."
    at = datetime.fromtimestamp(p["when"]).strftime("%a %d %b" if p["all_day"] else "%a %d %b, %I:%M %p")
    note = " I assumed 9:00 as no time was given; check it." if p["confidence"] == "date_only" and not p["all_day"] else ""
    return f"I can set that {what}: \u201c{p['title']}\u201d, {at}.{note} Confirm or edit it below."


def _action_reply(req: ChatRequest, conv: dict | None, act: dict):
    """Stores a validated proposal and shows its card. Nothing runs until the user approves it."""
    yield _sse("route", {"route": "action"})
    try:
        proposal = actions.propose(act["tool"], act["args"], conv["id"] if conv else None)
        reply = "I can do that. Check the exact details below, then approve."
        yield _sse("tool_proposal", proposal)
    except ToolError as exc:
        reply = f"I can't do that: {exc}"
    yield _sse("token", {"delta": reply})
    yield from _finish(req, conv, {"citations": [], "uncited": [], "all_verified": False, "answer": reply, "truncated": False, "route": "action"})


def _stream(req: ChatRequest, conv: dict | None):
    act = None if memory_command(req.message) or task_intent(req.message) else intents.from_message(req.message)
    if act:  # from the user's own words only; model output and file text are never turned into actions
        yield from _action_reply(req, conv, act)
        return
    intent = task_intent(req.message)
    if intent and not memory_command(req.message):  # a reminder or event: propose it, save only on confirm
        proposal = tasks.parse_message(req.message, intent)
        reply = _proposal_text(proposal)
        yield _sse("route", {"route": "task"})
        yield _sse("action", {"proposal": proposal})
        yield _sse("token", {"delta": reply})
        yield from _finish(req, conv, {"citations": [], "uncited": [], "all_verified": False, "answer": reply, "truncated": False, "route": "task"})
        return
    cmd = memory_command(req.message)
    if cmd:  # "remember that ..." / "forget ...": handled here, no model involved
        reply = _memory_reply(*cmd)
        yield _sse("route", {"route": "memory"})
        yield _sse("token", {"delta": reply})
        yield from _finish(req, conv, {"citations": [], "uncited": [], "all_verified": False, "answer": reply, "truncated": False, "route": "memory"})
        return
    if conv and req.mode != "chat" and facts.asks_source(req.message):
        src = facts.source_reply(conv["messages"])
        if src:  # "which file was that from?": the file the last answer used, not a fresh search
            text, cits = src
            yield _sse("route", {"route": "files"})
            yield _sse("token", {"delta": text})
            yield from _finish(req, conv, {"citations": cits, "uncited": [], "all_verified": all(c.get("verified") for c in cits),
                                           "answer": text, "truncated": False, "route": "files"})
            return
    history = conversation.window(conv["messages"]) if conv else []
    pinned: list[str] = []
    unlock_q = ""
    pw = _PASSWORD.search(req.message)
    if pw:
        locked, unlock_q = _last_locked(conv)
        secret = pw.group(1)
        req.message = req.message.replace(secret, "\u2022" * 6)  # never stored, never sent to the model
        if locked:
            opened = [p for p in locked if pdf_parser.unlock(p, secret)]
            if not opened:
                reply = "That password didn't open " + ", ".join(Path(p).name for p in locked[:3]) + ". Check it and try again."
                yield _sse("route", {"route": "files"})
                yield _sse("token", {"delta": reply})
                yield from _finish(req, conv, {"citations": [], "uncited": [], "all_verified": False, "answer": reply,
                                               "truncated": False, "route": "files", "locked": locked})
                return
            pinned = opened
        else:
            unlock_q = ""
    named = owner.learn(req.message)  # "my name is ...": the user's own documents are picked by this name from now on
    follow = _follow_up(req.message, conv, named) if req.mode != "chat" else None
    if named and not follow and len(req.message.split()) <= 8 and not _ASKS.search(req.message):
        reply = f"Got it, {named}. When you ask about your own documents (marksheet, roll no, certificates), I'll pick the ones in your name."
        yield _sse("route", {"route": "chat"})
        yield _sse("token", {"delta": reply})
        yield from _finish(req, conv, {"citations": [], "uncited": [], "all_verified": False, "answer": reply, "truncated": False, "route": "chat"})
        return
    ask = follow["ask"] if follow else unlock_q or req.message  # the question being answered (a correction re-asks the last one)
    who = {"name": owner.get(), "exclude": follow["exclude"] if follow else [], "others": [], "locked": []} if owner.about_me(ask) else None
    if follow:
        yield _sse("pc_step", {"step": "plan", "detail": f"Asking again: \u201c{ask}\u201d"
                                + (f" in the name of {who['name']}" if who and who["name"] else "")})
    ws = workspaces.get(req.workspace_id)
    within = tuple(ws["roots"]) if ws else ()  # [] = the whole PC: no limit
    chunks: list[dict] = []
    strict = req.mode == "files_only"  # held back and checked before it is shown (files_only)
    kind = "files" if strict else req.mode if req.mode in ("files", "chat") else "auto"
    if kind == "auto" and organize_intent(req.message):
        kind = "chat"  # clean-up request: the role points to Tidy-up; a file search would only find noise
    # "this pdf", "recheck", "that's wrong": stay on the file the previous answer read. A fresh search on
    # those words lands on whichever other file matches them best.
    if conv and kind != "chat" and not follow and not pinned and facts.refers_back(req.message):
        pinned = [p for p in facts.last_files(conv["messages"]) if Path(p).is_file()]

    if req.attach:  # files dropped on the chat: read now, answered from with citations, never added to the index
        kind = "files"
        yield _sse("route", {"route": kind})
        problems, locked = [], []
        for path in list(dict.fromkeys(req.attach))[:MAX_ATTACH]:
            yield _sse("pc_step", {"step": "name", "detail": f"Reading the file you added: {Path(path).name}"})
            got, problem = file_agent.read_chunks(path, ask, find_file.fuzzy.title_words(ask), explicit=True)
            if problem.startswith(file_agent.LOCKED):
                locked.append(path)
            elif problem:
                problems.append(f"{Path(path).name}: {problem}")
            for c in got:
                c["match"] = "name"
            chunks.extend(got)
        if locked and not chunks:
            text = _locked_reply(locked)
            yield _sse("token", {"delta": text})
            yield from _finish(req, conv, {"citations": [], "uncited": [], "all_verified": False, "answer": text,
                                           "truncated": False, "route": kind, "locked": locked})
            return
        if not chunks:
            text = "I couldn't read the file you added. " + " ".join(problems)
            yield _sse("token", {"delta": text})
            yield from _finish(req, conv, {"citations": [], "uncited": [], "all_verified": False, "answer": text,
                                           "truncated": False, "route": kind})
            return
        pinned = list(dict.fromkeys(c["path"] for c in chunks))
        yield _sse("results", {"results": [_public(c) for c in chunks], "semantic": False})
    elif pinned:
        kind = "files"
        yield _sse("route", {"route": kind})
        yield _sse("pc_step", {"step": "name", "detail": f"Same file as before: {Path(pinned[0]).name}"})
        dropped = {a for m in (conv["messages"] if conv else []) for a in (m.get("meta") or {}).get("attached", [])}
        for path in pinned:
            try:
                chunks.extend(_title_chunks(path, ask, explicit=path in dropped))
            except Exception:
                log.exception("reading the previous file failed")
        if chunks:
            yield _sse("results", {"results": [_public(c) for c in chunks], "semantic": False})
    elif kind != "chat":
        # Search resolves before any LLM call, so the UI shows matching files straight away
        # even while the model is still loading.
        if follow:
            query, kind = follow["search"], "files"
        else:
            query = rewrite_query(req.message, history, req.model) if history and needs_rewrite(req.message) else req.message
        try:
            found = hybrid_search(query, root=req.root, limit=6, within=list(within) or None)
        except Exception as exc:  # e.g. a locked or damaged index: say so, don't leave "Searching…" up
            log.exception("search failed")
            yield _sse("error", {"detail": f"Couldn't search your files: {exc}"})
            return
        chunks = [c for c in found["results"] if not who or c["path"] not in set(who["exclude"])]
        kind = route(kind, ask, chunks)
        yield _sse("route", {"route": kind})
        if kind == "files":
            yield _sse("results", {"results": [_public(c) for c in chunks], "semantic": found["semantic"]})
        before, was = len(chunks), kind
        chunks, kind = yield from _pc_lookup(query, ask, kind, chunks, who, within)
        if kind != was:
            yield _sse("route", {"route": kind})
        if len(chunks) != before:
            yield _sse("results", {"results": [_public(c) for c in chunks], "semantic": found["semantic"]})
        if kind == "chat" or not chunks:
            was = kind
            chunks, kind = yield from _title_lookup(req.message, kind, chunks, within)
            if kind != was:
                yield _sse("route", {"route": kind})
            if kind == "files" and chunks:
                yield _sse("results", {"results": [_public(c) for c in chunks], "semantic": found["semantic"]})
    else:
        yield _sse("route", {"route": "chat"})

    if who is not None and kind == "files" and chunks:
        _owner_tag(chunks, who["name"])
        if who["name"]:
            mine = [c for c in chunks if c.get("owner") == "yes"]
            theirs = [c for c in chunks if c.get("owner") == "no"]
            for c in theirs:
                if c["path"] not in {o["path"] for o in who["others"]}:
                    who["others"].append({"name": c["name"], "path": c["path"], "people": c.get("people", [])})
            chunks = mine or [c for c in chunks if c.get("owner") != "no"]
    wanted_class = doc_kinds.asked_class(ask)
    if wanted_class is not None and kind == "files" and chunks:  # "class 12": drop passages that are only about class 10
        signals = [doc_kinds.class_signal(c.get("text", ""), wanted_class) for c in chunks]
        if any(s > 0 for s in signals):
            chunks = [c for c, s in zip(chunks, signals) if s >= 0]
    if who is not None and kind == "chat":  # "what is my class 12 roll no" with nothing found: never a made-up number
        kind = "files"
        yield _sse("route", {"route": kind})
    if who is not None and kind == "files" and not chunks and who["locked"]:
        text = _locked_reply(who["locked"])
        yield _sse("token", {"delta": text})
        yield from _finish(req, conv, {"citations": [], "uncited": [], "all_verified": False, "answer": text, "truncated": False,
                                       "route": kind, "locked": who["locked"]})
        return
    if who is not None and kind == "files" and not chunks and not who["others"]:
        text = "I couldn't find a document with that in your files. " + _photo_hint()
        yield _sse("token", {"delta": text})
        # not "no_results": the UI swaps that for a generic notice, and this text says what to do next
        yield from _finish(req, conv, {"citations": [], "uncited": [], "all_verified": False, "answer": text, "truncated": False, "route": kind})
        return
    if who is not None and who["name"] and kind == "files" and not chunks and who["others"]:
        text = _not_yours_reply(who["name"], who["others"])
        yield _sse("results", {"results": [], "semantic": False})
        yield _sse("token", {"delta": text})
        yield from _finish(req, conv, {"citations": [], "uncited": [], "all_verified": False, "answer": text, "truncated": False, "route": kind})
        return

    # Facts about the file itself (page count, size, dates, folder, a page past the end) come from the file,
    # never from the model: it only ever sees a few passages, so it could only guess them.
    counted = None
    if kind == "files":
        where = pinned + _paths(chunks)
        if facts.asks_pages(req.message):
            counted = facts.pages_reply(where)
        if not counted and facts.asks_file_info(req.message):
            counted = facts.file_info_reply(req.message, where)
        if not counted:
            counted = facts.page_out_of_range(req.message, where)
    if counted:
        text, cits = counted
        yield _sse("token", {"delta": text})
        yield from _finish(req, conv, {"citations": cits, "uncited": [], "all_verified": True, "answer": text,
                                       "truncated": False, "route": kind, "files": [cits[0]["path"]]})
        return

    if kind == "files" and not chunks:
        done = {"citations": [], "uncited": [], "all_verified": False, "answer": "", "no_results": True, "truncated": False, "route": kind}
        yield from _finish(req, conv, done)
        return

    if kind == "files":
        try:
            reply = _direct_reply(ask, chunks)
        except Exception:  # a parsing problem must fall back to the normal answer, never block it
            log.exception("direct answer failed")
            reply = None
        if reply:
            yield _sse("token", {"delta": reply})
            result = verify(reply, chunks, THRESHOLD)
            _attach_snippets(result, chunks)
            yield from _finish(req, conv, {**result, "answer": reply, "truncated": False, "route": kind, "files": _paths(chunks)})
            return

    # File answers come from the files only. Remembered facts mixed in turned "class 10 roll no" into
    # "The user is in Class 10" (from an old "user is in class 12" memory), so they are for chat only.
    remembered = _recall(req.message) if kind != "files" else []
    used = [{"id": m["id"], "text": m["text"]} for m in remembered]
    if used:
        yield _sse("memory_used", {"memories": used})

    past = _recall_chats(req.message, conv) if kind == "chat" or settings.recall_in_files else []
    seen = [{"conv_id": p["conv_id"], "title": p["title"], "snippet": p["text"][:200], "at": p["created_at"]} for p in past]
    if seen:
        yield _sse("recall_used", {"chats": seen})

    mem = ram.status()
    if mem["low"]:
        yield _sse("warning", {"detail": f"Only {mem['free_mb']} MB of RAM free; the answer may be slow. Closing other apps helps."})

    answer = ""
    truncated = False
    pz = personalize.get_all()
    ws_style = ws["answer_style"] if ws else ""
    chosen = req.style if req.style in personalize.STYLES else ws_style or _folder_style(req.root, chunks) or pz["answer_style"]  # request > workspace > folder > saved default
    style = prompts.style_instruction(chosen, req.language if req.language in personalize.LANGUAGES else pz["language"], pz["cite_pages"])
    # Order: screen context, profile, memory, then the message. The search used the bare message.
    asked = ask
    if who is not None and who["name"] and kind == "files":
        asked += f"\n(The user's name is {who['name']}. Answer only from a document in that name.)"
    prompt = prompts.with_memory(prompts.with_recall(prompts.with_profile(prompts.with_context(asked, req.context), pz["profile"]), past), remembered)
    deltas = answer_stream(prompt, chunks, history, model=req.model, style=style) if kind == "files" else chat_stream(prompt, history, model=req.model, style=style)
    try:
        for what, value in _generate(deltas):
            if what == "token":
                answer += value
                if not strict:
                    yield _sse("token", {"delta": value})
            else:
                truncated = value
    except Exception as exc:
        log.exception("answer generation failed")
        yield _sse("error", {"detail": _model_error(exc)})
        return

    if kind == "files":
        result = verify(answer, chunks, STRICT_THRESHOLD if pz["verifier_strict"] == "strict" else THRESHOLD)
        _attach_snippets(result, chunks)
        missing = [] if says_not_found(answer) else unfound_numbers(answer, chunks, ask)
        if missing:  # say it plainly next to the answer; the citation alone is easy to miss
            note = (f"\n\n\u26a0 I couldn't find {', '.join(missing[:3])} in the file{'s' if len({c['path'] for c in chunks}) > 1 else ''} "
                    f"I read, so don't rely on {'it' if len(missing) == 1 else 'them'}. Open the file to check.")
            answer += note
            if not strict:
                yield _sse("token", {"delta": note})
        if says_not_found(answer) and not result.get("all_verified"):
            result = {**result, "citations": [], "uncited": []}  # "I couldn't find it" needs no red "Not verified" source
            if who is not None:
                hint = "\n\n" + _photo_hint()
                answer += hint
                if not strict:
                    yield _sse("token", {"delta": hint})
        elif who is not None and not who["name"]:
            people = next((c.get("people") for c in chunks if c.get("people")), None)
            if people:  # the user never said their name: say whose document this is, so a wrong one is noticed
                note = (f"\n\n(This document is in the name of {people[0]}. If that isn't you, tell me your name, "
                        f"e.g. \u201cmy name is …\u201d, and I'll look for yours.)")
                answer += note
                if not strict:
                    yield _sse("token", {"delta": note})
        if strict:
            answer, result = files_only(answer, result)
            yield _sse("token", {"delta": answer})
    else:  # general answers are not checked against files
        result = {"citations": [], "uncited": [], "all_verified": False}
    yield from _finish(req, conv, {**result, "answer": answer, "truncated": truncated, "route": kind, "memory_used": used, "recall_used": seen,
                                   **({"files": _paths(chunks)} if kind == "files" else {})})


def _attach_snippets(result: dict, chunks: list[dict]) -> None:
    """Each cited source gets the part of its text that backs the claim (centred on the claim's number
    or name), falling back to the search snippet when the claim's facts aren't in it."""
    by_loc: dict[tuple, dict] = {}
    for c in chunks:
        entry = by_loc.setdefault((c["path"], c["loc_kind"], int(c["loc_no"])), {"text": "", "snippet": c.get("snippet", "")})
        entry["text"] += "\n" + c.get("text", "")
    for cit in result["citations"]:
        if cit.get("path"):
            src = by_loc.get((cit["path"], cit["loc_kind"], int(cit["loc_no"])), {"text": "", "snippet": ""})
            cit["snippet"] = excerpt(src["text"], cit.get("claim", "")) or src["snippet"]


def _finish(req: ChatRequest, conv: dict | None, done: dict):
    """Saves the exchange (only complete ones, so history never holds a dangling question)."""
    if req.attach:
        done = {**done, "attached": req.attach}  # files dropped here stay readable for follow-ups ("recheck this pdf")
    meta = {k: done[k] for k in ("route", "citations", "uncited", "truncated", "no_results", "memory_used", "recall_used", "files", "locked", "attached") if k in done}
    title = conv["title"] if conv else conversation.title_from(req.message)
    conv_id = conv["id"] if conv else conversation.create(title, workspace_id=req.workspace_id)["id"]
    _, answer_id = conversation.add_messages(conv_id, [("user", req.message, {}), ("assistant", done["answer"], meta)])
    yield _sse("conversation", {"id": conv_id, "title": title, "message_id": answer_id})
    yield _sse("done", done)
    chat_recall.schedule_embed()  # after the reply: embedding must never delay it


@router.post("")
def chat(req: ChatRequest):
    conv = None
    if req.conversation_id is not None:
        conv = conversation.get(req.conversation_id)
        if conv is None:
            raise HTTPException(404, "No such conversation")
        req.workspace_id = conv["workspace_id"]  # a chat stays in the workspace it was started in
    elif workspaces.get(req.workspace_id) is None:
        req.workspace_id = None  # deleted meanwhile (or never existed): the chat goes to "All files"
    return StreamingResponse(_stream(req, conv), media_type="text/event-stream")
