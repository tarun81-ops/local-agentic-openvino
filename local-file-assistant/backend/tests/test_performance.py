"""Performance profiles: what each one overrides, auto from the battery, custom fields,
and that the settings really reach idle unload, the RAM guard, history and captions."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.core import personalize, ram
from app.core.assistant import conversation
from app.core.parsers import image_parser
from app.main import app

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture(autouse=True)
def _reset():
    personalize.update(personalize.DEFAULTS)
    yield
    personalize.update(personalize.DEFAULTS)


def _battery(monkeypatch, plugged):
    """psutil.sensors_battery() as a laptop on battery / on power, or None for a desktop."""
    import psutil

    value = None if plugged is None else SimpleNamespace(percent=50, power_plugged=plugged)
    monkeypatch.setattr(psutil, "sensors_battery", lambda: value)


def test_balanced_is_the_default_and_changes_nothing():
    assert personalize.active_profile() == "balanced"
    for name in ("llm_idle_unload_s", "min_free_ram_mb", "history_turns", "caption_images"):
        assert personalize.effective(name) == getattr(settings, name)


def test_battery_and_plugged_overrides():
    personalize.update({"perf_profile": "battery"})
    assert [personalize.effective(n) for n in ("llm_idle_unload_s", "caption_images", "history_turns")] == [120, False, 3]
    assert personalize.effective("min_free_ram_mb") == settings.min_free_ram_mb  # not part of a profile
    personalize.update({"perf_profile": "plugged"})
    assert [personalize.effective(n) for n in ("llm_idle_unload_s", "caption_images", "history_turns")] == [1800, True, 6]


@pytest.mark.parametrize("plugged,expected", [(False, "battery"), (True, "plugged"), (None, "balanced")])
def test_auto_follows_the_battery_and_degrades_without_one(monkeypatch, plugged, expected):
    _battery(monkeypatch, plugged)
    personalize.update({"perf_profile": "auto"})
    assert personalize.active_profile() == expected
    assert personalize.performance_status(1000)["chosen"] == "auto"


def test_auto_survives_psutil_failing(monkeypatch):
    import psutil

    def boom():
        raise RuntimeError("no sensors")

    monkeypatch.setattr(psutil, "sensors_battery", boom)
    personalize.update({"perf_profile": "auto"})
    assert personalize.active_profile() == "balanced"


def test_custom_fields_win_over_the_profile_and_validate():
    personalize.update({"perf_profile": "battery", "perf_unload_minutes": 45, "perf_min_free_ram_mb": 900})
    assert personalize.effective("llm_idle_unload_s") == 45 * 60 and personalize.effective("min_free_ram_mb") == 900
    personalize.update({"perf_unload_minutes": 0})
    assert personalize.effective("llm_idle_unload_s") == 0  # never unload
    personalize.update({"perf_unload_minutes": None, "perf_min_free_ram_mb": None})
    assert personalize.effective("llm_idle_unload_s") == 120
    for bad in ({"perf_unload_minutes": 241}, {"perf_unload_minutes": -1}, {"perf_unload_minutes": 1.5}, {"perf_unload_minutes": True}, {"perf_min_free_ram_mb": 9000}, {"perf_profile": "turbo"}):
        with pytest.raises(ValueError):
            personalize.update(bad)


def test_idle_unload_uses_the_profile_with_a_fake_clock():
    """Voice models are the ones unloaded when idle now (OVMS keeps the chat model loaded)."""
    from app.core.assistant.voice import Lazy

    model = Lazy()

    def loaded_at(t: float) -> None:
        model._obj, model._last = object(), t

    personalize.update({"perf_profile": "battery"})  # 120 s
    loaded_at(0)
    assert not model.drop_if_idle(119) and model.drop_if_idle(121)
    personalize.update({"perf_profile": "plugged"})  # 1800 s
    loaded_at(0)
    assert not model.drop_if_idle(1000) and model.drop_if_idle(1801)
    personalize.update({"perf_unload_minutes": 0})  # never
    loaded_at(0)
    assert not model.drop_if_idle(10**6)


def test_ram_guard_uses_the_custom_threshold(monkeypatch):
    monkeypatch.setattr(ram.psutil, "virtual_memory", lambda: SimpleNamespace(available=1500 * 1024 * 1024, total=16000 * 1024 * 1024))
    assert not ram.is_low()  # MIN_FREE_RAM_MB=0 in tests
    personalize.update({"perf_min_free_ram_mb": 2000})
    assert ram.is_low()


def test_history_window_follows_the_profile():
    msgs = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(20)]
    personalize.update({"perf_profile": "battery"})
    short = conversation.window(msgs)
    personalize.update({"perf_profile": "plugged"})
    assert len(short) < len(conversation.window(msgs))


def test_captions_follow_the_profile_unless_the_folder_decides(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(image_parser, "caption", lambda p: calls.append(p) or "a cat")
    monkeypatch.setattr(image_parser, "available", lambda: False)
    monkeypatch.setattr(image_parser.settings, "caption_images", False)
    img = tmp_path / "a.png"
    img.write_bytes(b"x")
    image_parser.parse_image(img)
    assert not calls
    personalize.update({"perf_profile": "plugged"})
    image_parser.parse_image(img)
    assert len(calls) == 1
    token = image_parser.CAPTION_OVERRIDE.set(False)  # a folder set to OCR only
    try:
        image_parser.parse_image(img)
    finally:
        image_parser.CAPTION_OVERRIDE.reset(token)
    assert len(calls) == 1


def test_status_endpoint_and_the_optional_bigger_model_note(monkeypatch):
    monkeypatch.setattr(ram, "psutil", SimpleNamespace(virtual_memory=lambda: SimpleNamespace(available=12000 * 1024 * 1024, total=16000 * 1024 * 1024)))
    with TestClient(app, base_url="http://127.0.0.1:8756") as c:
        balanced = c.get("/personalize/performance", headers=AUTH).json()
        assert balanced["active"] == "balanced" and balanced["note"] == "" and set(balanced["words"]) == {"battery", "balanced", "plugged", "auto"}
        c.put("/personalize", json={"perf_profile": "plugged"}, headers=AUTH)
        plugged = c.get("/personalize/performance", headers=AUTH).json()
        assert plugged["values"]["llm_idle_unload_s"] == 1800 and "never switched on" in plugged["note"]
        monkeypatch.setattr(ram, "psutil", SimpleNamespace(virtual_memory=lambda: SimpleNamespace(available=3000 * 1024 * 1024, total=16000 * 1024 * 1024)))
        assert c.get("/personalize/performance", headers=AUTH).json()["note"] == ""  # not when RAM is tight
