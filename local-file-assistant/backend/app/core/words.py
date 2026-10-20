"""Splitting text into words the same way the search indexes do.

Python's \\w (like SQLite's default tokenizer) does not count Devanagari vowel signs and the virama as word
characters, so "देवता" splits into "द" and "वत". WORD keeps them, so a Hindi word is one word everywhere:
in the FTS indexes (tokenize categories 'L* N* Co Mn Mc'), in search terms and in file-name lookups."""
import re

# \w plus the Devanagari block (U+0900-U+097F except the dandas U+0964/U+0965), Vedic extensions and
# Devanagari Extended, which hold the combining marks \w leaves out.
_MARKS = "\u0900-\u0963\u0966-\u097F\u1CD0-\u1CFF\uA8E0-\uA8FF"
WORD = re.compile(rf"[\w{_MARKS}]+")
NOT_WORD = re.compile(rf"[^\w{_MARKS}]")


def words(text: str) -> list[str]:
    return WORD.findall(text)


def strip_non_word(text: str) -> str:
    return NOT_WORD.sub("", text)
