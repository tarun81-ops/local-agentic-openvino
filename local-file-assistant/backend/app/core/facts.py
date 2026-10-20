"""Exact answers for questions where one wrong character is a wrong answer (IFSC codes) and for "which file?" follow-ups.

The model is not trusted for these. IFSC codes are found with a fixed pattern in the text of the files that were read,
and the 4-letter bank prefix has to match the bank the person named (BARB = Bank of Baroda, SBIN = State Bank of India),
so a code from another bank's file is never presented as the answer. A follow-up that asks which file an answer came from
is answered from the citations stored with the previous answer; nothing is searched again. Page counts are counted by
opening the file (the model only ever sees a few passages, so it can only guess them), and a follow-up that points back
("this pdf", "recheck", "wrong") stays on the file the previous answer read instead of searching again."""
from __future__ import annotations

import re
from pathlib import Path

# IFSC: 4 letters, a 0, then 6 letters or digits. OCR often reads that 0 as O, so both are accepted and repaired.
_IFSC = re.compile(r"(?<![A-Z0-9])([A-Z]{4})[0O]([A-Z0-9]{6})(?![A-Z0-9])")

BANKS = {  # prefix -> (display name, words people use for it)
    "BARB": ("Bank of Baroda", ["bank of baroda", "baroda", "bob"]),
    "SBIN": ("State Bank of India", ["state bank of india", "sbi", "state bank"]),
    "HDFC": ("HDFC Bank", ["hdfc"]),
    "ICIC": ("ICICI Bank", ["icici"]),
    "UTIB": ("Axis Bank", ["axis"]),
    "PUNB": ("Punjab National Bank", ["punjab national", "pnb"]),
    "CNRB": ("Canara Bank", ["canara"]),
    "UBIN": ("Union Bank of India", ["union bank"]),
    "BKID": ("Bank of India", ["bank of india", "boi"]),
    "IDIB": ("Indian Bank", ["indian bank"]),
    "KKBK": ("Kotak Mahindra Bank", ["kotak"]),
    "YESB": ("Yes Bank", ["yes bank"]),
    "CBIN": ("Central Bank of India", ["central bank"]),
    "IOBA": ("Indian Overseas Bank", ["indian overseas", "iob"]),
    "MAHB": ("Bank of Maharashtra", ["bank of maharashtra"]),
    "IDFB": ("IDFC First Bank", ["idfc"]),
    "PYTM": ("Paytm Payments Bank", ["paytm"]),
}

ASKS_IFSC = re.compile(r"\bifs[cs]\b|\bifsc\s*code\b", re.I)
ASKS_SOURCE = re.compile(  # must point back at the previous answer ("this info", "you extracted"), not be a new request
    r"\bfrom which (?:file|document|source|pdf)\b"
    r"|\bwhere did you (?:get|find|take|read|see|extract)\b"
    r"|\b(?:which|what) (?:file|document|source|pdf)\b.{0,40}\b(?:this|that|it|answer|info|you)\b"
    r"|\b(?:mention|name|tell me|show me?|give me|open) the (?:file|source|document|pdf)\b.{0,50}\b(?:this|that|you|answer|info|extract\w*)\b"
    r"|\bsource of (?:this|that|the answer|the info)\b",
    re.I,
)


def asks_ifsc(message: str) -> bool:
    return bool(ASKS_IFSC.search(message))


def asks_source(message: str) -> bool:
    return bool(ASKS_SOURCE.search(message))


def bank_named(message: str) -> str | None:
    """Prefix of the bank the message names, longest alias first so 'bank of baroda' beats 'bank of india'."""
    low = " " + re.sub(r"[^a-z ]", " ", message.lower()) + " "
    best, best_len = None, 0
    for prefix, (_, aliases) in BANKS.items():
        for a in aliases:
            if f" {a} " in low and len(a) > best_len:
                best, best_len = prefix, len(a)
    return best


def find_ifsc(text: str) -> list[str]:
    out: list[str] = []
    for m in _IFSC.finditer(text.upper()):
        code = f"{m.group(1)}0{m.group(2)}"
        if code not in out:
            out.append(code)
    return out


def _bank_of(code: str) -> str:
    return BANKS.get(code[:4], (f"a bank with prefix {code[:4]}", []))[0]


def ifsc_reply(message: str, chunks: list[dict]) -> str | None:
    """A finished answer, or None when no IFSC code appears in what was read (the normal path then runs)."""
    wanted = bank_named(message)
    seen: dict[str, tuple[str, str, int]] = {}  # code -> (file name, loc kind, loc no) of the first place it appears
    for c in sorted(chunks, key=lambda c: (c["path"], int(c["loc_no"]), c.get("chunk_no", 0))):
        for code in find_ifsc(c["text"]):
            seen.setdefault(code, (Path(c["path"]).name, c["loc_kind"], int(c["loc_no"])))
    if not seen:
        return None
    matching = {k: v for k, v in seen.items() if not wanted or k.startswith(wanted)}
    if matching:
        who = f" of your {BANKS[wanted][0]} account" if wanted else ""
        lines = []
        for code, (name, kind, no) in list(matching.items())[:3]:
            tag = "p." if kind in ("page", "p.") else kind
            lines.append(f"{code} [{name}, {tag} {no}]")
        if len(lines) == 1:
            return f"The IFSC code{who} is {lines[0]}."
        return f"I found {len(lines)} IFSC codes{who}: " + "; ".join(lines) + ". Check which account you mean."
    others = ", ".join(f"{code} ({_bank_of(code)}) in {name}" for code, (name, _, _) in list(seen.items())[:3])
    return (f"I could not find a {BANKS[wanted][0]} IFSC code in the files I read. "
            f"The only IFSC code(s) I found: {others}. Those belong to a different bank, so I won't give them as your answer.")


def source_reply(history_messages: list[dict]) -> tuple[str, list[dict]] | None:
    """(answer text, citations) naming the files the previous answer was taken from, or None if it had none.
    The citations are the ones stored with that answer, so the same source cards (with OPEN) come back."""
    for m in reversed(history_messages):
        if m.get("role") != "assistant":
            continue
        cits = [c for c in (m.get("meta") or {}).get("citations", []) if c.get("path")]
        if not cits:
            return None
        uniq: list[dict] = []
        for c in cits:
            if not any(u["path"] == c["path"] and u["loc_no"] == c["loc_no"] for u in uniq):
                uniq.append(c)
        out: list[dict] = []
        shown = uniq[:3]
        text = "That answer was taken from " if len(shown) == 1 else "That answer was taken from these files: "
        for i, c in enumerate(shown, start=1):
            name = Path(c["path"]).name
            kind = "p." if c["loc_kind"] in ("page", "p.") else c["loc_kind"]
            marker = f"[{name}, {kind} {c['loc_no']}]"
            start = len(text)
            text += marker + (", " if i < len(shown) else "")
            out.append({**c, "n": i, "file": name, "span": [start, start + len(marker)]})
        text += f". It is on your computer at {shown[0]['path']}." if len(shown) == 1 else "."
        return text, out
    return None


# ---------- page counts ----------
ASKS_PAGES = re.compile(
    r"\bhow many (?:pages|slides)\b|\bnumber of (?:pages|slides)\b|\b(?:page|slide) count\b|\bno\.? of (?:pages|slides)\b"
    r"|\btotal (?:pages|slides)\b|\bkitne\s+(?:page|pages|panne|pej|slide|slides)\b|\b(?:page|pages|panne|pej|slides?)\s+(?:kitne|kitna)\b",
    re.I,
)


def asks_pages(message: str) -> bool:
    return bool(ASKS_PAGES.search(message))


def page_count(path: str) -> int | None:
    """Pages of a PDF or slides of a PowerPoint, read from the file. None for other types or unreadable files."""
    suffix = Path(path).suffix.lower()
    try:
        if suffix == ".pdf":
            import pymupdf

            with pymupdf.open(path) as doc:
                return doc.page_count
        if suffix == ".pptx":
            from pptx import Presentation

            return len(Presentation(path).slides)
    except Exception:
        return None
    return None


def pages_reply(paths: list[str]) -> tuple[str, list[dict]] | None:
    """(answer text, citations) with the exact page count of the first file that has one, else None."""
    for path in dict.fromkeys(paths):
        n = page_count(path)
        if n is None:
            continue
        name = Path(path).name
        unit = "slides" if path.lower().endswith(".pptx") else ("page" if n == 1 else "pages")
        claim = f"{name} has {n} {unit}"
        text = f"{claim} (counted from the file itself) "
        marker = f"[{name}, p. 1]"
        cit = {"n": 1, "file": name, "path": path, "loc_kind": "page", "loc_no": 1, "span": [len(text), len(text) + len(marker)],
               "claim": claim, "status": "checked", "verified": True}
        return text + marker + ".", [cit]
    return None


# ---------- follow-ups about the same file ----------
REFERS_BACK = re.compile(
    r"\b(?:this|that|the same|same|above|previous|last|is|us)\s+(?:pdf|file|document|doc|book|one|wala)\b"
    r"|\bre-?check\b|\bcheck (?:it |this |that )?again\b|\blook again\b|\bread (?:it |this )?again\b"
    r"|\b(?:is|that'?s|it'?s|this is|answer is)\s+(?:wrong|incorrect|not (?:right|correct))\b|\bgalat\b|\bdobara\b|\bphir se\b",
    re.I,
)


# A short follow-up whose only pointer is a pronoun ("who wrote it?", "iska author kaun hai") also means the same file.
_SHORT_BACK = re.compile(r"\b(?:it|its|iska|iski|iske|isme|ismein|isko|isse|iss|uska|uski|uske|usme|usko|ye|yeh)\b", re.I)
_NAMES_A_FILE = re.compile(r"\b[\w\-]+\.(?:pdf|docx?|pptx?|xlsx?|txt|md|csv|jpe?g|png)\b", re.I)


def refers_back(message: str) -> bool:
    if _NAMES_A_FILE.search(message):  # "how big is invoice.pdf" names its own file
        return False
    return bool(REFERS_BACK.search(message)) or (len(message.split()) <= 8 and bool(_SHORT_BACK.search(message)))


# ---------- which part of a file a question is about ----------
_PAGE_N = re.compile(
    r"\b(?:page|pg|p\.|panna|pej|slide)\s*(?:no\.?|number|num)?\s*#?\s*(\d{1,4})\b"
    r"|\b(\d{1,4})\s*(?:st|nd|rd|th|va|vi|ve)?\s+(?:page|panna|pej|slide)\b",
    re.I,
)
_LAST_PAGE = re.compile(r"\b(?:last|final|aakhri|akhri|aakhiri|akhiri)\s+(?:page|panna|pej|slide)\b", re.I)
_FIRST_PAGE = re.compile(r"\b(?:first|pehla|pehle|pahla)\s+(?:page|panna|pej|slide)\b", re.I)
_OVERVIEW = re.compile(
    r"\bsummar(?:y|ise|ize|ies)\b|\boverview\b|\bgist\b|\bsaar\b|\bsaransh\b|\b(?:main|key) points\b"
    r"|\bwhat(?:'s| is| was)? (?:it|(?:this|that|the) (?:book|file|pdf|document|doc|story|novel|chapter)|this|that) (?:all )?about\b"
    r"|\bkis (?:bare|baare|ke bare|ke baare) (?:me|mein|main)\b|\b(?:kya|kiske) (?:bare|baare) (?:me|mein)\b|\bstory kya hai\b",
    re.I,
)


def wanted_page(message: str) -> int | str | None:
    """A page the question names: its number, "first", "last", or None."""
    m = _PAGE_N.search(message)
    if m:
        n = int(m.group(1) or m.group(2))
        return n if n > 0 else None
    if _LAST_PAGE.search(message):
        return "last"
    if _FIRST_PAGE.search(message):
        return "first"
    return None


def asks_overview(message: str) -> bool:
    return bool(_OVERVIEW.search(message))


def page_out_of_range(message: str, paths: list[str]) -> tuple[str, list[dict]] | None:
    """A named page past the end of the file: say how many pages it has instead of letting the model invent page N."""
    page = wanted_page(message)
    if not isinstance(page, int):
        return None
    for path in dict.fromkeys(paths):
        n = page_count(path)
        if n is None:
            continue
        if page <= n:
            return None
        counted = pages_reply([path])
        if not counted:
            return None
        text, cits = counted
        return f"There is no page {page}. " + text, [{**c, "span": [c["span"][0] + len(f"There is no page {page}. "), c["span"][1] + len(f"There is no page {page}. ")]} for c in cits]
    return None


# ---------- size, dates and location: from the file system, never from the model ----------
_ASKS_SIZE = re.compile(
    # only the file's own size: "size of the plot in the sale deed" is a question about the text
    r"\bhow (?:big|large|heavy) is (?:it|this|that|the (?:file|pdf|document|doc|book))\b|\bfile ?size\b"
    r"|\bsize of (?:this|that|the|my) (?:file|pdf|document|doc|book)\b|\bits size\b|\bwhat(?:'s| is) its size\b"
    r"|\b(?:kitn[aie]|kitne) (?:bada|badi|bade|mb|kb|gb)\b|\b(?:iska|uska|file ka|pdf ka) size\b|\bhow many (?:mb|kb|gb|bytes)\b",
    re.I,
)
_ASKS_DATE = re.compile(
    r"\bwhen (?:was|did|were) .{0,40}?\b(?:modified|changed|updated|edited|saved|created|made|downloaded|added)\b"
    r"|\blast (?:modified|changed|updated|edited|saved)\b|\b(?:created|modified|download(?:ed)?) (?:on|date|time)\b"
    r"|\bkab (?:bana|bani|banaya|banai|save|download|modify|change|edit|aaya|aayi)\w*\b",
    re.I,
)
_ASKS_WHERE = re.compile(
    r"\bwhere is (?:it|this|that)\s+(?:saved|stored|located|kept)\b|\bwhere is (?:this|that|the) (?:file|pdf|document|book|doc)\b"
    r"|\bwhere (?:is it|are they) (?:on my|in my)\b|\b(?:full |file )?path of\b|\bwhich folder\b|\bwhat folder\b"
    r"|\b(?:kaha|kahan)\s+(?:hai|rakha|rakhi|save|pada|padi|stored)\b|\bkis folder\b",
    re.I,
)


def asks_file_info(message: str) -> bool:
    return any(rx.search(message) for rx in (_ASKS_SIZE, _ASKS_DATE, _ASKS_WHERE))


def _human_size(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n} bytes" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _when(ts: float) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(ts).strftime("%d %b %Y, %I:%M %p")


def file_info_reply(message: str, paths: list[str]) -> tuple[str, list[dict]] | None:
    """(answer text, citations) with the size / dates / folder the question asks for, read with os.stat."""
    import sys

    for path in dict.fromkeys(paths):
        p = Path(path)
        try:
            st = p.stat()
        except OSError:
            continue
        name = p.name
        parts = []
        if _ASKS_SIZE.search(message):
            parts.append(f"{name} is {_human_size(st.st_size)}")
        if _ASKS_DATE.search(message):
            born = getattr(st, "st_birthtime", None) or (st.st_ctime if sys.platform == "win32" else None)
            line = f"it was last modified on {_when(st.st_mtime)}"
            if born:
                line += f" and created on this PC on {_when(born)}"
            parts.append(line if parts else f"{name} " + line.removeprefix("it "))
        if _ASKS_WHERE.search(message):
            parts.append(("it is" if parts else f"{name} is") + f" in the folder {p.parent}")
        if not parts:
            return None
        text = "; ".join(parts) + " (read from the file itself) "
        marker = f"[{name}, p. 1]"
        cit = {"n": 1, "file": name, "path": str(p), "loc_kind": "page", "loc_no": 1, "span": [len(text), len(text) + len(marker)],
               "claim": parts[0], "status": "checked", "verified": True}
        return text + marker + ".", [cit]  # starts with the file name, kept exactly as it is spelled
    return None


def last_files(history_messages: list[dict]) -> list[str]:
    """Files the previous answer used (its citations first, then the files it read), if that answer came from files.
    Only the most recent answer counts: after a general chat reply, "this" no longer means a file."""
    for m in reversed(history_messages):
        if m.get("role") != "assistant":
            continue
        meta = m.get("meta") or {}
        if meta.get("route") != "files":
            return []
        paths = [c["path"] for c in meta.get("citations", []) if c.get("path")] + list(meta.get("files", []))
        return list(dict.fromkeys(paths))[:2]
    return []

