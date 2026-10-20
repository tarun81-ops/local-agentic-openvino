"""Exact arithmetic for questions about marks, done in code, never by the model.

The local model only quotes. Here the subject marks are read out of the OCR text of a marksheet and added up in Python.
Each mark is cross-checked against the "marks in words" column the board prints next to it ("NINE TWO" = 92), so an OCR
slip such as 1O for 10 is caught instead of silently giving a wrong percentage.

Nothing here is executed from file text: it is matched with fixed patterns and only ever turned into integers."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

ASKS_MARKS = re.compile(
    r"\b(percentage|percent|cgpa|sgpa|gpa|total marks|marks obtained|my marks|my score|how (?:many|much) (?:marks|did i (?:score|get))|"
    r"aggregate|average marks|out of)\b",
    re.I,
)

_DIGIT_WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9}
_TEEN_WORDS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
               "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS_WORDS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_ALL_WORDS = {**_DIGIT_WORDS, **_TEEN_WORDS, **_TENS_WORDS, "hundred": 100}
_BY_LENGTH = sorted(_ALL_WORDS, key=len, reverse=True)  # longest first: "eighty" before "eight"


def _split_words(s: str) -> list[str] | None:
    """'NINETWO' or 'NINE TWO' -> ['nine', 'two']; None if anything is not a number word."""
    s = re.sub(r"[^a-z]", "", s.lower())
    out: list[str] = []
    while s:
        for w in _BY_LENGTH:
            if s.startswith(w):
                out.append(w)
                s = s[len(w):]
                break
        else:
            return None
    return out or None


def words_to_int(s: str) -> int | None:
    """Both styles boards use: digit by digit ("ZERO NINE TWO" = 92) and ordinary ("NINETY TWO" = 92)."""
    ws = _split_words(s)
    if not ws:
        return None
    if all(w in _DIGIT_WORDS for w in ws):
        return int("".join(str(_DIGIT_WORDS[w]) for w in ws))
    total = 0
    for w in ws:
        if w == "hundred":
            total = (total or 1) * 100
        else:
            total += _ALL_WORDS[w]
    return total


@dataclass
class Subject:
    code: str
    name: str
    marks: int
    checked: bool  # the marks agree with the printed words (or with theory + practical)
    note: str = ""


@dataclass
class Marksheet:
    subjects: list[Subject] = field(default_factory=list)


# 3-digit subject code, subject name, one to three numbers (theory / practical / total), then the total in words + grade.
_ROW = re.compile(
    r"(?<!\d)(\d{3})\s+([A-Z][A-Z0-9 .&,'()/\-]{2,40}?)\s+((?:\d{2,3}\s+){1,3})((?:[A-Z]{3,12}\s+){1,4})",
)
_NOT_SUBJECT = re.compile(r"\b(roll|regn|regist|school|board|marks|statement|certificate|examination|result|total)\b", re.I)


def parse_marksheet(text: str) -> Marksheet:
    flat = " ".join(text.replace(" ", " ").split())
    sheet = Marksheet()
    seen: set[str] = set()
    for m in _ROW.finditer(flat.upper() + " "):
        code, name, nums, words = m.group(1), m.group(2).strip(" .-"), m.group(3).split(), m.group(4)
        if code in seen or _NOT_SUBJECT.search(name):
            continue
        values = [int(n) for n in nums]
        total = values[-1]
        if total > 100:  # one subject is out of 100 on these sheets
            continue
        # The words may run on into the grade ("NINE TWO A2"): take the longest leading run of number words.
        tokens = words.split()
        spoken = None
        for k in range(len(tokens), 0, -1):
            spoken = words_to_int(" ".join(tokens[:k]))
            if spoken is not None:
                break
        checked, note = False, ""
        if spoken is not None and spoken == total:
            checked = True
        elif len(values) >= 2 and sum(values[:-1]) == total:
            checked, note = True, "theory + practical add up"
        else:
            note = f"printed words say {spoken}" if spoken is not None else "no words to check against"
        seen.add(code)
        sheet.subjects.append(Subject(code, name.title(), total, checked, note))
    return sheet


@dataclass
class Result:
    lines: str          # "Hindi 92, English 88, ..."
    working: str        # "92 + 88 + ... = 424 out of 500 = 84.8%"
    percent: float
    caution: str = ""   # non-empty when something could not be verified
    best_five: str = ""


def _fmt(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".")


def compute(sheet: Marksheet) -> Result | None:
    subs = sheet.subjects
    if len(subs) < 3:
        return None
    use = subs
    best_five = ""
    if len(subs) > 5:  # a sixth, additional subject: boards count the best five
        use = sorted(subs, key=lambda s: -s.marks)[:5]
        all_total = sum(s.marks for s in subs)
        best_five = (f"All {len(subs)} subjects: {all_total} out of {len(subs) * 100} = {_fmt(all_total / len(subs))}%. "
                     f"The usual percentage counts the best five.")
    total = sum(s.marks for s in use)
    out_of = len(use) * 100
    pct = total * 100 / out_of
    working = " + ".join(str(s.marks) for s in use) + f" = {total} out of {out_of} = {_fmt(pct)}%"
    caution = ""
    bad = [s for s in use if not s.checked]
    if bad:
        caution = ("Check these against the scan, the text recognition may have misread them: "
                   + ", ".join(f"{s.name} ({s.marks}; {s.note})" for s in bad) + ".")
    return Result(", ".join(f"{s.name} {s.marks}" for s in subs), working, pct, caution, best_five)


def answer_for(file_name: str, loc_no: int, text: str) -> str | None:
    """The finished answer for one marksheet, or None if its marks could not be read."""
    res = compute(parse_marksheet(text))
    if res is None:
        return None
    parts = [f"Your marks in {file_name} are: {res.lines} [{file_name}, p. {loc_no}].",
             f"Calculated by the app, not by the AI: {res.working}."]
    if res.best_five:
        parts.append(res.best_five)
    if res.caution:
        parts.append(res.caution)
    return "\n\n".join(parts)


def wanted(message: str) -> bool:
    return bool(ASKS_MARKS.search(message))


# ---------- a whole marksheet, summarised in code ----------
# "summarise my class 10 result" got "shows his roll number, school and grades" from the model: true, and useless.
# The facts are on the sheet in fixed places, so they are read out here and the model is not asked.

ASKS_SUMMARY = re.compile(r"\b(summar\w*|overview|details?|result|marksheet|mark\s*sheet|report\s*card|sab\s*kuch|everything)\b", re.I)
# Questions about one field go to the model (or to the percentage maths above), not to the whole summary.
_ONE_FIELD = re.compile(r"\b(roll|school|dob|birth|name|grade in|marks in|how many|which subject|father|mother|seat|registration|regn)\b", re.I)
_SHORT_CLASS = re.compile(r"^\s*(?:my\s+|meri\s+|mera\s+)?(?:class|std)\s*(?:10|12|x|xii)\s*(?:th)?\s*(?:result|marksheet)?\s*\??\s*$", re.I)

_ROLL = re.compile(r"roll\s*(?:no\.?|number|code)?\s*[:\-.]?\s*([A-Z]?\d{5,12})", re.I)
_SCHOOL = re.compile(r"(?:^|\n)[ \t]*(?:name\s+of\s+)?school(?:'?s)?(?:[ \t]*name)?[ \t]*[:\-]?[ \t]*([A-Z][A-Za-z0-9 .,&'()\-]{3,70}?)[ \t]*(?=\n|$)", re.I)
_DOB = re.compile(r"(?:date\s+of\s+birth|d\.?o\.?b\.?)\s*[:\-]?\s*(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}|\d{1,2}\s+[A-Za-z]+\s+\d{4})", re.I)
_RESULT = re.compile(r"\bresult\s*[:\-]?\s*(pass(?:ed)?|fail(?:ed)?|compartment|comptt\.?|essential repeat|qualified|not qualified)\b", re.I)
_EXAM = re.compile(r"((?:senior\s+)?(?:secondary\s+|higher\s+secondary\s+)?school\s+(?:certificate\s+)?examination|high\s+school\s+examination|"
                   r"intermediate\s+examination)[^\n\d]{0,25}(\d{4})?", re.I)


def asks_summary(message: str) -> bool:
    return bool(ASKS_SUMMARY.search(message) and not _ONE_FIELD.search(message)) or bool(_SHORT_CLASS.match(message))


def summary_for(file_name: str, loc_no: int, text: str) -> str | None:
    """The whole sheet in a few lines: exam, name, roll no, school, marks, the percentage (computed here) and the
    result. None if the text is not a marksheet whose marks can be read."""
    from app.core import owner

    res = compute(parse_marksheet(text))
    if res is None:
        return None
    flat = re.sub(r"[ \t]+", " ", text)
    lines = []
    exam = _EXAM.search(flat)
    if exam:
        lines.append(f"Exam: {' '.join(exam.group(1).split()).title()}" + (f" {exam.group(2)}" if exam.group(2) else ""))
    people = owner.named_people(flat)
    if people:
        lines.append(f"Name: {people[0]}")
    for label, rx in (("Roll no", _ROLL), ("Date of birth", _DOB), ("School", _SCHOOL)):
        m = rx.search(flat)
        if m:
            lines.append(f"{label}: {m.group(1).strip(' ,.-')}")
    lines.append(f"Marks: {res.lines}")
    lines.append(f"Percentage: {_fmt(res.percent)}% (calculated by the app, not by the AI: {res.working})")
    if res.best_five:
        lines.append(res.best_five)
    result = _RESULT.search(flat)
    if result:
        lines.append(f"Result: {result.group(1).upper()}")
    out = f"From {file_name} [{file_name}, p. {loc_no}]:\n" + "\n".join(f"- {line}" for line in lines)
    if res.caution:
        out += "\n\n" + res.caution
    return out
