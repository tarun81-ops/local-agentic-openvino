"""Optional: answer with another OpenAI-compatible server (for example LM Studio on this PC) instead of the built-in
engine. Off by default. Only chat goes there; file search and embeddings always stay on this PC.

A server that isn't on this PC (not a loopback address) receives every question and the file passages it is answered
from, so switching one on needs a second, explicit confirmation (confirm_remote), and Settings says so in words."""
import ipaddress
from urllib.parse import urlparse

from app.core import prefs

_KEY = "external_server"
OFF = {"enabled": False, "url": "", "model": ""}


def is_loopback(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def get() -> dict:
    cfg = {**OFF, **(prefs.get(_KEY) or {})}
    return {**cfg, "loopback": is_loopback(cfg["url"]) if cfg["url"] else True}


def set(enabled: bool, url: str = "", model: str = "", confirm_remote: bool = False) -> dict:
    """Saves the setting, or ValueError with what to fix. Switching off always works."""
    url, model = (url or "").strip().rstrip("/"), (model or "").strip()
    if enabled:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("Enter the server's address, for example http://127.0.0.1:1234/v1 (LM Studio).")
        if not model:
            raise ValueError("Enter the model name the server should use (as the server lists it).")
        if not is_loopback(url) and not confirm_remote:
            raise ValueError(f"{parsed.hostname} is not this PC. Your questions and the text of the files they are "
                             "answered from would be sent there. Confirm to use it anyway.")
    prefs.set(_KEY, {"enabled": bool(enabled), "url": url, "model": model})
    return get()


def active() -> dict | None:
    """The server to answer with, or None for the built-in engine."""
    cfg = get()
    return cfg if cfg["enabled"] and cfg["url"] and cfg["model"] else None
