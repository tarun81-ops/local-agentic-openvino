"""Checks the assistant's rules (not the model) against tables of example inputs, so a change to
the router, the date reader or the memory filter shows up as a number instead of a surprise.

    cd backend
    .venv\\Scripts\\python.exe eval\\assistant_eval.py

Needs no model server. Exit code 1 if any case fails. When a real phrase is misread, add it to a table."""
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 3, 10, 0)  # a Saturday, 10:00 local

# message -> what the rules should decide. "task" / "event" / None for task_intent.
INTENT = [
    ("remind me to call mom tomorrow at 5pm", "task"),
    ("Remind me about the invoice on Friday", "task"),
    ("set a reminder to drink water in 30 minutes", "task"),
    ("please remind me to stretch every day", "task"),
    ("add a task: buy milk", "task"),
    ("create a todo for the report", "task"),
    ("todo: renew passport", "task"),
    ("schedule a meeting with Sam tomorrow 3pm", "event"),
    ("add an appointment on 15 oct", "event"),
    ("book a dentist visit next monday", "event"),
    ("what should I remind my team about?", None),
    ("how do reminders work in this app", None),
    ("explain the meeting notes", None),
    ("the schedule is on page 3", None),
    ("hello", None),
]

# message -> ("remember" | "forget", text) or None
MEMORY_COMMAND = [
    ("remember that my sister is called Priya", ("remember", "my sister is called Priya")),
    ("Please remember I prefer dark mode.", ("remember", "I prefer dark mode")),
    ("forget my cat", ("forget", "my cat")),
    ("forget about the old address", ("forget", "the old address")),
    ("do you remember my name?", None),
    ("I forgot my password", None),
    ("what do you remember about me", None),
]

# message -> tool the actions rules would propose (no index is needed for these)
ACTION = [
    ("open notepad", "open_app"),
    ("launch the calculator", "open_app"),
    ("create a note called Groceries saying milk and eggs", "create_note"),
    ("take a note: call the bank", "create_note"),
    ("open the door", None),
    ("start explaining photosynthesis", None),
    ("move on to the next topic", None),
    ("what is a note?", None),
]

# text -> (local datetime or None, confidence)
DATES = [
    ("call mom tomorrow at 5pm", (datetime(2026, 10, 4, 17, 0), "high")),
    ("send report on Friday 5pm", (datetime(2026, 10, 9, 17, 0), "high")),
    ("take a break in 2 hours", (datetime(2026, 10, 3, 12, 0), "high")),
    ("in 45 minutes", (datetime(2026, 10, 3, 10, 45), "high")),
    ("pay rent on 15 oct", (datetime(2026, 10, 15, 9, 0), "date_only")),
    ("submit the report by Friday", (datetime(2026, 10, 9, 9, 0), "date_only")),
    ("standup next monday 9am", (datetime(2026, 10, 5, 9, 0), "high")),
    ("gym tomorrow morning", (datetime(2026, 10, 4, 9, 0), "high")),
    ("dinner tonight", (datetime(2026, 10, 3, 20, 0), "high")),
    ("call the bank at 17:30", (datetime(2026, 10, 3, 17, 30), "high")),
    ("lunch at noon tomorrow", (datetime(2026, 10, 4, 12, 0), "high")),
    ("buy milk", (None, "none")),
    ("tell me about summer", (None, "none")),
    ("meet me in the evening", (datetime(2026, 10, 3, 18, 0), "high")),
]


def run() -> list[tuple[str, int, list[str]]]:
    """[(area, total, failures)]"""
    if "app.config" not in sys.modules:  # standalone: keep the app's real data untouched
        tmp = Path(tempfile.mkdtemp(prefix="lfa-assistant-eval-"))
        os.environ.update({"DATA_DIR": str(tmp), "DB_PATH": str(tmp / "index.db"), "VECTOR_DB_DIR": str(tmp / "lancedb"), "ASSISTANT_DB_PATH": str(tmp / "assistant.db"), "API_TOKEN": "eval"})
    sys.path.insert(0, str(BACKEND))
    from app.core.assistant import dates
    from app.core.assistant.router import memory_command, task_intent
    from app.core.assistant.tools import intents

    def check(area, table, fn, show=lambda x: x):
        bad = []
        for given, want in table:
            got = fn(given)
            if got != want:
                bad.append(f"{given[:60]!r}: wanted {show(want)}, got {show(got)}")
        return area, len(table), bad

    def action_tool(message):
        found = intents.from_message(message)
        return found["tool"] if found else None

    def read_date(text):
        r = dates.extract(text, NOW)
        return r["when"], r["confidence"]

    return [
        check("task / event intent", INTENT, task_intent),
        check("remember / forget", MEMORY_COMMAND, memory_command),
        check("action intents", ACTION, action_tool),
        check("date reading", DATES, read_date),
    ]


def main() -> int:
    results = run()
    for area, total, failures in results:
        print(f"{area:22} {total - len(failures):>3}/{total:<3} {'ok' if not failures else 'FAILED'}")
        for line in failures:
            print(f"    {line}")
    return 1 if any(f for _, _, f in results) else 0


if __name__ == "__main__":
    sys.exit(main())
