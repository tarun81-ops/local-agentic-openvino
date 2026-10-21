-- Plug-in (MCP) tools that change things or send data are stored with risk 'external': they need approval like the
-- built-in actions but have no undo. SQLite can't change a CHECK constraint in place, so the table is rebuilt.
CREATE TABLE tool_runs_new (
    id          INTEGER PRIMARY KEY,
    conv_id     INTEGER,
    tool        TEXT NOT NULL,
    args_json   TEXT NOT NULL,
    risk        TEXT NOT NULL CHECK (risk IN ('read', 'reversible', 'external')),
    status      TEXT NOT NULL DEFAULT 'proposed'
                CHECK (status IN ('proposed', 'running', 'done', 'failed', 'rejected', 'undoing', 'undone')),
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at  REAL NOT NULL
);
INSERT INTO tool_runs_new SELECT id, conv_id, tool, args_json, risk, status, result_json, created_at FROM tool_runs;
DROP TABLE tool_runs;
ALTER TABLE tool_runs_new RENAME TO tool_runs;
CREATE INDEX tool_runs_conv ON tool_runs(conv_id, id);
