"""Whole-PC access, in two tiers.

Tier 1 (this module): a catalog of file NAMES and metadata for the drives the user switched on,
kept in its own small database (catalog.db). Fast to build, cheap to keep, and it is what lets the
assistant find "the electricity bill I downloaded last month" without that file ever being indexed.
Tier 3 (file_agent.py): the assistant reads a few of the catalog's files live, to answer one question.
File contents are read in the background by pc_content.py (hidden roots in the main index).

Off until the user turns it on (Settings > PC access): nothing is crawled before that.

Safety rules, applied to the crawl AND to every read:
  * system and dependency folders are never entered (SKIP_DIRS), links and junctions are never followed;
  * credential-like files and folders (SENSITIVE_*) are neither catalogued nor readable;
  * a read must be inside a drive the user enabled.
Change detection is a periodic incremental rescan (a stat per file, no content read). Reading the NTFS
change journal instead would be faster but needs administrator rights; see docs/PC-ACCESS.md."""
import fnmatch
import logging
import os
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.config import settings
from app.core import fuzzy, prefs
from app.core.words import strip_non_word, words

log = logging.getLogger(__name__)

PREF = "pc_access"
FTS_VERSION = 4  # bump when _fts_text or the tokenizer changes (3: Devanagari names also carry their Latin spelling; 4: Hindi words stay whole)
RESCAN_AFTER_S = 12 * 3600  # at startup, rescan if the last full scan is older than this

# Never entered. Compared lower-cased against each folder name.
SKIP_DIRS = {
    "windows", "windows.old", "program files", "program files (x86)", "programdata", "appdata",
    "$recycle.bin", "$windows.~bt", "$windows.~ws", "system volume information", "recovery", "msocache", "perflogs",
    "node_modules", ".git", ".svn", "__pycache__", ".venv", "venv", "site-packages", ".cache", ".gradle", ".m2", ".npm",
}
# Credential stores: not catalogued, not readable.
SENSITIVE_DIRS = {".ssh", ".aws", ".gnupg", ".azure", ".kube", ".docker", ".password-store", "keepass", "bitwarden"}
SENSITIVE_NAMES = (
    ".env", ".env.*", "id_rsa*", "id_ed25519*", "id_ecdsa*", "*.pem", "*.key", "*.pfx", "*.p12", "*.ppk", "*.keystore",
    "*.kdbx", "*.kdb", "wallet.dat", "*.wallet", "login data", "cookies", "web data", "key[34].db", "logins.json",
    "password*", "*passwords*", "credentials*", "secrets*", ".netrc", ".pgpass", "*.rdp",
    ".git-credentials", ".npmrc", ".pypirc", ".dockercfg", "*.ovpn", "*.jks", "id_dsa*", "*.gpg", "*.asc", "known_hosts", "ntuser.dat*", "sam", "*.pst",
)
# What gets a catalog row: everyday personal files. Programs, libraries and caches are left out on purpose.
PERSONAL_EXTS = {
    ".pdf", ".doc", ".docx", ".odt", ".rtf", ".txt", ".md", ".xls", ".xlsx", ".ods", ".csv", ".ppt", ".pptx", ".odp",
    ".html", ".htm", ".eml", ".msg", ".epub", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".heic", ".tif", ".tiff",
    ".mp3", ".wav", ".m4a", ".mp4", ".mkv", ".mov", ".avi", ".zip", ".7z", ".rar", ".ipynb", ".py", ".java", ".c", ".cpp", ".js", ".ts",
}
_REPARSE = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT: links and junctions, but ALSO OneDrive/cloud folders and files
# Windows reparse tags with this bit set are "name surrogates": they point somewhere else (symlinks,
# junctions, mount points). Cloud-sync placeholders (OneDrive's IO_REPARSE_TAG_CLOUD_*) don't have it:
# they are the real folders and files, so they must be walked and listed.
_NAME_SURROGATE = 0x20000000
# Never entered either, matched as patterns: language runtimes and vendor driver/tool folders that hold
# thousands of .py/.txt/.html files nobody asks about. Folders whose name starts with "." (.vscode,
# .codex, .cursor, ...) are app settings and caches, also skipped.
SKIP_DIR_GLOBS = ("python[0-9]*", "anaconda*", "miniconda*", "msys64", "cygwin*", "mingw*", "esupport", "drivers", "intel", "amd", "nvidia")
# Bump when the walk rules change: the next start then rescans at once instead of waiting RESCAN_AFTER_S.
SCAN_RULES = 3  # 3: folders are listed too, so Find can return a folder
# A folder row in the catalog has this as its ext. Content readers filter by real extensions, so they never see one.
FOLDER = "/"

_STOP = set(
    "the a an of my me i for to from in on at by is are was were what which where when who how that this these those with "
    "please find show get open give tell file files document documents doc docs about and or any all some last latest new "
    "recent old one it its can could would you your do does did have has had there their into out up "
    "ke ka ki ko ne se mein par hai hain aur".split()  # Hindi/Hinglish glue: "gunaho ke devata"
)

_lock = threading.Lock()


# ---------- settings ----------

def config() -> dict:
    cfg = prefs.get(PREF, {})
    cfg = cfg if isinstance(cfg, dict) else {}
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "drives": [d for d in cfg.get("drives", []) if isinstance(d, str)],
        "extra_excludes": [x for x in cfg.get("extra_excludes", []) if isinstance(x, str)],
        # background reading of file contents (tier 2); never in names-only mode, whatever was saved
        "read_content": bool(cfg.get("read_content", True)) and settings.store_file_text,
        "read_images": bool(cfg.get("read_images", False)),   # OCR photos/screenshots too: slow, off by default
    }


def enabled() -> bool:
    return config()["enabled"]


def set_config(enabled: bool | None = None, drives: list[str] | None = None, extra_excludes: list[str] | None = None,
               read_content: bool | None = None, read_images: bool | None = None) -> dict:
    """Saves the switch, the drives (or folders) to cover, and extra folder names to skip. Drives that
    don't exist are dropped. Turning it on with no drives picked uses every fixed drive."""
    cfg = config()
    if drives is not None:
        cfg["drives"] = [str(Path(d)) for d in drives if Path(d).is_dir()]
    if extra_excludes is not None:
        cfg["extra_excludes"] = [x.strip() for x in extra_excludes if x.strip()][:100]
    if read_content is not None:
        cfg["read_content"] = bool(read_content)
    if read_images is not None:
        cfg["read_images"] = bool(read_images)
    if enabled is not None:
        cfg["enabled"] = bool(enabled)
    if cfg["enabled"] and not cfg["drives"]:
        cfg["drives"] = fixed_drives()
    prefs.set(PREF, cfg)
    return cfg


# Linux: top-level system folders. Never walked or read, even if the user picks "/" as a drive.
LINUX_SYSTEM_PATHS = {
    "/proc", "/sys", "/dev", "/run", "/usr", "/etc", "/var", "/boot", "/bin", "/sbin", "/lib", "/lib32",
    "/lib64", "/libx32", "/opt", "/snap", "/srv", "/tmp", "/root", "/lost+found", "/efi",
}
# Linux mount points that are data partitions (a second disk, or the Windows partition on a dual boot).
_LINUX_DATA_MOUNTS = ("/media/", "/mnt/", "/run/media/", "/data")
_LINUX_PSEUDO_FS = {"squashfs", "tmpfs", "devtmpfs", "overlay", "vfat"}  # vfat: the EFI boot partition


def _linux_system_path(path: str) -> bool:
    p = path.rstrip("/") or "/"
    return any(p == s or p.startswith(s + "/") for s in LINUX_SYSTEM_PATHS)


def fixed_drives() -> list[str]:
    """Windows: local fixed disks (C:\\, D:\\ ...). Linux: the home folder plus mounted data partitions
    (e.g. the Windows partition on a dual-boot PC) - never "/" itself, which is mostly the system.
    Removable, network and optical drives are never included by default."""
    try:
        import psutil

        parts = psutil.disk_partitions(all=False)
    except Exception:
        parts = []
    if sys.platform == "win32":
        out = []
        for p in parts:
            opts = (p.opts or "").lower()
            if "fixed" not in opts or "cdrom" in opts or "removable" in opts:
                continue
            out.append(p.mountpoint)
        return out
    out = [str(Path.home())]
    for p in parts:
        mp, fstype = p.mountpoint, (p.fstype or "").lower()
        if fstype in _LINUX_PSEUDO_FS or _linux_system_path(mp) and not mp.startswith("/run/media/"):
            continue
        if mp.startswith(_LINUX_DATA_MOUNTS) and mp not in out:
            out.append(mp)
    return out


# ---------- rules ----------

def _matches_sensitive_name(name: str) -> bool:
    n = name.lower()
    return any(fnmatch.fnmatch(n, pat) for pat in SENSITIVE_NAMES)


def is_sensitive(path: Path | str) -> bool:
    p = Path(path)
    if _matches_sensitive_name(p.name):
        return True
    return any(part.lower() in SENSITIVE_DIRS for part in p.parts)


def _skipped_part(path: Path, extra: list[str], drives: list[str]) -> bool:
    if sys.platform != "win32" and _linux_system_path(str(path)) and not str(path).startswith("/run/media/"):
        return True
    parts = path.parts
    for r in drives:  # only what lies below the enabled drive/folder counts: the folder itself may sit under AppData
        if _under(path, [r]):
            parts = parts[len(Path(r).parts):]
            break
    for part in parts:
        low = part.lower()
        if low in SKIP_DIRS or low.startswith("$"):
            return True
        if any(fnmatch.fnmatch(low, x.lower()) for x in extra):
            return True
    return False


def under(path: Path | str, roots: list[str]) -> bool:
    """path is one of roots or inside one (case-insensitive on Windows). Keeps a workspace's chat in its folders."""
    return _under(Path(path), roots)


def _under(path: Path, roots: list[str]) -> bool:
    p = os.path.normcase(str(path))
    for r in roots:
        base = os.path.normcase(str(Path(r)))
        if p == base or p.startswith(base.rstrip("\\/") + os.sep):
            return True
    return False


def can_read(path: Path | str) -> tuple[bool, str]:
    """May the assistant read this file for an answer? (allowed, reason). Checked at read time, never
    trusting a path the model produced: it must be a real file inside an enabled drive, not behind a
    link, and not credential-like or inside a skipped system folder."""
    cfg = config()
    if not cfg["enabled"]:
        return False, "PC access is off."
    try:
        p = Path(path)
        if p.is_symlink() or not p.is_file():
            return False, "Not a regular file."
        real = p.resolve()
    except OSError as exc:
        return False, f"Can't open it: {exc}"
    if not _under(real, cfg["drives"]):
        return False, "Outside the drives PC access covers."
    if is_sensitive(real):
        return False, "That looks like a credentials file, so it is never read."
    if _skipped_part(real, cfg["extra_excludes"], cfg["drives"]):
        return False, "That is in a system or excluded folder."
    return True, ""


def can_show(path: Path | str) -> bool:
    """May Find open this folder in File Explorer? The same rules as can_read, for a folder."""
    cfg = config()
    if not cfg["enabled"]:
        return False
    try:
        p = Path(path)
        if p.is_symlink() or not p.is_dir():
            return False
        real = p.resolve()
    except OSError:
        return False
    return _under(real, cfg["drives"]) and not is_sensitive(real) and not _skipped_part(real, cfg["extra_excludes"], cfg["drives"])


# ---------- catalog ----------

def _db_path() -> Path:
    return settings.data_dir / "catalog.db"


def connect() -> sqlite3.Connection:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS catalog(
            id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, name TEXT NOT NULL, folder TEXT NOT NULL,
            ext TEXT NOT NULL, size INTEGER, mtime REAL, scan INTEGER);
        CREATE INDEX IF NOT EXISTS catalog_scan ON catalog(scan);
        CREATE VIRTUAL TABLE IF NOT EXISTS catalog_fts USING fts5(name, folder, tokenize="unicode61 remove_diacritics 2 categories 'L* N* Co Mn Mc'");
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
        CREATE VIRTUAL TABLE IF NOT EXISTS catalog_vocab USING fts5vocab(catalog_fts, 'row');
        """
    )
    if _meta(conn, "fts_version") != str(FTS_VERSION):
        # The name-text rules changed: start the list over (the next scan refills it; nothing on disk is touched).
        conn.execute("DELETE FROM catalog")
        # Recreated, not just emptied: a tokenizer change only applies to a newly created FTS table.
        conn.executescript(
            """
            DROP TABLE IF EXISTS catalog_vocab;
            DROP TABLE IF EXISTS catalog_fts;
            CREATE VIRTUAL TABLE catalog_fts USING fts5(name, folder, tokenize="unicode61 remove_diacritics 2 categories 'L* N* Co Mn Mc'");
            CREATE VIRTUAL TABLE catalog_vocab USING fts5vocab(catalog_fts, 'row');
            """
        )
        conn.execute("DELETE FROM meta WHERE k='last_scan'")
        _set_meta(conn, "fts_version", FTS_VERSION)
        conn.commit()
    if _meta(conn, "scan_rules") != str(SCAN_RULES):
        # The walk rules changed (e.g. OneDrive folders are now listed): rescan at the next start.
        # The catalog stays usable meanwhile; the complete rescan drops rows the new rules skip.
        conn.execute("DELETE FROM meta WHERE k='last_scan'")
        _set_meta(conn, "scan_rules", SCAN_RULES)
        conn.commit()
    return conn


def _meta(conn, key: str, default=None):
    row = conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    return row["v"] if row else default


def _set_meta(conn, key: str, value) -> None:
    conn.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, str(value)))


def _fts_text(s: str) -> str:
    """Splits CamelCase and letter/digit joins so 'ElectricityBill2026' is findable by 'bill' (the
    tokenizer already treats _ - . and spaces as separators)."""
    split = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
    split = re.sub(r"([A-Za-z])(\d)", r"\1 \2", split)
    split = re.sub(r"(\d)([A-Za-z])", r"\1 \2", split)
    # The joined form stays searchable too: "GridSentinel" must match a query for "gridsentinel".
    text = s if split == s else f"{s} {split}"
    if any("\u0900" <= c <= "\u097f" for c in s):  # "गुनाहों का देवता" is also findable as "gunahon ka devata"
        text = f"{text} {fuzzy.transliterate(s)}"
    return text


def reparse_is_link(attributes: int, tag: int) -> bool:
    """Should a Windows reparse point be treated as a link (not followed)? Only real links: symlinks,
    junctions and mount points. OneDrive's cloud folders and files are reparse points too but are real
    data (tag without the name-surrogate bit). An unknown tag (0) is treated as a link, to be safe."""
    if not attributes & _REPARSE:
        return False
    return tag == 0 or bool(tag & _NAME_SURROGATE)


def _is_link(entry: os.DirEntry) -> bool:
    try:
        if entry.is_symlink():
            return True
        if sys.platform == "win32":
            st = entry.stat(follow_symlinks=False)
            attrs = getattr(st, "st_file_attributes", 0)
            if not attrs & _REPARSE:
                return False
            tag = getattr(st, "st_reparse_tag", 0) or getattr(os.lstat(entry.path), "st_reparse_tag", 0)
            return reparse_is_link(attrs, tag)
    except OSError:
        return True
    return False


_NOT_WIN = sys.platform != "win32"


def _skipped_system(path: str) -> bool:
    return _linux_system_path(path) and not path.startswith("/run/media/")


def _skip_dir(low: str, extra: list[str]) -> bool:
    if low in SKIP_DIRS or low in SENSITIVE_DIRS or low.startswith(("$", ".")):
        return True
    return any(fnmatch.fnmatch(low, x) for x in SKIP_DIR_GLOBS) or any(fnmatch.fnmatch(low, x.lower()) for x in extra)


def _walk(root: str, extra: list[str], cancel: threading.Event):
    """Yields (path, name, ext, size, mtime) for each personal file and each folder walked into under root
    (folders with ext FOLDER and size 0). Iterative, so a very deep tree can't hit the recursion limit;
    unreadable folders are skipped."""
    stack = [root]
    n = 0
    while stack:
        if cancel.is_set():
            return
        folder = stack.pop()
        try:
            it = os.scandir(folder)
        except OSError:
            continue
        with it:
            for entry in it:
                n += 1
                if n % 1000 == 0:
                    time.sleep(0.005)  # stay out of the way of the foreground app
                name = entry.name
                low = name.lower()
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if _skip_dir(low, extra) or _is_link(entry) or (_NOT_WIN and _skipped_system(entry.path)):
                            continue
                        stack.append(entry.path)
                        yield entry.path, name, FOLDER, 0, entry.stat(follow_symlinks=False).st_mtime
                    elif entry.is_file(follow_symlinks=False):
                        ext = os.path.splitext(low)[1]
                        if ext not in PERSONAL_EXTS or low.startswith("~$") or _matches_sensitive_name(name):
                            continue
                        st = entry.stat(follow_symlinks=False)
                        yield entry.path, name, ext, st.st_size, st.st_mtime
                except OSError:
                    continue


def scan(roots: list[str], cancel: threading.Event | None = None, on_progress: Callable[[dict], None] | None = None) -> dict:
    """Incremental scan of the roots into the catalog. New and changed files are upserted, files that
    are gone are removed (only after a COMPLETE walk, so a stopped scan never deletes anything)."""
    cancel = cancel or threading.Event()
    extra = config()["extra_excludes"]
    counts = {"seen": 0, "added": 0, "changed": 0, "removed": 0, "cancelled": False}
    conn = connect()
    try:
        scan_id = int(_meta(conn, "scan_id", 0)) + 1
        _set_meta(conn, "scan_id", scan_id)
        conn.commit()
        for root in roots:
            if not Path(root).is_dir():
                continue
            pending = 0
            for path, name, ext, size, mtime in _walk(root, extra, cancel):
                tally = 0 if ext == FOLDER else 1  # the counts you see are files; folders are listed quietly
                counts["seen"] += tally
                row = conn.execute("SELECT id,size,mtime FROM catalog WHERE path=?", (path,)).fetchone()
                folder = os.path.dirname(path)
                if row is None:
                    cur = conn.execute(
                        "INSERT INTO catalog(path,name,folder,ext,size,mtime,scan) VALUES(?,?,?,?,?,?,?)",
                        (path, name, folder, ext, size, mtime, scan_id),
                    )
                    conn.execute("INSERT INTO catalog_fts(rowid,name,folder) VALUES(?,?,?)", (cur.lastrowid, _fts_text(name), _fts_text(folder)))
                    counts["added"] += tally
                elif row["size"] != size or row["mtime"] != mtime:
                    conn.execute("UPDATE catalog SET size=?,mtime=?,scan=? WHERE id=?", (size, mtime, scan_id, row["id"]))
                    counts["changed"] += tally
                else:
                    conn.execute("UPDATE catalog SET scan=? WHERE id=?", (scan_id, row["id"]))
                pending += 1
                if pending >= 2000:
                    conn.commit()
                    pending = 0
                    if on_progress:
                        on_progress(dict(counts))
            conn.commit()
            if cancel.is_set():
                counts["cancelled"] = True
                break
            # Complete walk of this root: whatever it did not see again is gone.
            prefix = os.path.normcase(str(Path(root)).rstrip("\\/")) + os.sep
            for row in conn.execute("SELECT id,path,ext FROM catalog WHERE scan<>?", (scan_id,)).fetchall():
                if os.path.normcase(row["path"]).startswith(prefix):
                    conn.execute("DELETE FROM catalog_fts WHERE rowid=?", (row["id"],))
                    conn.execute("DELETE FROM catalog WHERE id=?", (row["id"],))
                    counts["removed"] += 0 if row["ext"] == FOLDER else 1
            conn.commit()
        if not counts["cancelled"]:
            _set_meta(conn, "last_scan", time.time())
            conn.commit()
    finally:
        conn.close()
    if on_progress:
        on_progress(dict(counts))
    return counts


def keywords(text: str) -> list[str]:
    """Search words from a sentence: lower-cased, stop words dropped, a plural 's' trimmed."""
    out = []
    for w in (x for part in words(text.lower()) for x in part.split("_") if x):  # Hindi words whole; "_" splits
        if w in _STOP or len(w) < 2:
            continue
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        if w not in out:
            out.append(w)
    return out[:8]


# Words people use that rarely appear in file names. Each maps to extra words that may (any one is enough).
MONTHS = {m: [m[:3]] + (["sept"] if m == "september" else []) for m in
          "january february march april may june july august september october november december".split()}
SYNONYMS = {
    "resume": ["cv", "biodata"], "cv": ["resume", "biodata"], "biodata": ["resume", "cv"],
    "curriculum": ["cv", "resume"], "vitae": ["cv", "resume"],
    "certificate": ["cert", "certification"], "certificates": ["certificate", "cert"], "cert": ["certificate"],
    "marksheet": ["mark", "result", "grade"], "percentage": ["marksheet", "marks", "result", "percent"], "percent": ["marksheet", "marks", "result"],
    "cgpa": ["marksheet", "result", "grade"], "sgpa": ["marksheet", "result", "grade"], "gpa": ["marksheet", "result", "grade"], "score": ["marksheet", "result", "marks"],
    "class": ["std", "standard", "grade"], "matric": ["10th", "marksheet"], "ssc": ["10th", "marksheet"], "hsc": ["12th", "marksheet"], "result": ["marksheet", "results"], "results": ["marksheet", "result"], "marks": ["marksheet"],
    "invoice": ["bill"], "bill": ["invoice"], "receipt": ["payment", "challan"], "fees": ["fee"], "fee": ["fees"],
    "insurance": ["policy"], "mediclaim": ["insurance", "health"], "policy": ["insurance"],
    "statement": ["passbook"], "passbook": ["statement"], "ticket": ["tickets", "pnr"], "tickets": ["ticket"],
    "photo": ["img", "image", "picture", "pic"], "picture": ["photo", "img", "image"], "pic": ["photo", "picture", "img"],
    "wifi": ["broadband", "internet"], "internet": ["broadband", "wifi"], "broadband": ["internet", "wifi"],
    "bijli": ["electricity", "electric"], "paani": ["water"], "kiraya": ["rent"], "bima": ["insurance"],
    "slides": ["ppt", "presentation", "lecture"], "ppt": ["slides", "presentation"], "presentation": ["ppt", "slides"],
    "timetable": ["schedule"], "syllabus": ["curriculum"], "assignment": ["homework", "assign"], "report": ["project"],
    "notes": ["note"], "note": ["notes"], "sem": ["semester"], "semester": ["sem"],
}


def _vocab_has_prefix(conn: sqlite3.Connection, term: str) -> bool:
    return conn.execute("SELECT 1 FROM catalog_vocab WHERE term>=? AND term<? LIMIT 1", (term, term + "\U0010ffff")).fetchone() is not None


_VOCAB_CACHE: dict = {}
_VOCAB_TTL_S = 300


def _catalog_vocab(conn: sqlite3.Connection):
    """The words in catalog file names as a fuzzy.Vocab, built once per scan (cached ~5 min). Built lazily:
    only a typed word that matches nothing needs it."""
    key = (str(settings.data_dir), _meta(conn, "last_scan"), _meta(conn, "fts_version"))
    hit = _VOCAB_CACHE.get("v")
    if hit and hit[0] == key and time.time() - hit[1] < _VOCAB_TTL_S:
        return hit[2]
    vocab = fuzzy.Vocab(r[0] for r in conn.execute("SELECT term FROM catalog_vocab WHERE length(term) BETWEEN 3 AND 30"))
    _VOCAB_CACHE["v"] = (key, time.time(), vocab)
    return vocab


def _repair(conn: sqlite3.Connection, term: str) -> list[str]:
    """Spellings from the catalog's own words that sound like a term which matches nothing ("elecricity" ->
    "electricity", "devata" -> "devta", "jindagi" -> "zindagi"). Only tried when the term is not the start of
    any word, so it costs nothing on a hit."""
    if len(term) < 4 or term.isdigit() or _vocab_has_prefix(conn, term):
        return []
    try:
        return _catalog_vocab(conn).close(term, limit=4)
    except Exception:  # fuzzy help is a bonus: never let it break an exact search
        return []


def _group(conn: sqlite3.Connection, term: str) -> list[str]:
    """All the spellings of one search word, any of which counts: itself, its abbreviations (october ->
    oct), synonyms (resume -> cv) and, if nothing in the catalog starts with it, close spellings."""
    alts = [term, *MONTHS.get(term, []), *SYNONYMS.get(term, [])]
    if not _vocab_has_prefix(conn, term):
        alts += _repair(conn, term)
    return list(dict.fromkeys(alts))


def find(terms: list[str], exts: list[str] | None = None, modified_after: float | None = None, limit: int = 15, recent: bool = False,
         folders: bool = False) -> list[dict]:
    """File-name search over the catalog: every term must match (prefix match on name or folder
    words); if that finds nothing, any term may match. Credentials and vanished files are filtered out.
    Folders are returned only with folders=True (Find); Chat and forms read files, so they never get one."""
    terms = [strip_non_word(t) for t in terms]  # keeps Devanagari vowel signs (the catalog keeps them too)
    terms = [t for t in terms if t]
    if not terms:
        return []
    conn = connect()
    try:
        rows: list[sqlite3.Row] = []
        groups = [_group(conn, t) for t in terms]
        for joiner in (" AND ", " OR "):
            match = joiner.join("(" + " OR ".join(f'"{a}"*' for a in g) + ")" for g in groups)
            sql = ("SELECT c.path,c.name,c.folder,c.ext,c.size,c.mtime FROM catalog_fts f JOIN catalog c ON c.id=f.rowid "
                   "WHERE catalog_fts MATCH ?")
            args: list = [match]
            if exts:
                sql += f" AND c.ext IN ({','.join('?' * len(exts))})"
                args += [e if e.startswith(".") else "." + e for e in exts]
            if not folders:
                sql += " AND c.ext<>?"
                args.append(FOLDER)
            if modified_after is not None:
                sql += " AND c.mtime>=?"
                args.append(modified_after)
            sql += " ORDER BY bm25(catalog_fts, 5.0, 1.0), c.mtime DESC LIMIT ?"
            args.append(limit * 3)
            rows = conn.execute(sql, args).fetchall()
            if rows or len(terms) == 1:
                break
        if recent:  # "latest", "newest": the best matches, newest first
            rows = sorted(rows[: limit * 2], key=lambda r: -(r["mtime"] or 0))
    finally:
        conn.close()
    out = []
    for r in rows:
        exists = os.path.isdir if r["ext"] == FOLDER else os.path.isfile
        if is_sensitive(r["path"]) or not exists(r["path"]):
            continue
        out.append({"path": r["path"], "name": r["name"], "folder": r["folder"], "ext": r["ext"], "size": r["size"],
                    "modified": datetime.fromtimestamp(r["mtime"], timezone.utc).strftime("%Y-%m-%d")})
        if len(out) >= limit:
            break
    return out


def _content_state() -> dict:
    try:
        from app.core import pc_content

        return pc_content.worker.state()
    except Exception as exc:  # status must never fail because of the content worker
        return {"error": str(exc)}


def status() -> dict:
    cfg = config()
    info = {"enabled": cfg["enabled"], "drives": cfg["drives"], "extra_excludes": cfg["extra_excludes"],
            "available_drives": fixed_drives(), "files": 0, "last_scan": None, **crawler.state(),
            "read_content": cfg["read_content"], "read_images": cfg["read_images"], "content": _content_state()}
    try:
        conn = connect()
        try:
            info["files"] = conn.execute("SELECT COUNT(*) FROM catalog WHERE ext<>?", (FOLDER,)).fetchone()[0]
            info["folders"] = conn.execute("SELECT COUNT(*) FROM catalog WHERE ext=?", (FOLDER,)).fetchone()[0]
            ts = _meta(conn, "last_scan")
            info["last_scan"] = datetime.fromtimestamp(float(ts), timezone.utc).isoformat(timespec="seconds") if ts else None
        finally:
            conn.close()
    except sqlite3.Error as exc:
        info["error"] = str(exc)
    return info


# ---------- background crawler ----------

class Crawler:
    """Runs one scan at a time on a background thread; the scan can be stopped from the UI."""

    def __init__(self):
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._progress: dict = {}
        self._error = ""

    def state(self) -> dict:
        return {"running": bool(self._thread and self._thread.is_alive()), "progress": dict(self._progress), "scan_error": self._error}

    def start(self) -> bool:
        """Starts a scan of the enabled drives. False if PC access is off or a scan is already running."""
        cfg = config()
        with _lock:
            if not cfg["enabled"] or not cfg["drives"] or (self._thread and self._thread.is_alive()):
                return False
            self._cancel = threading.Event()
            self._error = ""
            self._progress = {}
            self._thread = threading.Thread(target=self._run, args=(list(cfg["drives"]),), name="pc-catalog", daemon=True)
            self._thread.start()
            return True

    def _run(self, roots: list[str]) -> None:
        try:
            scan(roots, self._cancel, lambda p: self._progress.update(p))
        except Exception as exc:  # a bad disk must not take the app down
            log.exception("PC catalog scan failed")
            self._error = str(exc)

    def stop(self) -> None:
        self._cancel.set()

    def start_if_stale(self) -> bool:
        """At startup: scan if PC access is on and the catalog is missing or older than RESCAN_AFTER_S."""
        if not enabled():
            return False
        try:
            conn = connect()
            try:
                ts = _meta(conn, "last_scan")
            finally:
                conn.close()
        except sqlite3.Error:
            ts = None
        if ts and time.time() - float(ts) < RESCAN_AFTER_S:
            return False
        return self.start()


crawler = Crawler()
