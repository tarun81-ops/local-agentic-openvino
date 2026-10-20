from pathlib import Path

from app.core.assistant import prompts
from app.core.llm import idle
from app.config import settings
from app.core.llm.client import ThinkFilter, current_model, get_client, strip_think, too_long

PROMPT = """Answer the question using only the excerpts below. After each fact, cite the excerpt it
came from by copying that excerpt's label exactly, in round brackets, for example (report.pdf, page 2)
or (notes.docx, part 1). Never invent a label. Use only excerpts that actually answer the question and ignore the rest.
If none of them answer it, say so in one sentence. Keep the answer short and to the point.
Talk to the person asking as "you" (never "the user"). Answer only what was asked: don't add facts
about the person that the question didn't ask for.

{context}

Question: {question}
{style}"""

MAX_ANSWER_TOKENS = 500  # bounds worst-case latency on a runaway/repetitive generation


def label(chunk: dict) -> str:
    """The citation label for a chunk: file name only (small models copy long paths badly)."""
    return f"{Path(chunk['path']).name}, {chunk['loc_kind']} {chunk['loc_no']}"


def cost(text: str) -> int:
    """Prompt size in "English characters": Devanagari and other non-Latin text takes about 3x the tokens."""
    return sum(1 if ord(ch) < 0x250 else 3 for ch in text or "")


def _fit(question: str, chunks: list[dict], history: list[dict], style: str) -> tuple[list[dict], list[dict]]:
    """Drops the oldest chat turns, then shortens the last passages, until the prompt fits max_prompt_chars.
    The question, the rules and the first (best) passages are kept whole."""
    budget = settings.max_prompt_chars
    fixed = cost(prompts.system()) + cost(PROMPT) + cost(question) + cost(style) + 50
    history = list(history)
    while history and fixed + sum(cost(m["content"]) for m in history) > budget * 0.25:  # chat history: at most a quarter
        history = history[2:] if len(history) > 1 else []
    room = budget - fixed - sum(cost(m["content"]) for m in history)
    kept: list[dict] = []
    for c in chunks:
        head = cost(f"[{label(c)}]\n") + 2
        if room - head < 120:
            break
        text = c["text"]
        if cost(text) > room - head:  # cut this passage to what's left (by characters, cheap and safe)
            keep = len(text)
            while keep > 0 and cost(text[:keep]) > room - head:
                keep = int(keep * 0.85)
            text = text[:keep]
        kept.append({**c, "text": text})
        room -= head + cost(text)
    return kept, history


def _messages(question: str, chunks: list[dict], history: list[dict] | None = None, style: str = "") -> list[dict]:
    chunks, history = _fit(question, chunks, history or [], style)
    context = "\n\n".join(f"[{label(c)}]\n{c['text']}" for c in chunks)
    return [{"role": "system", "content": prompts.system()}, *history,
            {"role": "user", "content": PROMPT.format(context=context, question=question, style=style)}]


def _stream(messages: list[dict], model: str | None):
    """Yields text deltas; returns (as the generator's return value) True if cut off at the token cap."""
    idle.touch()
    stream = get_client().chat.completions.create(
        model=model or current_model(),
        messages=messages,
        max_tokens=MAX_ANSWER_TOKENS,
        stream=True,
    )
    truncated = False
    think = ThinkFilter()
    for chunk in stream:
        if not chunk.choices:
            continue
        if chunk.choices[0].delta.content:
            shown = think.feed(chunk.choices[0].delta.content)
            if shown:
                yield shown
        truncated = truncated or chunk.choices[0].finish_reason == "length"
    rest = think.flush()
    if rest:
        yield rest
    idle.touch()
    return truncated


def answer_stream(question: str, chunks: list[dict], history: list[dict] | None = None, model: str | None = None, style: str = ""):
    """File mode: answers from the excerpts only. Same delta/return convention as _stream.
    `style` is a ready-made prompts.style_instruction line; empty keeps the old prompt."""
    try:
        return (yield from _stream(_messages(question, chunks, history, style), model))
    except Exception as exc:  # the server refuses an over-long prompt before generating anything
        if not too_long(exc) or (len(chunks) <= 1 and not history):
            raise
        fewer = chunks[: max(1, len(chunks) // 2)]  # best passages come first; history goes first
        return (yield from answer_stream(question, fewer, None, model, style))


def chat_stream(message: str, history: list[dict] | None = None, model: str | None = None, style: str = ""):
    """General chat: no excerpts, no citations."""
    system = prompts.system(style or prompts.CHAT_RULES)
    try:
        return (yield from _stream([{"role": "system", "content": system}, *(history or []), {"role": "user", "content": message}], model))
    except Exception as exc:
        if not too_long(exc) or not history:
            raise
        return (yield from chat_stream(message, history[2:] or None, model, style))  # drop the oldest exchange


def rewrite_query(message: str, history: list[dict], model: str | None = None) -> str:
    """Turns a follow-up into a standalone search query. Falls back to the last question + message."""
    last_q = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
    try:
        idle.touch()
        text = "\n".join(f"{m['role']}: {m['content'][:300]}" for m in history[-4:])
        r = get_client().chat.completions.create(
            model=model or current_model(),
            messages=[{"role": "user", "content": prompts.REWRITE.format(history=text, message=message)}],
            max_tokens=40,
            temperature=0.3,
        )
        return strip_think(r.choices[0].message.content or "").strip().strip('"') or f"{last_q} {message}".strip()
    except Exception:
        return f"{last_q} {message}".strip()
