import json
import re

import httpx
from openai import OpenAI

from app.config import settings
from app.core import prefs


def _without_thinking(create):
    """Wraps chat.completions.create: every call asks the model not to think out loud and gets Qwen's
    non-thinking sampling values (a caller's own values win)."""

    def create_no_think(*args, **kwargs):
        body = dict(kwargs.pop("extra_body", None) or {})
        if settings.ov_disable_thinking:
            body.setdefault("chat_template_kwargs", {"enable_thinking": False})
        body.setdefault("top_k", settings.ov_top_k)  # not an OpenAI field: OVMS reads it from the body
        kwargs["extra_body"] = body
        kwargs.setdefault("temperature", settings.ov_temperature)
        kwargs.setdefault("top_p", settings.ov_top_p)
        kwargs.setdefault("presence_penalty", settings.ov_presence_penalty)
        return create(*args, **kwargs)

    return create_no_think


_THINK = re.compile(r"<think>.*?(?:</think>|$)", re.S)


def strip_think(text: str) -> str:
    """Removes a <think>...</think> block that leaked into the answer (OVMS puts the raw thinking in `content`
    when max_tokens runs out mid-thought, issue #4495). An unclosed block drops everything after it."""
    return _THINK.sub("", text or "").strip()


class ThinkFilter:
    """The same for a token stream: drops text between <think> and </think>, also when a tag arrives split
    across deltas. feed() returns the text that is safe to show now; flush() the rest at the end."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self.buf, self.inside, self.started = "", False, False

    @staticmethod
    def _partial(buf: str, tag: str) -> int:
        """Length of the longest end of buf that could be the start of tag."""
        return next((k for k in range(min(len(tag) - 1, len(buf)), 0, -1) if buf.endswith(tag[:k])), 0)

    def feed(self, text: str) -> str:
        self.buf += text
        out = ""
        while True:
            if self.inside:
                i = self.buf.find(self.CLOSE)
                if i < 0:
                    self.buf = self.buf[-(len(self.CLOSE) - 1):]
                    break
                self.buf, self.inside = self.buf[i + len(self.CLOSE):].lstrip(), False
                continue
            i = self.buf.find(self.OPEN)
            if i >= 0:
                out, self.buf, self.inside = out + self.buf[:i], self.buf[i + len(self.OPEN):], True
                continue
            k = self._partial(self.buf, self.OPEN)
            out, self.buf = out + self.buf[: len(self.buf) - k], self.buf[len(self.buf) - k:]
            break
        if not self.started:  # no leading blank line where a think block was
            out = out.lstrip()
            self.started = bool(out)
        return out

    def flush(self) -> str:
        rest = "" if self.inside else self.buf
        self.buf = ""
        return rest


def too_long(exc: Exception) -> bool:
    """OVMS never truncates a prompt: one longer than the model allows comes back as HTTP 400."""
    msg = str(exc).lower()
    return getattr(exc, "status_code", None) == 400 and any(w in msg for w in ("max length", "too long", "exceeds", "context length", "max_prompt_len"))


def parse_json_object(text: str) -> dict:
    """The first JSON object in a model reply, also when a small model wraps it in ```json fences, puts <think>
    text first or adds a sentence around it. {} if there is none."""
    text = strip_think(text)
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except ValueError:
        pass
    start = text.find("{")
    while start >= 0:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(text[start : i + 1])
                        if isinstance(data, dict):
                            return data
                    except ValueError:
                        pass
                    break
        start = text.find("{", start + 1)
    return {}


def get_client() -> OpenAI:
    """OpenVINO Model Server's OpenAI-compatible API (the model runs on the NPU, iGPU or CPU), or the other
    OpenAI-compatible server switched on in Settings > Model (core/llm/external.py)."""
    from app.core.llm import external

    ext = external.active()
    if ext:  # plain OpenAI API: no OVMS-only request fields
        return OpenAI(base_url=ext["url"], api_key="unused", timeout=httpx.Timeout(settings.llm_timeout_s, connect=5), max_retries=0)
    # No retries: a second attempt doubles the wait, and the caller reports the failure anyway.
    client = OpenAI(
        base_url=settings.ov_base_url,
        api_key="unused",  # OVMS has no API keys; it listens on 127.0.0.1 only
        timeout=httpx.Timeout(settings.llm_timeout_s, connect=5),
        max_retries=0,
    )
    client.chat.completions.create = _without_thinking(client.chat.completions.create)
    return client


def current_model() -> str:
    """The model picked in Settings (saved in the data dir), else OV_CHAT_MODEL.
    (Saved under "ov_model": an old Ollama name saved as "model" must not be sent to OVMS.)"""
    from app.core.llm import external

    ext = external.active()
    return ext["model"] if ext else prefs.get("ov_model") or settings.ov_chat_model


def set_model(name: str) -> None:
    prefs.set("ov_model", name)


def llm_json(prompt: str, max_tokens: int = 300) -> dict:
    """One non-streamed call that must answer with a JSON object; {} if it doesn't."""
    from app.core.llm import idle

    idle.touch()
    r = get_client().chat.completions.create(
        model=current_model(),
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=0.2,  # a plan or a list of facts, not prose
        response_format={"type": "json_object"},
    )
    # OVMS silently skips a response_format it can't apply, so the text is checked: think blocks, ```json fences
    # and chatter around the object are removed (parse_json_object).
    return parse_json_object(r.choices[0].message.content or "")


def server_status() -> dict:
    """Whether OpenVINO Model Server answers, and which models it serves."""
    try:
        r = httpx.get(f"{settings.ov_base_url.rstrip('/')}/models", timeout=3)
        r.raise_for_status()
        return {"connected": True, "models": sorted(m["id"] for m in r.json().get("data", []))}
    except Exception as exc:
        return {"connected": False, "models": [], "error": str(exc)}
