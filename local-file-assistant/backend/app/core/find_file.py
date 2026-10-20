""""Find that file": search by description ("the electricity bill from last month", "my internship
offer letter") and get the files back, best first, each with the reason it matched.

No model of any kind is used (no chat model, no embeddings), so it answers fast even while the model server is
busy. The description is turned into search words, a file-type filter and a date window by rules,
then three lookups are merged:
  1. file names in the whole-PC catalog (when PC access is on),
  2. file names and folder names of everything in the index,
  3. the text inside indexed files (keyword search).
A file that matches by name *and* by content ranks highest. The date window and file type are soft:
when they leave nothing, the search runs again without them, so a wrong guess never hides the file."""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from app.config import settings
from app.core import fuzzy, pc_access
from app.core.assistant.file_agent import TYPE_WORDS
from app.db import sqlite_fts

log = logging.getLogger(__name__)

NAME_POOL = 40          # catalog candidates looked at
INDEX_NAME_POOL = 300   # index-name candidates, best-matching first (ranked in SQL before the cut)
CONTENT_POOL = 15       # content-search files looked at
MAX_LIMIT = 30
SIMILAR_POOL = 250      # candidates compared when suggesting similar files

# Words that only describe when or what kind of file, never words in its name.
_DATE_ONLY = {"month", "week", "year", "yesterday", "today", "downloaded", "saved", "made", "created", "ago",
              "previous", "past", "this", "recently", "received", "sent", "got", "earlier"}
_FILLER = {"that", "file", "named", "called", "something", "thing", "copy", "version", "one", "wala", "wali", "vala", "vali"} | set(fuzzy.PARTICLES)
# "the blackmagic folder": look for a folder, not a file named "folder".
_FOLDER_WORDS = {"folder", "directory", "dir"}
_RECENT = re.compile(r"\b(?:latest|newest|most recent|recent|recently|current|new)\b", re.I)


# ---------- 1. understand the description (rules only) ----------

def _month_start(d: datetime) -> datetime:
    return d.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def date_window(text: str, now: datetime | None = None) -> tuple[float | None, str]:
    """(after, label): the start of the time a phrase like "last month" or "this week" points at."""
    now = now or datetime.now()
    t = text.lower()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if re.search(r"\b(?:last|previous|past) month\b", t):
        start = _month_start(_month_start(now) - timedelta(days=1))
        # A bill "from last month" is often downloaded early this month, so the window runs to now.
        return start.timestamp(), "since " + start.strftime("%d %b")
    if re.search(r"\bthis month\b", t):
        return _month_start(now).timestamp(), "this month"
    if re.search(r"\b(?:last|previous|past) week\b", t):
        return (today - timedelta(days=14)).timestamp(), "last 2 weeks"
    if re.search(r"\bthis week\b", t):
        return (today - timedelta(days=7)).timestamp(), "last 7 days"
    if re.search(r"\byesterday\b", t):
        return (today - timedelta(days=1)).timestamp(), "since yesterday"
    if re.search(r"\btoday\b", t):
        return today.timestamp(), "today"
    if re.search(r"\b(?:last|previous|past) year\b", t):
        start = today.replace(year=today.year - 1, month=1, day=1)
        return start.timestamp(), "since " + start.strftime("%Y")
    if re.search(r"\bthis year\b", t):
        return today.replace(month=1, day=1).timestamp(), "this year"
    m = re.search(r"\b(\d{1,2})\s+(day|week|month)s?\s+ago\b", t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        days = n * {"day": 1, "week": 7, "month": 31}[unit]
        start = today - timedelta(days=days + max(2, days // 3))  # "2 weeks ago" is never exact
        return start.timestamp(), f"about {n} {unit}{'s' if n > 1 else ''} ago"
    return None, ""


def understand(description: str, now: datetime | None = None) -> dict:
    """{"words", "exts", "after", "recent", "date_label", "type_label"}. No model."""
    words = pc_access.keywords(description)
    type_words = [w for w in words if w in TYPE_WORDS or w in _FOLDER_WORDS]
    exts = list(dict.fromkeys(e for w in type_words for e in TYPE_WORDS.get(w, [pc_access.FOLDER])))
    keep = [w for w in words if w not in TYPE_WORDS and w not in _FOLDER_WORDS and w not in _DATE_ONLY and w not in _FILLER]
    after, label = date_window(description, now)
    return {
        "words": keep or [w for w in words if w not in _DATE_ONLY] or words,
        "exts": exts,
        "after": after,
        "recent": bool(_RECENT.search(description)),
        "date_label": label,
        "type_label": "/".join(type_words).upper(),
    }


def _alts(word: str) -> list[str]:
    return list(dict.fromkeys([word, *pc_access.MONTHS.get(word, []), *pc_access.SYNONYMS.get(word, [])]))


def _norm_name(text: str) -> str:
    """'Electricity_Bill-Oct2026' -> 'electricity bill oct 2026' (CamelCase and digits split)."""
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    text = re.sub(r"([A-Za-z])(\d)", r"\1 \2", text)
    text = re.sub(r"(\d)([A-Za-z])", r"\1 \2", text)
    return " ".join(re.findall(r"[^\W_]+", text.lower()))


def _stem(word: str) -> str:
    return word[:-1] if len(word) > 4 and word.endswith("s") and not word.endswith("ss") else word


def name_score_detail(path: str, words: list[str]) -> tuple[float, list[str], list[tuple[str, str]]]:
    """(score 0..1, words that hit, [(typed word, name word)] for hits that were by sound, not spelling).
    A word counts fully when the file name has it (or starts with it); at 0.9 when the name has a word that
    sounds like it ("devata" for "Devta"); at half weight when only the folder name does."""
    if not words:
        return 0.0, [], []
    p = Path(path)
    name_words = [_stem(w) for w in _norm_name(p.name if p.is_dir() else p.stem).split()]
    joined = "".join(name_words)
    folder_words = [_stem(w) for w in _norm_name(p.parent.name).split()]
    total, hits, by_sound = 0.0, [], []
    for w in words:
        alts = _alts(w)
        if any(n.startswith(a) for a in alts for n in name_words) or any(len(a) >= 4 and a in joined for a in alts):
            total += 1.0
            hits.append(w)
            continue
        like = max(((fuzzy.word_sim(w, n), n) for n in name_words), default=(0.0, ""))
        if like[0] < fuzzy.CUTOFF and len(w) >= 5:  # names written without separators: "GunahonKaDevta"
            part = fuzzy.name_match([w], p.name if p.is_dir() else p.stem)
            like = (part[0], part[1][0][1]) if part[1] else like
        if like[0] >= fuzzy.CUTOFF:
            total += 0.9 * like[0]
            hits.append(w)
            by_sound.append((w, like[1]))
        elif any(n.startswith(a) for a in alts for n in folder_words) or any(fuzzy.word_sim(w, n) >= fuzzy.CUTOFF for n in folder_words):
            total += 0.5
            hits.append(w)
    return total / len(words), hits, by_sound


def name_score(path: str, words: list[str]) -> tuple[float, list[str]]:
    """How well a file's name (and, at half weight, its folder) matches: 0..1, plus the words that hit."""
    score, hits, _ = name_score_detail(path, words)
    return score, hits


# ---------- 2. look up ----------

def _from_catalog(plan: dict) -> list[str]:
    if not pc_access.enabled():
        return []
    try:
        # No type or date filter here: those are applied (and relaxed) after merging, so a wrong
        # "pdf" guess can still fall back to the .docx that only the catalog knows about.
        rows = pc_access.find(plan["words"], None, None, limit=NAME_POOL, recent=plan["recent"], folders=True)
    except Exception as exc:  # catalog not built yet, or busy
        log.info("find: catalog lookup skipped (%s)", exc)
        return []
    return [r["path"] for r in rows]


_INDEX_VOCAB: dict = {}


def _index_vocab():
    """The words in the names of indexed files, as a fuzzy.Vocab; rebuilt only when the index changed."""
    conn = sqlite_fts.connect(settings.db_path)
    try:
        sig = (str(settings.db_path),) + tuple(conn.execute("SELECT count(*), max(indexed_at) FROM files").fetchone())
        if _INDEX_VOCAB.get("sig") == sig:
            return _INDEX_VOCAB["vocab"]
        hidden = sqlite_fts.hidden_roots(conn)
        skip = f" WHERE root NOT IN ({','.join('?' * len(hidden))})" if hidden else ""
        paths = [r[0] for r in conn.execute(f"SELECT path FROM files{skip}", hidden)]
    finally:
        conn.close()
    tokens = (t for p in paths for t in fuzzy.split_name(Path(p).name))
    _INDEX_VOCAB.update(sig=sig, vocab=fuzzy.Vocab(tokens))
    return _INDEX_VOCAB["vocab"]


def _close(vocab, word: str) -> list[str]:
    try:
        return vocab.close(word, limit=4) if vocab is not None else []
    except Exception:  # fuzzy help is a bonus
        return []


def _from_index_names(plan: dict) -> list[str]:
    """Indexed files whose path inside its indexed folder contains a spelling of the words, ranked in
    SQL by how many of the words match (then newest), so the cut-off never drops the best match.
    Matching is on the path *inside* the indexed folder, so "C:\\Users\\<name>" matches nothing."""
    vocab = _index_vocab()
    groups = [[a for a in _alts(w) + _close(vocab, w) if len(a) >= 2] for w in plan["words"]]
    groups = [g for g in groups if g]
    if not groups:
        return []
    rel = "lower(substr(path, length(root) + 2))"
    hit = lambda g: "(" + " OR ".join(f"{rel} LIKE ?" for _ in g) + ")"  # noqa: E731
    args: list = []
    for g in groups:  # the ORDER BY score
        args += [f"%{a}%" for a in g]
    for g in groups:  # the WHERE
        args += [f"%{a}%" for a in g]
    score = " + ".join(hit(g) for g in groups)
    where = " OR ".join(hit(g) for g in groups)
    conn = sqlite_fts.connect(settings.db_path)
    try:
        # Whole-PC roots are left out *in the query*, before the LIMIT: filtering afterwards let thousands
        # of hidden matches fill every slot and Find came back empty. Their names never need this lookup:
        # while PC access is on the catalog already covers them, and while it is off they must not show.
        hidden = sqlite_fts.hidden_roots(conn)
        skip = f" AND root NOT IN ({','.join('?' * len(hidden))})" if hidden else ""
        rows = conn.execute(f"SELECT path, ({score}) AS n FROM files WHERE ({where}){skip} ORDER BY n DESC, mtime DESC LIMIT ?",
                            [*args, *hidden, INDEX_NAME_POOL]).fetchall()
    finally:
        conn.close()
    return [r["path"] for r in rows]


def _from_content(description: str, plan: dict) -> tuple[list[dict], bool]:
    """Keyword search over the text of indexed files. Keyword only on purpose: semantic search needs
    the embedding model, which can take seconds when the model server is busy, and Find must stay instant."""
    from app.core.search import fts_search
    from app.core.search.hybrid import snippet

    query = " ".join(plan["words"]) or description
    conn = sqlite_fts.connect(settings.db_path)
    try:
        skip = [] if pc_access.enabled() else sqlite_fts.hidden_roots(conn)
        rows = fts_search.search(conn, query, limit=CONTENT_POOL * 3, skip_roots=skip or None)
    except Exception as exc:
        log.warning("find: content search failed (%s)", exc)
        return [], False
    finally:
        conn.close()
    terms = fts_search.terms(query)
    best: dict[str, dict] = {}
    for r in rows:
        if r["path"] not in best:
            best[r["path"]] = {**r, "snippet": snippet(r["text"], terms)}
    return list(best.values())[:CONTENT_POOL], False


# ---------- 3. rank ----------

def _fmt_day(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%d %b %Y")


def find(description: str, limit: int = 10, now: datetime | None = None, content=None) -> dict:
    """content: the content-search function (tests pass a stand-in)."""
    t0 = time.perf_counter()
    description = (description or "").strip()[:300]
    plan = understand(description, now)
    understood = {k: plan[k] for k in ("words", "exts", "date_label", "type_label", "recent")}
    if not description or not plan["words"]:
        return {"results": [], "similar": [], "understood": understood, "relaxed": [], "pc_access": pc_access.enabled(), "semantic": False, "ms": 0}
    limit = max(1, min(limit, MAX_LIMIT))

    pool: dict[str, dict] = {}
    for path in _from_catalog(plan) + _from_index_names(plan):
        pool.setdefault(path, {"path": path, "content_rank": None, "snippet": "", "loc": None})
    content_rows, semantic = (content or _from_content)(description, plan)
    for i, r in enumerate(content_rows):
        item = pool.setdefault(r["path"], {"path": r["path"], "content_rank": None, "snippet": "", "loc": None})
        item["content_rank"] = i
        item["snippet"] = r.get("snippet") or ""
        item["loc"] = [r.get("loc_kind"), r.get("loc_no")]

    scored = []
    for item in pool.values():
        path = item["path"]
        if pc_access.is_sensitive(path):
            continue
        try:
            st = os.stat(path)
        except OSError:
            continue  # moved or deleted since it was indexed
        is_dir = os.path.isdir(path)
        if not is_dir and not os.path.isfile(path):
            continue
        ns, hits, by_sound = name_score_detail(path, plan["words"])
        if is_dir and pc_access.FOLDER not in plan["exts"]:
            ns *= 0.9  # a file named like the words wins over a folder named like them, unless you asked for a folder
        cs = 0.0 if item["content_rank"] is None else 1.0 - item["content_rank"] / (CONTENT_POOL + 1)
        if ns == 0 and cs == 0:
            continue
        score = 0.65 * ns + 0.45 * cs + (0.15 if ns >= 0.5 and cs > 0 else 0)
        why = []
        exact = [w for w in hits if w not in {t for t, _ in by_sound}]
        if exact:
            why.append("name has “" + ", ".join(exact) + "”")
        if by_sound:
            why.append("name sounds like “" + ", ".join(n for _, n in by_sound) + "”")
        if cs:
            why.append("text inside matches")
        scored.append({
            "path": path, "name": Path(path).name, "folder": str(Path(path).parent),
            "ext": pc_access.FOLDER if is_dir else Path(path).suffix.lower(), "is_folder": is_dir,
            "size": None if is_dir else st.st_size, "mtime": st.st_mtime, "modified": _fmt_day(st.st_mtime),
            "snippet": item["snippet"], "loc": item["loc"], "score": score, "why": why,
        })

    def filtered(use_type: bool, use_date: bool):
        out = scored
        if use_type and plan["exts"]:
            out = [r for r in out if r["ext"] in plan["exts"]]
        if use_date and plan["after"] is not None:
            out = [r for r in out if r["mtime"] >= plan["after"]]
        return out

    relaxed: list[str] = []
    results = filtered(True, True)
    if not results and plan["after"] is not None:
        results = filtered(True, False)
        if results:
            relaxed.append("date")
    if not results and plan["exts"]:
        results = filtered(False, False)
        if results:
            relaxed.append("type")
    if plan["after"] is not None and "date" not in relaxed:
        for r in results:
            r["why"].append("modified " + r["modified"])
    if plan["recent"] or plan["after"] is not None:
        # Among about-equally good matches the newer one wins ("latest", "last month").
        results.sort(key=lambda r: (round(r["score"], 1), r["mtime"]), reverse=True)
    else:
        results.sort(key=lambda r: (r["score"], r["mtime"]), reverse=True)
    for r in results:
        r["score"] = round(r["score"], 3)
    shown = results[:limit]
    similar: list[dict] = []
    if shown:
        try:  # suggestions are a bonus: they must never cost the answer
            similar = similar_files(shown[0]["path"], limit=5, exclude={r["path"] for r in shown})
        except Exception:
            log.exception("find: similar files skipped")
    return {
        "results": shown,
        "similar": similar,
        "understood": understood,
        "relaxed": relaxed,
        "pc_access": pc_access.enabled(),
        "semantic": semantic,
        "ms": round((time.perf_counter() - t0) * 1000),
    }


# ---------- 4. similar files and title lookups ----------

def _card(path: str, st: os.stat_result | None = None) -> dict | None:
    """The fields the UI shows for a file; None when it is gone, a credential, or not a regular file."""
    if pc_access.is_sensitive(path):
        return None
    try:
        st = st or os.stat(path)
    except OSError:
        return None
    if not os.path.isfile(path):
        return None
    return {"path": path, "name": Path(path).name, "folder": str(Path(path).parent), "ext": Path(path).suffix.lower(),
            "is_folder": False, "size": st.st_size, "mtime": st.st_mtime, "modified": _fmt_day(st.st_mtime)}


def similar_files(path: str, limit: int = 6, exclude: set[str] | None = None) -> list[dict]:
    """Files like this one, best first, each with the reason: a similar name (by sound, so "Sep2026" and
    "Jun2026" versions and "Devta"/"Devata" spellings count), the same folder, the same type. Names only:
    no model and no embedding, so it is instant. Only files Find itself could return are suggested."""
    exclude = (exclude or set()) | {path}
    target = Path(path)
    words = [t for t in fuzzy.split_name(target.stem) if t not in fuzzy.PARTICLES and not t.isdigit() and len(t) >= 2]
    pool: dict[str, None] = {}
    if words:
        plan = {"words": words[:6], "exts": [], "after": None, "recent": False}
        for p in _from_catalog(plan) + _from_index_names(plan):
            pool.setdefault(p)
    conn = sqlite_fts.connect(settings.db_path)
    try:  # neighbours in the same folder, even when their names share nothing
        hidden = sqlite_fts.hidden_roots(conn)
        skip = f" AND root NOT IN ({','.join('?' * len(hidden))})" if hidden else ""
        for r in conn.execute(f"SELECT path FROM files WHERE path LIKE ?{skip} ORDER BY mtime DESC LIMIT 60", [str(target.parent) + os.sep + "%", *hidden]):
            pool.setdefault(r[0])
    finally:
        conn.close()
    scored = []
    for cand in list(pool)[:SIMILAR_POOL * 2]:
        if cand in exclude:
            continue
        sim = fuzzy.name_similarity(target.name, Path(cand).name)
        same_folder = Path(cand).parent == target.parent
        same_ext = Path(cand).suffix.lower() == target.suffix.lower()
        if sim < 0.4 and not (same_folder and same_ext):
            continue
        card = _card(cand)
        if card is None:
            continue
        why = []
        if sim >= 0.4:
            why.append("similar name")
        if same_folder:
            why.append("same folder")
        if same_ext:
            why.append("same type")
        card.update(score=round(0.75 * sim + 0.15 * same_folder + 0.1 * same_ext, 3), why=why, snippet="", loc=None)
        scored.append(card)
    scored.sort(key=lambda r: (r["score"], r["mtime"]), reverse=True)
    return scored[:limit]


def name_matches(query: str, limit: int = 5, min_score: float = 0.75, exclude: set[str] | None = None) -> list[dict]:
    """Files whose NAME is close to what was typed, by sound as well as spelling: "gunaho ke devata" ->
    "Gunahon Ka Devta.pdf". Names only: no model, instant. Each hit has the words that matched."""
    words = fuzzy.title_words(query) or [w for w in fuzzy.split_name(query) if w not in fuzzy.PARTICLES]
    words = list(dict.fromkeys(words))[:8]
    if not words:
        return []
    plan = {"words": words, "exts": [], "after": None, "recent": False}
    paths = list(dict.fromkeys(_from_catalog(plan) + _from_index_names(plan)))
    out = []
    for p in paths:
        if exclude and p in exclude:
            continue
        card = _card(p)
        if card is None:
            continue
        score, hits = fuzzy.name_match(words, Path(p).name)
        if score < min_score:
            continue
        why = [f"name has “{n}”" if w.lower() == n else f"name sounds like “{n}”" for w, n, _ in hits]
        card.update(score=round(score, 3), why=why, snippet="", loc=None)
        out.append(card)
    out.sort(key=lambda r: (r["score"], r["mtime"]), reverse=True)
    return out[:limit]


def title_hits(message: str, limit: int = 2) -> list[dict]:
    """For chat: does this short message look like the title of a file the app can read? Strict on purpose
    (every title word must match, one-word titles need 6+ letters), because a wrong hit would answer from
    the wrong file. Returns files the app is allowed to open, best first. Names only: no model."""
    words = fuzzy.title_words(message)
    if not words or len(words) > 6 or len(message.split()) > 10:
        return []
    if len(words) == 1 and len(fuzzy.fold(words[0])) < 6:
        return []
    return [h for h in name_matches(message, limit=limit * 3, min_score=0.8) if allowed_to_open(h["path"])][:limit]


def allowed_to_open(path: str, folders: bool = False) -> bool:
    """A file Find could have shown may be opened or shown in Explorer: it is in a folder you indexed,
    or PC access is on and covers it. Contents read from the whole PC stay closed while PC access is
    off, the same rule Find itself follows. folders=True also allows a folder PC access covers (Find
    opens it in File Explorer); callers that read the file leave it False."""
    if pc_access.is_sensitive(path):
        return False
    if folders and Path(path).is_dir():
        return pc_access.can_show(path)
    conn = sqlite_fts.connect(settings.db_path)
    try:
        root = sqlite_fts.file_root(conn, path)
        hidden = set(sqlite_fts.hidden_roots(conn))
    finally:
        conn.close()
    if root is not None and root not in hidden:
        return Path(path).is_file()
    ok, _ = pc_access.can_read(path)  # False whenever PC access is off
    return ok
