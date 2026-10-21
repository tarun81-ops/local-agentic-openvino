-- Workspaces: a name, the folders chat may answer from ([] = the whole PC) and an answer style. A conversation
-- belongs to one workspace, or to none ("All files"). Deleting a workspace moves its chats to "All files" and
-- never touches a file on disk.
CREATE TABLE workspaces (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    roots_json   TEXT NOT NULL DEFAULT '[]',
    answer_style TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL,
    pinned       INTEGER NOT NULL DEFAULT 0
);

ALTER TABLE conversations ADD COLUMN workspace_id INTEGER REFERENCES workspaces(id) ON DELETE SET NULL;
CREATE INDEX conversations_workspace ON conversations(workspace_id);
