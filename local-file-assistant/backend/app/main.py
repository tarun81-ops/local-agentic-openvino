import logging
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import routes_actions, routes_chat, routes_collections, routes_context, routes_conversations, routes_events, routes_files, routes_find, routes_forms, routes_learning, routes_memory, routes_organize, routes_pc, routes_personalize, routes_proactive, routes_search, routes_settings, routes_setup, routes_study, routes_tasks, routes_voice
from app.config import settings
from app.core import doc_photos, pc_access, pc_content
from app.core.assistant import memory_store, scheduler
from app.core.watcher import watcher
from app.core.llm import ovms_runner
from app.db import assistant_db
from app.security import make_guard, resolve_token

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def _setup_logging() -> None:
    """Console plus %APPDATA%\\LocalFileAssistant\\logs\\backend.log (5 MB x 3), so a crash in the
    installed app leaves something to read. Only file paths and errors are logged, never
    file contents, questions or the API token."""
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        log_dir = settings.data_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(log_dir / "backend.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=logging.INFO, format=_FORMAT, handlers=handlers, force=True)


_setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    assistant_db.migrate()
    ovms_runner.ensure_running_async()  # the model server, so no terminal has to stay open; never delays startup
    watcher.start()
    # Memories come only from "remember that ..." in chat. Facts the model used to guess from chats
    # waited in a review queue as "pending" and were never used; that queue is gone, so clear it.
    memory_store.drop_pending()
    scheduler.start()
    if settings.auto_rescan:
        auto = routes_files.auto_access_startup()
        if auto:
            logging.getLogger(__name__).info("auto access: added %d common folder(s)", len(auto))
        pending = routes_files.rescan_unscanned()
        if pending:
            logging.getLogger(__name__).info("rebuilding the index for %d folder(s)", len(pending))
        pc_access.crawler.start_if_stale()  # whole-PC name catalog: only runs if the user switched it on
        pc_content.worker.start()  # reads file contents of those drives in the background (idles while PC access is off)
        doc_photos.worker.start()  # names-only: OCRs each photo once so document photos (IMG-...jpg marksheets) can be found
    yield
    pc_content.worker.stop()
    doc_photos.worker.stop()
    pc_access.crawler.stop()
    scheduler.stop()
    watcher.stop()
    ovms_runner.stop()


app = FastAPI(title="Local File Assistant", lifespan=lifespan)

# Order matters: the last middleware added runs first. CORS must answer preflights
# before the token guard sees them.
app.middleware("http")(make_guard(resolve_token(settings.data_dir), settings.origin_list))
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.origin_list,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.get("/health")
def health():
    return {"ok": True}


for module in (routes_search, routes_actions, routes_chat, routes_collections, routes_context, routes_conversations, routes_events, routes_files, routes_find, routes_forms, routes_learning, routes_memory, routes_organize, routes_pc, routes_personalize, routes_proactive, routes_settings, routes_setup, routes_study, routes_tasks, routes_voice):
    app.include_router(module.router)
