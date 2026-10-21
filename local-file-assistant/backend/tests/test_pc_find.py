"""File-name matching quality for whole-PC access: abbreviations, synonyms, typos, joined words, recency."""
import importlib.util
import os
import time
from pathlib import Path

import pytest

from app.config import settings
from app.core import pc_access
from app.core.assistant import file_agent


@pytest.fixture
def pc(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    drive = tmp_path / "drive"
    docs = drive / "Docs"
    docs.mkdir(parents=True)
    old = time.time() - 400 * 86400
    for name in ("Electricity_Bill-Oct2026.pdf", "Electricity_Bill-Oct2025.pdf", "GridSentinel_V2_Report.pdf", "TrainTracker_Report.pdf",
                 "resume_final_v3.docx", "resume_draft_v1.docx", "Health_Insurance_Policy_2026.pdf", "Class TimeTable.png"):
        (docs / name).write_bytes(b"x")
    for name in ("Electricity_Bill-Oct2025.pdf", "resume_draft_v1.docx"):
        os.utime(docs / name, (old, old))
    pc_access.set_config(enabled=True, drives=[str(drive)])
    pc_access.scan([str(drive)])
    return drive


def names(rows):
    return [r["name"] for r in rows]


def test_month_names_match_their_abbreviation(pc):
    assert names(pc_access.find(["electricity", "october", "2026"]))[0] == "Electricity_Bill-Oct2026.pdf"


def test_synonyms_and_hinglish(pc):
    assert "resume_final_v3.docx" in names(pc_access.find(["biodata"]))
    assert names(pc_access.find(["mediclaim"]))[0] == "Health_Insurance_Policy_2026.pdf"
    assert names(pc_access.find(["bijli", "bill"]))[0].startswith("Electricity_Bill")


def test_a_typo_is_repaired_from_the_catalogs_own_words(pc):
    assert names(pc_access.find(["elecricity", "bill"]))[0].startswith("Electricity_Bill")
    assert pc_access.find(["zzqqxx"]) == []  # nothing close: no wild guesses


def test_joined_camel_case_names_match_as_one_word_and_as_parts(pc):
    assert names(pc_access.find(["gridsentinel"])) == ["GridSentinel_V2_Report.pdf"]
    assert names(pc_access.find(["grid", "sentinel"])) == ["GridSentinel_V2_Report.pdf"]
    assert "Class TimeTable.png" in names(pc_access.find(["time", "table"]))


def test_recent_puts_the_newest_match_first(pc):
    assert names(pc_access.find(["resume"], recent=True))[0] == "resume_final_v3.docx"


def test_the_plan_turns_type_and_date_words_into_filters(pc, monkeypatch):
    monkeypatch.setattr(file_agent, "llm_json", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no model")))
    plan = file_agent.understand("the electricity bill pdf I downloaded last month")
    assert plan["exts"] == [".pdf"] and plan["after"] is not None
    assert "pdf" not in plan["keywords"] and "month" not in plan["keywords"] and "downloaded" not in plan["keywords"]
    assert names(file_agent.candidates(plan))[0] == "Electricity_Bill-Oct2026.pdf"  # the 2025 copy is older than a month
    assert file_agent.understand("find my latest resume")["recent"] is True
    assert file_agent.understand("find my resume")["recent"] is False


def test_an_old_catalog_is_rebuilt_when_the_name_rules_change(pc):
    conn = pc_access.connect()
    conn.execute("UPDATE meta SET v='1' WHERE k='fts_version'")
    conn.commit()
    conn.close()
    assert pc_access.status()["files"] == 0 and pc_access.status()["last_scan"] is None  # the first access clears it
    pc_access.scan([str(pc)])
    assert names(pc_access.find(["gridsentinel"]))


def test_eval_recall_does_not_regress(tmp_path, monkeypatch):
    """Runs eval/pc_find_eval.py's corpus (two drives, decoys, hard questions) and holds recall at the measured level."""
    monkeypatch.setattr(settings, "data_dir", settings.data_dir)  # the eval points it at a throwaway dir; put it back after
    monkeypatch.setattr(file_agent, "llm_json", file_agent.llm_json)
    spec = importlib.util.spec_from_file_location("pc_find_eval", Path(__file__).resolve().parents[1] / "eval" / "pc_find_eval.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    r = mod.run(verbose=False)
    assert r["recall@3"] >= 0.95 and r["recall@8"] >= 0.97 and r["recall@1"] >= 0.85, r


def test_the_step_log_says_when_the_local_model_cannot_plan_or_choose(monkeypatch):
    from app.core.assistant import file_agent

    monkeypatch.setattr(file_agent, "llm_json", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("engine down")))
    assert file_agent.understand("my electricity bill")["model_down"]
    notes = []
    cands = [{"name": f"bill {i}.pdf", "folder": "C:/d", "modified": "", "path": f"C:/d/bill {i}.pdf"} for i in (1, 2, 3)]
    assert file_agent.choose("my electricity bill", cands, 2, notes) == cands[:2]  # the top matches, quietly
    assert notes == [file_agent.DOWN_CHOOSE]
