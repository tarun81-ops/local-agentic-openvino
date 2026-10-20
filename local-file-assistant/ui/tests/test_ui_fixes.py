"""Browser tests for UI fixes, run against the built app (ui/dist) with a fake backend.

    cd ui
    npm run build:web
    pip install pytest playwright   (then: python -m playwright install chromium)
    pytest tests

Every API call is answered by page.route below, so no backend or model server is needed. The tests skip
if Playwright or ui/dist is missing."""
import functools
import http.server
import json
import threading
import time
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

DIST = Path(__file__).resolve().parents[1] / "dist"
API = "http://127.0.0.1:8756"


@pytest.fixture(scope="module")
def site():
    if not (DIST / "index.html").exists():
        pytest.skip("run `npm run build:web` first")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(DIST))
    handler.log_message = lambda *a: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def stub(navigate_to=None, delays=None):
    """window.lfa as Electron's preload provides it. navigate_to: a page the tray asks for at startup,
    before the backend is up. delays: {api path: seconds} the page waits after the request was answered,
    so the reply is still "in flight" for the page while the test clicks (sleeping in a route handler
    would block the test itself)."""
    nav = f"cb({json.dumps(navigate_to)});" if navigate_to else ""
    slow = (
        f"const __d = {json.dumps(delays)}; const __f = window.fetch.bind(window);"
        " window.fetch = async (u, o) => { const p = new URL(String(u)).pathname;"
        " const res = await __f(u, o); if (__d[p]) await new Promise((r) => setTimeout(r, __d[p] * 1000)); return res; };"
        if delays else ""
    )
    return slow + (
        "window.lfa = new Proxy({config: async () => ({port: 8756, token: 't'}),"
        f" onNavigate: (cb) => {{ {nav} }}, onVoiceToggle: () => {{}},"
        " shortcut: async () => 'Ctrl+Shift+Space', getLaunchAtStartup: async () => ({enabled: false, supported: true})},"
        " {get: (t, k) => (k in t ? t[k] : async () => ({}))});"
    )


# Reply shapes copied from the real backend, with empty data.
DEFAULTS = {
    "/health": {"ok": True},
    "/settings": {"model": "", "server": {"connected": False, "models": []}, "memory": {"free_mb": 4000, "total_mb": 16000, "low": False}},
    "/conversations": {"conversations": []},
    "/collections": {"collections": []},
    "/files/roots": {"roots": []},
    "/files": {"files": []},
    "/files/common": {"enabled": True, "folders": []},
    "/files/recent": {"files": []},
    "/files/index/status": {"state": "idle", "folder": None, "queue": [], "error": None, "seen": 0, "indexed": 0,
                            "skipped": 0, "failed": 0, "removed": 0, "keyword_only": 0, "errors": []},
    "/organize/history": {"batches": []},
    "/memory": {"memories": [], "counts": {"active": 0, "pending": 0}},
    "/pc": {"enabled": False, "drives": [], "extra_excludes": [], "available_drives": ["C:\\"], "files": 0,
            "last_scan": None, "running": False, "progress": {}, "scan_error": "", "read_content": True,
            "read_images": False, "content": {"paused": "off", "current": "", "indexed": 0, "failed": 0, "running": False,
                                              "eligible": 0, "done": 0, "pending": 0, "read_content": True, "read_images": False}},
}


class FakeBackend:
    def __init__(self, page, overrides=None):
        self.calls: list[tuple[float, str]] = []
        self.overrides = overrides or {}
        page.route(f"{API}/**", self._handle)

    def _handle(self, route):
        path = route.request.url.split("8756", 1)[1].split("?", 1)[0]
        self.calls.append((time.monotonic(), path))
        if route.request.method == "OPTIONS":
            return route.fulfill(status=204, headers=self._cors())
        body = self.overrides.get(path, DEFAULTS.get(path, {}))
        route.fulfill(status=200, headers={**self._cors(), "Content-Type": "application/json"}, body=json.dumps(body))

    @staticmethod
    def _cors():
        return {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*"}

    def count(self, path, after=0.0):
        return sum(1 for t, p in self.calls if p == path and t >= after)


@pytest.fixture
def page():
    with playwright_api.sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page(viewport={"width": 1280, "height": 860})
        pg.errors = []
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        yield pg
        browser.close()


def test_files_tab_stops_polling_after_you_leave_it(page, site):
    # The file-name list is being updated, so the Files tab polls PC status. The reply is slow; leaving
    # the tab while it is in flight must not start a poll loop that never stops.
    running = {**DEFAULTS["/pc"], "enabled": True, "drives": ["C:\\"], "running": True, "progress": {"seen": 10}}
    fake = FakeBackend(page, overrides={"/pc": running})
    page.add_init_script(stub(delays={"/pc": 0.8}))
    page.goto(f"{site}/index.html")
    page.click('.nav-item:has-text("SETTINGS")')
    page.click("#settings-tab-files")
    page.wait_for_timeout(4500)  # a few polls (each one: 0.8 s reply + 1.5 s pause)
    seen = fake.count("/pc")
    assert seen >= 2
    for _ in range(150):  # leave right after the next status request went out, while its reply is in flight
        if fake.count("/pc") > seen:
            break
        page.wait_for_timeout(20)
    page.click("#settings-tab-memory")
    left = time.monotonic()
    page.wait_for_timeout(5000)
    assert fake.count("/pc", after=left) == 0
    assert page.errors == []


def test_tray_link_at_startup_opens_the_right_settings_tab(page, site):
    FakeBackend(page)
    page.add_init_script(stub(navigate_to="index"))  # asked for before the backend is ready
    page.goto(f"{site}/index.html")
    page.wait_for_selector(".tab[aria-selected='true']")
    assert page.get_attribute(".tab[aria-selected='true']", "id") == "settings-tab-files"
    assert page.errors == []


def _tidy_backend(page):
    plan = {"summary": "1 file to sort.", "root": "C:\\Downloads", "kind": "tidy", "freed_bytes": 0, "scanned": 1,
            "dropped_invalid": 0,
            "actions": [{"op": "move", "path": "C:\\Downloads\\a.pdf", "to": "C:\\Downloads\\Documents\\a.pdf",
                         "group": "Sort by type/Documents", "selected": True, "size": 10, "reason": ".pdf file"}]}
    applied = {"batch": "b1", "results": [{"op": "move", "path": "C:\\Downloads\\a.pdf", "to": "C:\\Downloads\\Documents\\a.pdf", "ok": True}]}
    return FakeBackend(page, overrides={
        "/organize/tidy/folders": {"folders": [{"key": "downloads", "label": "Downloads", "path": "C:\\Downloads"}]},
        "/organize/tidy": plan,
        "/organize/tidy/start": {"job": "j1"},
        "/organize/tidy/jobs/j1": {"state": "done", "phase": "", "done": 0, "total": None, "plan": plan, "error": None},
        "/organize/apply": applied,
    })


def test_plan_buttons_are_locked_while_approved_changes_run(page, site):
    _tidy_backend(page)
    page.add_init_script(stub(delays={"/organize/apply": 1.5}))
    page.goto(f"{site}/index.html")
    page.click('.nav-item:has-text("ORGANIZE")')
    page.click("text=FIND CLUTTER")
    page.wait_for_selector("text=1 change selected")
    page.click('.org-actionbar button:has-text("Apply")')
    page.wait_for_timeout(300)
    assert page.locator("text=FIND CLUTTER").is_disabled() and page.locator("#tidy-root").is_disabled()
    page.wait_for_selector("text=1 item moved")  # the result stays in view
    assert not page.locator("text=FIND CLUTTER").is_disabled()  # unlocked afterwards
    assert page.errors == []


# ---------- the simplified app: removed features stay removed ----------

def test_organize_is_tidy_up_only(page, site):
    _tidy_backend(page)
    page.add_init_script(stub())
    page.goto(f"{site}/index.html")
    page.click('.nav-item:has-text("ORGANIZE")')
    page.wait_for_selector("text=FIND CLUTTER")
    assert page.locator("text=AI PLAN").count() == 0 and page.locator("text=PROPOSE PLAN").count() == 0
    assert page.errors == []


def test_memory_tab_is_a_simple_list_with_no_review_queue(page, site):
    fake = FakeBackend(page, overrides={"/memory": {"memories": [{"id": 1, "text": "I study ETE in Raipur", "kind": "fact", "pinned": False, "status": "active"}],
                                                    "counts": {"active": 1, "pending": 0}}})
    page.add_init_script(stub())
    page.goto(f"{site}/index.html")
    page.click('.nav-item:has-text("SETTINGS")')
    page.click("#settings-tab-memory")
    page.wait_for_selector("text=I study ETE in Raipur")
    for gone in ("APPROVE", "DISMISS", "NEEDS REVIEW", "WAITING FOR YOUR REVIEW", "EXPORT"):
        assert page.locator(f"text={gone}").count() == 0, gone
    assert page.locator("text=EDIT").count() == 1 and page.locator("text=DELETE").count() == 1
    assert page.locator("text=FORGET EVERYTHING").count() == 1
    assert page.errors == []


def test_files_tab_is_just_pc_access_and_undo(page, site):
    on = {**DEFAULTS["/pc"], "enabled": True, "drives": ["C:\\"], "available_drives": ["C:\\", "D:\\"], "files": 18341,
          "last_scan": "2026-10-15T06:00:00+00:00"}
    fake = FakeBackend(page, overrides={"/pc": on, "/organize/history": {"batches": [{"batch": "b1", "at": "2026-10-15T06:00:00+00:00", "moves": 3, "trashed": 1, "entries": []}]}})
    page.add_init_script(stub())
    page.goto(f"{site}/index.html")
    page.click('.nav-item:has-text("SETTINGS")')
    page.click("#settings-tab-files")
    page.wait_for_selector("text=Knows 18,341 files")
    for gone in ("ADD FOLDER", "INDEXED FOLDERS", "SEMANTIC COVERAGE", "FILE TYPES", "READ FILE CONTENTS", "FORGET WHAT WAS READ", "SAVE SKIP LIST"):
        assert page.locator(f"text={gone}").count() == 0, gone
    assert page.locator("text=UPDATE LIST NOW").count() == 1 and page.locator("text=TURN OFF").count() == 1
    assert page.locator("text=Organize → History").count() == 1  # the undo list moved to Organize
    for path in ("/files", "/files/roots", "/files/common", "/files/index/status"):
        assert fake.count(path) == 0, path  # the old index endpoints are no longer called from here
    # Adding a drive saves straight away.
    page.get_by_label("Cover drive D:\\").check()
    page.wait_for_timeout(300)
    assert fake.count("/pc/settings") == 1
    assert page.errors == []


def test_overlay_quick_search_uses_find(page, site):
    fake = FakeBackend(page, overrides={"/find": {"results": [{"path": "C:\\Docs\\offer.pdf", "name": "offer.pdf", "folder": "C:\\Docs",
                                                              "ext": ".pdf", "size": 10, "mtime": 0, "modified": "01 Oct 2026", "snippet": "",
                                                              "loc": None, "score": 1, "why": ["name has “offer”"]}],
                                                 "understood": {"words": ["offer"]}, "relaxed": [], "pc_access": True, "semantic": False, "ms": 12}})
    page.add_init_script(stub())
    page.goto(f"{site}/overlay.html")
    page.wait_for_selector("input")
    page.fill("input", "my offer letter")
    page.wait_for_selector("text=offer.pdf")
    assert fake.count("/find") >= 1 and fake.count("/search") == 0
    assert page.errors == []
