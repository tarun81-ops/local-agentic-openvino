# Roadmap

Done
- Vertical slice, hybrid search, verifier, organizer with reversible apply.
- Electron app with the Modernist UI: Ask, Search, Organize, Index, Settings, and the
  Ctrl+Shift+Space overlay.
- Parsers for PDF, docx, pptx, xlsx, txt/md/csv, html, eml; OCR for images when rapidocr is installed.
- Security: token always required, Origin check, paths confined to indexed folders.
- Packaging: PyInstaller backend inside the NSIS installer, free port, tray.

- Hardening pass (2026-09-30): schema upgrade rebuilds, RAM guard + idle unload, fast
  rescans + stop, scanned-PDF OCR, optional image captions, organizer checks for Windows
  names/long paths/links/case, Host-header check, logs, backend auto-restart, CI, eval harness.

- Retrieval pass (2026-10-01): `eval/run_eval.py --retrieval` (synthetic 36-file corpus, 43
  questions); text/HTML/email parsers; file name + folder in the embedded text; one result per
  file on Search. Porter stemming and a bigger candidate pool were measured and left out.
  Numbers in docs/RESEARCH-accuracy-device-learning.md.

- Personalization pass (2026-10-04): About me, answer styles and language, memory review, per-request and
  per-folder styles, study cards, per-folder indexing rules, collections and a recent-files home, themes
  and accents with a contrast test, performance profiles, reminders about files, an opt-in new-file nudge
  (docs/ARCHITECTURE.md, "Personalization").

Next
1. Run docs/TESTING-ON-WINDOWS.md on the laptop and record an eval baseline for qwen3-vl:2b.
2. Grow eval/questions.jsonl with 30–50 questions over real documents; tune chunk size and
   the verifier threshold against it. The verifier checks a claim is in the source, not
   that it answers the question: an eval run with a stand-in model showed on-topic
   citations for off-topic answers, so answer relevance needs its own check.
3. Multi-turn Ask (send earlier turns as context).
4. Benchmark OpenVINO Model Server on NPU vs iGPU vs CPU (docs/OPENVINO.md).
5. Try the personalization features on the laptop (docs/TESTING-ON-WINDOWS.md, section 2) and measure
   whether the added prompt text changes answer quality or latency on qwen3-vl:2b.
6. Not done: saved study decks (cards live only in the dialog and export to CSV), per-folder subfolder
   rules beyond name patterns, and recolouring charts without leaving the page.
