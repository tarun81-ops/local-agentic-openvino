"""When the chat model was last used, so background work (indexing, the PC reader) can back off
while you are chatting. OpenVINO Model Server keeps its models loaded, so nothing is unloaded
here; voice models unload themselves (core/assistant/voice)."""
import threading
import time

_last_used: float | None = None
_lock = threading.Lock()


def touch() -> None:
    """Call whenever the chat model is used."""
    global _last_used
    with _lock:
        _last_used = time.monotonic()


def recently_used(seconds: float) -> bool:
    """True if the chat model was used in the last `seconds` (background work backs off meanwhile)."""
    with _lock:
        return _last_used is not None and time.monotonic() - _last_used < seconds
