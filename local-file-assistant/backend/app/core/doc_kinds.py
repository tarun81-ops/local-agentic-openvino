"""What kind of personal document a text is, and which school class it is for.

Used three ways: photo cards (core/doc_photos.py) get these words so "marksheet" finds a photo whose text says
"MARKS STATEMENT"; file reading (file_agent.read_chunks) prefers the class-12 page of a combined "10th&12th.pdf"
for a class-12 question; the chat drops passages that are clearly for the other class.

Board wording, not file names: CBSE prints "SECONDARY SCHOOL EXAMINATION" on class 10 sheets and "SENIOR SCHOOL
CERTIFICATE EXAMINATION" on class 12 sheets; state boards say "High School" / "Higher Secondary" / "Intermediate"."""
import re

KINDS = [
    ("marksheet", re.compile(r"marks?\s*statement|statement\s+of\s+marks|mark\s*sheet|marksheet|grade\s*sheet|\bresult\b|marks\s+obtained|total\s+marks", re.I)),
    ("certificate", re.compile(r"certificat", re.I)),
    ("aadhaar", re.compile(r"aadhaa?r|unique identification|\b\d{4}\s\d{4}\s\d{4}\b", re.I)),
    ("pan", re.compile(r"permanent account number|income tax department", re.I)),
    ("admit card", re.compile(r"admit\s*card|hall\s*ticket", re.I)),
    ("receipt", re.compile(r"receipt|invoice|amount paid|transaction id", re.I)),
    ("id card", re.compile(r"identity card|\bid card\b|student id", re.I)),
]

# Scans often lose the spaces ("SENIORSCHOOLCERTIFICATEEXAMINATION"), so the board phrases allow none.
CLASS_TEXT = {
    12: re.compile(r"senior\s*school|senior\s*secondary|higher\s*secondary|10\s*\+\s*2|class\s*[-:]?\s*(?:xii|12)\b|\(\s*xii\s*\)|\b12th\b|\bhsc\b|intermediate", re.I),
    10: re.compile(r"(?<!senior)(?<!senior )(?<!higher)(?<!higher )secondary\s*school\s*(?:examination|certificate)|class\s*[-:]?\s*(?:x|10)\b(?!\s*[+i])|\(\s*x\s*\)|\b10th\b|\bssc\b|high\s*school|matric", re.I),
}
CLASS_WORDS = {12: "class 12 12th hsc senior secondary", 10: "class 10 10th ssc secondary"}

# In a question: "class 12", "12th", "xii", "hsc", "inter" -> 12; "class 10", "10th", "x", "ssc", "matric" -> 10.
_ASKED = {
    12: re.compile(r"\b(?:class|std|standard)\s*(?:12|xii)\b|\b12\s*(?:th|vi|vin|wi)\b|\bxii(?:th)?\b|\bhsc\b|\binter(?:mediate)?\b|\bbarahvi\b", re.I),
    10: re.compile(r"\b(?:class|std|standard)\s*(?:10|x)\b|\b10\s*(?:th|vi|vin|wi)\b|\bssc\b|\bmatric\b|\bdasvi\b|\bhigh school\b", re.I),
}


def kinds(text: str) -> list[str]:
    return [k for k, rx in KINDS if rx.search(text or "")]


def classes_in(text: str) -> set[int]:
    """School classes a text is for (by board wording)."""
    return {n for n, rx in CLASS_TEXT.items() if rx.search(text or "")}


def asked_class(question: str) -> int | None:
    """10 or 12 if the question names exactly one of them."""
    hits = [n for n, rx in _ASKED.items() if rx.search(question or "")]
    return hits[0] if len(hits) == 1 else None


def class_signal(text: str, wanted: int | None) -> int:
    """+1 the text is for the wanted class, -1 it is only for another class, 0 can't tell."""
    if wanted is None:
        return 0
    found = classes_in(text)
    if wanted in found:
        return 1
    return -1 if found else 0
